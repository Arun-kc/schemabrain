"""YAML grammar parser for v1 canonical-join files.

The grammar is a flat mapping with a small fixed schema:

    version: 1                            # required, int, must equal 1
    name: order_billing_address           # required, identifier-shaped
    description: "..."                    # optional, defaults to ""
    source_entity: order                  # required, identifier-shaped
    target_entity: address                # required, identifier-shaped
    on:                                   # required, non-empty list
      - source: billing_address_id        # required identifier on each item
        target: id                        # required identifier on each item
    origin: manual                        # optional, defaults "manual"

Parse errors raise `CanonicalJoinParseError` (a `ValueError` subclass)
with a message that names the offending field and shows the value when
relevant. The CLI (`schemabrain joins apply <path>`) prints the message
verbatim and exits 1.

Strict-keys policy: unknown top-level keys are rejected at parse time
so a typo like `from_entity:` doesn't silently become a no-op. Same
policy applies inside each `on` item.

Mirrors `entities/yaml_grammar.py` in style — version field required,
identifier validation deferred to dataclass `__post_init__`, errors
wrap with field context.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from schemabrain.core.join import (
    _VALID_CARDINALITIES,
    _VALID_ORIGINS,
    CanonicalJoin,
    Cardinality,
    JoinColumnPair,
    JoinOrigin,
)


class _Yaml12SafeLoader(yaml.SafeLoader):
    """SafeLoader with YAML 1.2 boolean semantics.

    PyYAML defaults to YAML 1.1, which coerces `on`, `off`, `yes`, and
    `no` (unquoted) to booleans. The canonical-join grammar's top-level
    `on:` key — the natural English word for "the JOIN ON predicate
    list" — would parse as `True:` under YAML 1.1, silently turning a
    valid file into "missing required field." YAML 1.2 dropped these
    coercions; we mirror that behavior in the loader so user YAMLs
    don't carry quoting tax for English-ish field names.
    """


# Strip the YAML 1.1 bool resolver tag, then re-add it with the YAML 1.2
# pattern (only `true`/`false`/`True`/`False`/`TRUE`/`FALSE`). The
# `yaml_implicit_resolvers` dict maps the first character of a scalar
# to the list of (tag, regex) candidates the loader tries — we filter
# bool out, then add it back with the narrower pattern. The mutation
# is scoped to the subclass so it doesn't affect the base SafeLoader
# (or other callers via `entities/yaml_grammar.py`).
_Yaml12SafeLoader.yaml_implicit_resolvers = {
    first_char: [(tag, regex) for tag, regex in resolvers if tag != "tag:yaml.org,2002:bool"]
    for first_char, resolvers in _Yaml12SafeLoader.yaml_implicit_resolvers.items()
}
_Yaml12SafeLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)

# Closed sets used for strict-keys validation. Drift here without a
# schema/version bump silently changes the parser surface, so the
# frozensets are the single source of truth.
_ALLOWED_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "version",
        "name",
        "description",
        "source_entity",
        "target_entity",
        "on",
        "origin",
        "cardinality",
    }
)
_REQUIRED_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {"version", "name", "source_entity", "target_entity", "on"}
)

# Per-on-item shape — strict keys so `from:` typos surface cleanly.
_ALLOWED_ON_KEYS: frozenset[str] = frozenset({"source", "target"})
_REQUIRED_ON_KEYS: frozenset[str] = frozenset({"source", "target"})

_SUPPORTED_VERSION: int = 1


class CanonicalJoinParseError(ValueError):
    """Raised when a canonical-join YAML document fails to parse or validate.

    Subclass of `ValueError` so callers that just want to catch "bad
    input" can do so without importing this module. The CLI catches
    the specific type to keep its exit-code contract narrow.
    """


def parse_canonical_join_yaml(text: str) -> CanonicalJoin:
    """Parse `text` (raw YAML) into a `CanonicalJoin`.

    Raises `CanonicalJoinParseError` on any structural, schema, or value
    violation. The error message is user-facing — keep it short and
    name the field that's wrong.
    """
    try:
        # _Yaml12SafeLoader is a SafeLoader subclass that disables YAML
        # 1.1 boolean coercion (`on`, `off`, `yes`, `no`) so the
        # natural English `on:` key in canonical-join YAMLs parses as
        # the string "on" instead of True. Safe-by-construction —
        # `# nosec B506` suppresses bandit's "yaml.load() with custom
        # Loader is unsafe" rule which doesn't recognise SafeLoader
        # subclasses.
        data = yaml.load(text, Loader=_Yaml12SafeLoader)  # nosec B506
    except yaml.YAMLError as exc:
        raise CanonicalJoinParseError(f"failed to parse YAML: {exc}") from exc

    if data is None:
        raise CanonicalJoinParseError("canonical-join YAML is empty")
    if not isinstance(data, dict):
        raise CanonicalJoinParseError(
            f"canonical-join YAML must be a mapping at the top level (got {type(data).__name__})"
        )

    _check_keys(
        data,
        allowed=_ALLOWED_TOP_LEVEL_KEYS,
        required=_REQUIRED_TOP_LEVEL_KEYS,
        context="top-level",
    )
    _check_version(data["version"])

    name = _require_str(data, "name")
    description = _optional_str(data, "description", default="")
    source_entity = _require_str(data, "source_entity")
    target_entity = _require_str(data, "target_entity")
    on = _parse_on_list(data["on"])
    origin = _parse_origin(data.get("origin", "manual"))
    cardinality = _parse_cardinality(data.get("cardinality"))

    # The dataclass `__post_init__` re-runs identifier-shape +
    # self-join + empty-on + origin-enum checks. Re-wrap any failure
    # as `CanonicalJoinParseError` so the CLI surface is uniform; this
    # catches anything that slipped past us. The prefix distinguishes
    # "join validation failed" from mid-parse errors so a user reading
    # the CLI message can tell where the check fired.
    try:
        return CanonicalJoin(
            name=name,
            description=description,
            source_entity=source_entity,
            target_entity=target_entity,
            on=on,
            origin=origin,
            cardinality=cardinality,
        )
    except ValueError as exc:
        raise CanonicalJoinParseError(f"canonical-join validation failed: {exc}") from exc


def parse_canonical_join_yaml_file(path: Path) -> CanonicalJoin:
    """Read `path` and parse its contents as a canonical-join YAML document.

    `FileNotFoundError` / `IsADirectoryError` propagate verbatim so
    the caller can distinguish "missing file" from "malformed YAML."
    `PermissionError` and `UnicodeDecodeError` (from a non-UTF-8 file
    masquerading as YAML) are wrapped as `CanonicalJoinParseError` so
    the CLI surface stays uniform — both are user-fixable conditions,
    not "missing file".
    Everything else surfaces as `CanonicalJoinParseError`.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError):
        raise
    except PermissionError as exc:  # pragma: no cover — POSIX file-mode test would require fragile setup; same posture as entities/yaml_grammar.py
        raise CanonicalJoinParseError(
            f"cannot read {path}: permission denied ({exc.strerror or exc})"
        ) from exc
    except UnicodeDecodeError as exc:
        raise CanonicalJoinParseError(
            f"{path} is not a valid UTF-8 text file "
            f"(canonical-join YAML must be UTF-8): {exc.reason}"
        ) from exc
    return parse_canonical_join_yaml(text)


# ----- field validators -------------------------------------------------------


def _check_keys(
    data: dict[str, Any],
    *,
    allowed: frozenset[str],
    required: frozenset[str],
    context: str,
) -> None:
    keys = set(data.keys())
    missing = required - keys
    if missing:
        raise CanonicalJoinParseError(f"missing required field(s) in {context}: {sorted(missing)}")
    unknown = keys - allowed
    if unknown:
        raise CanonicalJoinParseError(f"unknown {context} keys: {sorted(unknown)}")


def _check_version(value: Any) -> None:
    # YAML's `version: 1` parses as int. We reject the string form
    # `version: "1"` and the float form `version: 1.0` strictly —
    # both indicate a hand-typed file that drifted from the grammar,
    # and accepting them now would create ambiguity at v2 when
    # versioned dispatch arrives. Mirrors the entity grammar's
    # version policy verbatim.
    if not isinstance(value, int) or isinstance(value, bool):
        raise CanonicalJoinParseError(
            f"version must be the integer 1 (got {value!r}, type {type(value).__name__})"
        )
    if value != _SUPPORTED_VERSION:
        raise CanonicalJoinParseError(
            f"version must be {_SUPPORTED_VERSION} (got {value}); "
            f"this parser only supports v1 canonical-join files"
        )


def _require_str(data: dict[str, Any], field: str) -> str:
    value = data[field]
    if not isinstance(value, str):
        raise CanonicalJoinParseError(
            f"{field} must be a string (got {type(value).__name__}: {value!r})"
        )
    if not value:
        raise CanonicalJoinParseError(f"{field} must be non-empty")
    return value


def _optional_str(data: dict[str, Any], field: str, *, default: str) -> str:
    if field not in data:
        return default
    value = data[field]
    if not isinstance(value, str):
        raise CanonicalJoinParseError(
            f"{field} must be a string (got {type(value).__name__}: {value!r})"
        )
    return value


def _parse_on_list(raw: Any) -> tuple[JoinColumnPair, ...]:
    if not isinstance(raw, list):
        raise CanonicalJoinParseError(
            f"on must be a list of {{source, target}} mappings (got {type(raw).__name__})"
        )
    if not raw:
        # The dataclass would catch this too, but a YAML-context error
        # is more actionable than the generic dataclass message.
        raise CanonicalJoinParseError(
            "on must contain at least one column pair (cross joins are not canonical)"
        )
    pairs: list[JoinColumnPair] = []
    for index, item in enumerate(raw):
        pair = _parse_on_item(item, index=index)
        pairs.append(pair)
    return tuple(pairs)


def _parse_on_item(item: Any, *, index: int) -> JoinColumnPair:
    if not isinstance(item, dict):
        raise CanonicalJoinParseError(
            f"on[{index}] must be a mapping with `source` and `target` keys "
            f"(got {type(item).__name__})"
        )
    _check_keys(
        item,
        allowed=_ALLOWED_ON_KEYS,
        required=_REQUIRED_ON_KEYS,
        context=f"on[{index}]",
    )
    source = item["source"]
    target = item["target"]
    if not isinstance(source, str) or not source:
        raise CanonicalJoinParseError(
            f"on[{index}].source must be a non-empty string "
            f"(got {type(source).__name__}: {source!r})"
        )
    if not isinstance(target, str) or not target:
        raise CanonicalJoinParseError(
            f"on[{index}].target must be a non-empty string "
            f"(got {type(target).__name__}: {target!r})"
        )
    try:
        return JoinColumnPair(source_column=source, target_column=target)
    except ValueError as exc:
        raise CanonicalJoinParseError(f"on[{index}]: {exc}") from exc


def _parse_origin(raw: Any) -> JoinOrigin:
    # Sources from `_VALID_ORIGINS` (set in `core.join`) so the parser
    # stays in lock-step with the Literal — adding a new origin value
    # won't require touching this function.
    if not isinstance(raw, str):
        raise CanonicalJoinParseError(
            f"origin must be a string (got {type(raw).__name__}: {raw!r})"
        )
    if raw not in _VALID_ORIGINS:
        raise CanonicalJoinParseError(
            f"origin must be one of {sorted(_VALID_ORIGINS)} (got {raw!r})"
        )
    # `dbt_import` is reserved in the dataclass + SQL CHECK for the
    # eventual dbt-relationships importer, but no producer exists yet
    # at this release. Refusing here keeps the YAML surface consistent
    # with the documented "rejected with 'not yet implemented'"
    # contract — otherwise the CLI's `dataclasses.replace(origin=
    # "manual")` would silently re-label and the user would see no
    # error, contradicting the design intent.
    if raw == "dbt_import":
        raise CanonicalJoinParseError(
            "origin 'dbt_import' is reserved for the dbt-relationships "
            "importer, which has not shipped yet. Use 'manual' or omit "
            "the field to default. To import entities from dbt today, "
            "see `schemabrain import dbt`; the joins counterpart is on "
            "the roadmap."
        )
    # The Literal narrowing happens via the runtime check above; mypy
    # sees a `str`, but the value is provably one of the three.
    return raw  # type: ignore[return-value]


def _parse_cardinality(raw: Any) -> Cardinality | None:
    # Optional field. Absent (None on the YAML mapping) → None on the
    # dataclass — the back-compat shape for joins authored before
    # cardinality landed. Anything present must be a string in the
    # closed Literal set.
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise CanonicalJoinParseError(
            f"cardinality must be a string (got {type(raw).__name__}: {raw!r})"
        )
    if raw not in _VALID_CARDINALITIES:
        raise CanonicalJoinParseError(
            f"cardinality must be one of {sorted(_VALID_CARDINALITIES)} (got {raw!r})"
        )
    # Literal narrowing via runtime check.
    return raw  # type: ignore[return-value]
