"""YAML grammar parser + emitter for v1 PII policy files.

The grammar is a flat mapping with a small fixed schema:

    version: 1                                # required, int, must equal 1
    description: "..."                        # optional, free-text
    block:                                    # required list (may be empty)
      - credential
      - payment_card
      - government_id
    column_overrides:                         # optional mapping
      public.users.email:
        sensitivity: internal                 # required when override is set
        categories: []                        # optional, defaults to []
      public.payment_methods.card_number_last4:
        sensitivity: internal
        categories: []

Parse errors raise `PolicyYamlError` (a `ValueError` subclass) with a
message that names the offending field. The CLI (`schemabrain policy
apply <path>`) surfaces the message verbatim and exits 1.

Strict-keys policy: unknown top-level keys and unknown override fields
are rejected at parse time so a typo like `blocks:` (note plural)
doesn't silently become a no-op. Mirrors `metrics/yaml_grammar.py` in
style — version field required, dataclass validation deferred to
`__post_init__`, errors wrap with field context.

The `column_overrides` mapping (qualified_column -> {sensitivity,
categories}) is YAML-natural: editing a YAML file produced by the
emitter feels like editing a config rather than a list of records.
The on-disk shape is one block per qualified column key, which makes
git diffs read as "this column's policy changed" rather than "the
overrides list reshuffled."
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from schemabrain.pii.categories import (
    PII_CATEGORIES_ORDERED,
    SENSITIVITIES,
    PIICategory,
    Sensitivity,
)
from schemabrain.pii.policy import ColumnOverride, Policy

_ALLOWED_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "version",
        "description",
        "block",
        "column_overrides",
    }
)
_REQUIRED_TOP_LEVEL_KEYS: frozenset[str] = frozenset({"version", "block"})

_ALLOWED_OVERRIDE_KEYS: frozenset[str] = frozenset({"sensitivity", "categories"})
_REQUIRED_OVERRIDE_KEYS: frozenset[str] = frozenset({"sensitivity"})

_SUPPORTED_VERSION: int = 1


class PolicyYamlError(ValueError):
    """Raised when a pii_policy YAML document fails to parse or validate.

    Subclass of `ValueError` so callers that just want to catch "bad
    input" can do so without importing this module. The CLI catches
    the specific type to keep its exit-code contract narrow.
    """


def parse_policy_yaml(text: str) -> Policy:
    """Parse `text` (raw YAML) into a `Policy`.

    Raises `PolicyYamlError` on any structural, schema, or value
    violation. The error message is user-facing — keep it short and
    name the field that's wrong.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyYamlError(f"failed to parse policy YAML: {exc}") from exc

    if data is None:
        raise PolicyYamlError("policy YAML is empty")
    if not isinstance(data, dict):
        raise PolicyYamlError(
            f"policy YAML must be a mapping at the top level "
            f"(got {type(data).__name__})"
        )

    _check_keys(
        data,
        allowed=_ALLOWED_TOP_LEVEL_KEYS,
        required=_REQUIRED_TOP_LEVEL_KEYS,
        scope="top-level",
    )
    _check_version(data["version"])

    description = _optional_str(data, "description", default="")
    block = _parse_block(data["block"])
    column_overrides = _parse_column_overrides(data.get("column_overrides"))

    try:
        return Policy(
            block=block,
            column_overrides=column_overrides,
            description=description,
        )
    except ValueError as exc:
        raise PolicyYamlError(f"policy validation failed: {exc}") from exc


def policy_to_yaml(policy: Policy) -> str:
    """Serialise a `Policy` into the v1 policy YAML body.

    Inverse of `parse_policy_yaml`: any `Policy` round-tripped through
    `policy_to_yaml` then `parse_policy_yaml` is value-equal to the
    input.

    Block categories are emitted in the canonical
    `PII_CATEGORIES_ORDERED` order so version-control diffs stay
    stable when the operator's edit is logically a no-op (two writes
    of the same set don't produce noisy reordering).

    `column_overrides` are emitted sorted by qualified column for the
    same reason — diff stability on no-op writes.

    `description` is omitted when empty so the YAML body stays compact
    for minimal policies; the parser fills the field with the empty
    string by default.

    Uses `yaml.safe_dump` so an operator-supplied description
    containing YAML-special characters is properly quoted/escaped.
    """
    body: dict[str, object] = {"version": _SUPPORTED_VERSION}
    if policy.description:
        body["description"] = policy.description
    body["block"] = [c for c in PII_CATEGORIES_ORDERED if c in policy.block]
    if policy.column_overrides:
        sorted_overrides = sorted(
            policy.column_overrides, key=lambda o: o.qualified_column
        )
        body["column_overrides"] = {
            override.qualified_column: {
                "sensitivity": override.sensitivity,
                "categories": [
                    c for c in PII_CATEGORIES_ORDERED if c in override.categories
                ],
            }
            for override in sorted_overrides
        }
    return yaml.safe_dump(
        body,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).rstrip()


def parse_policy_yaml_file(path: Path) -> Policy:
    """Read `path` and parse its contents as a policy YAML document.

    `FileNotFoundError` / `IsADirectoryError` propagate verbatim so
    the caller can distinguish "missing file" from "malformed YAML."
    `PermissionError` and `UnicodeDecodeError` are wrapped as
    `PolicyYamlError`. Mirrors `metrics/yaml_grammar.py`'s shape.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError):
        raise
    except PermissionError as exc:
        raise PolicyYamlError(
            f"cannot read {path}: permission denied ({exc.strerror or exc})"
        ) from exc
    except UnicodeDecodeError as exc:
        raise PolicyYamlError(
            f"{path} is not a valid UTF-8 text file (policy YAML must be UTF-8): "
            f"{exc.reason}"
        ) from exc
    return parse_policy_yaml(text)


# ----- field validators -------------------------------------------------------


def _check_keys(
    data: dict[str, Any],
    *,
    allowed: frozenset[str],
    required: frozenset[str],
    scope: str,
) -> None:
    keys = set(data.keys())
    missing = required - keys
    if missing:
        raise PolicyYamlError(f"missing required {scope} field(s): {sorted(missing)}")
    unknown = keys - allowed
    if unknown:
        raise PolicyYamlError(f"unknown {scope} keys: {sorted(unknown)}")


def _check_version(value: Any) -> None:
    # Same posture as the metrics/entities/joins parsers: only the
    # integer 1 is accepted. `version: "1"` (string), `version: 1.0`
    # (float), and `version: true` (bool, an int subclass) all
    # signal a hand-typed file that drifted from the grammar.
    if not isinstance(value, int) or isinstance(value, bool):
        raise PolicyYamlError(
            f"version must be the integer 1 (got {value!r}, "
            f"type {type(value).__name__})"
        )
    if value != _SUPPORTED_VERSION:
        raise PolicyYamlError(
            f"version must be {_SUPPORTED_VERSION} (got {value}); "
            f"this parser only supports v1 policy files"
        )


def _optional_str(data: dict[str, Any], field: str, *, default: str) -> str:
    if field not in data:
        return default
    value = data[field]
    if not isinstance(value, str):
        raise PolicyYamlError(
            f"{field} must be a string (got {type(value).__name__}: {value!r})"
        )
    return value


def _parse_block(raw: Any) -> frozenset[PIICategory]:
    # Reject None / non-list to surface the wrong-shape error here
    # rather than letting the dataclass post-init produce a less
    # clear "block contains unknown" wrap.
    if not isinstance(raw, list):
        raise PolicyYamlError(
            f"block must be a list of category names (got {type(raw).__name__}: {raw!r})"
        )
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise PolicyYamlError(
                f"block items must be strings (got {type(item).__name__}: {item!r})"
            )
        if item in seen:
            raise PolicyYamlError(f"block contains duplicate category: {item!r}")
        seen.add(item)
    # Dataclass __post_init__ will reject any string that isn't a
    # valid PIICategory; we trust that surface and forward.
    return cast(frozenset[PIICategory], frozenset(seen))


def _parse_column_overrides(raw: Any) -> tuple[ColumnOverride, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise PolicyYamlError(
            f"column_overrides must be a mapping (got {type(raw).__name__}: {raw!r})"
        )
    overrides: list[ColumnOverride] = []
    for qualified_column, body in raw.items():
        if not isinstance(qualified_column, str):
            raise PolicyYamlError(
                f"column_overrides keys must be strings (got "
                f"{type(qualified_column).__name__}: {qualified_column!r})"
            )
        if not isinstance(body, dict):
            raise PolicyYamlError(
                f"column_overrides[{qualified_column!r}] must be a mapping "
                f"(got {type(body).__name__}: {body!r})"
            )
        _check_keys(
            body,
            allowed=_ALLOWED_OVERRIDE_KEYS,
            required=_REQUIRED_OVERRIDE_KEYS,
            scope=f"column_overrides[{qualified_column!r}]",
        )
        sensitivity_raw = body["sensitivity"]
        if not isinstance(sensitivity_raw, str):
            raise PolicyYamlError(
                f"column_overrides[{qualified_column!r}].sensitivity must be "
                f"a string (got {type(sensitivity_raw).__name__}: "
                f"{sensitivity_raw!r})"
            )
        if sensitivity_raw not in SENSITIVITIES:
            raise PolicyYamlError(
                f"column_overrides[{qualified_column!r}].sensitivity must be "
                f"one of {list(SENSITIVITIES)} (got {sensitivity_raw!r})"
            )

        categories_raw = body.get("categories", [])
        if not isinstance(categories_raw, list):
            raise PolicyYamlError(
                f"column_overrides[{qualified_column!r}].categories must be a "
                f"list (got {type(categories_raw).__name__}: {categories_raw!r})"
            )
        category_set: set[str] = set()
        for cat in categories_raw:
            if not isinstance(cat, str):
                raise PolicyYamlError(
                    f"column_overrides[{qualified_column!r}].categories items "
                    f"must be strings (got {type(cat).__name__}: {cat!r})"
                )
            if cat in category_set:
                raise PolicyYamlError(
                    f"column_overrides[{qualified_column!r}].categories "
                    f"contains duplicate: {cat!r}"
                )
            category_set.add(cat)

        try:
            overrides.append(
                ColumnOverride(
                    qualified_column=qualified_column,
                    sensitivity=cast(Sensitivity, sensitivity_raw),
                    categories=cast(frozenset[PIICategory], frozenset(category_set)),
                )
            )
        except ValueError as exc:
            raise PolicyYamlError(
                f"column_overrides[{qualified_column!r}]: {exc}"
            ) from exc

    return tuple(overrides)
