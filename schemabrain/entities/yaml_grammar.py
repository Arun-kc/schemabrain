"""YAML grammar parser for v1 entity files.

The grammar is a flat mapping with a small fixed schema:

    version: 1                            # required, int, must equal 1
    name: customer                        # required, identifier-shaped
    description: "..."                    # optional, defaults to ""
    binding:
      single_table: public.customers      # required; only single_table at v1
    identity: id                          # required, identifier-shaped
    origin: manual                        # optional, defaults "manual"

Parse errors raise `EntityParseError` (a `ValueError` subclass) with a
message that names the offending field and shows the value when
relevant. The CLI (`schemabrain entities apply <path>`) prints the
message verbatim and exits 1.

Strict-keys policy: unknown top-level keys are rejected at parse time
so a typo like `bindings:` (note plural) doesn't silently become a
no-op. Same policy applies inside `binding`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from schemabrain.core.entity import (
    _VALID_ORIGINS,
    Entity,
    Origin,
    SingleTableBinding,
)

# Closed sets used for strict-keys validation. Drift here without a
# schema/version bump silently changes the parser surface, so the
# tuples are the single source of truth.
_ALLOWED_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {"version", "name", "description", "binding", "identity", "origin"}
)
_REQUIRED_TOP_LEVEL_KEYS: frozenset[str] = frozenset({"version", "name", "binding", "identity"})

# Binding shapes the parser *recognises*. `multi_table` is recognised
# so its presence triggers a "deferred to v2" error rather than the
# generic "unknown binding shape" message. A future contributor who
# renames this set to anything implying "accepted" must remember
# that `multi_table` is still rejected with a specific message —
# the rejection path is intentional, not an oversight.
_KNOWN_BINDING_KEYS: frozenset[str] = frozenset({"single_table", "multi_table"})

_SUPPORTED_VERSION: int = 1


class EntityParseError(ValueError):
    """Raised when an entity YAML document fails to parse or validate.

    Subclass of `ValueError` so callers that just want to catch "bad
    input" can do so without importing this module. The CLI catches
    the specific type to keep its exit-code contract narrow.
    """


def parse_entity_yaml(text: str) -> Entity:
    """Parse `text` (raw YAML) into an `Entity`.

    Raises `EntityParseError` on any structural, schema, or value
    violation. The error message is user-facing — keep it short and
    name the field that's wrong.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise EntityParseError(f"failed to parse YAML: {exc}") from exc

    if data is None:
        raise EntityParseError("entity YAML is empty")
    if not isinstance(data, dict):
        raise EntityParseError(
            f"entity YAML must be a mapping at the top level (got {type(data).__name__})"
        )

    _check_keys(data, allowed=_ALLOWED_TOP_LEVEL_KEYS, required=_REQUIRED_TOP_LEVEL_KEYS)
    _check_version(data["version"])

    name = _require_str(data, "name")
    description = _optional_str(data, "description", default="")
    binding = _parse_binding(data["binding"])
    identity = _require_str(data, "identity")
    origin = _parse_origin(data.get("origin", "manual"))

    # Entity's __post_init__ runs identifier-shape + origin-enum checks.
    # Re-wrap any failure as EntityParseError so the CLI surface is
    # uniform; this also catches any field that slipped past us. The
    # prefix distinguishes "entity validation failed" from mid-parse
    # errors so a user reading the CLI message can tell where the
    # check fired.
    try:
        return Entity(
            name=name,
            description=description,
            binding=binding,
            identity=identity,
            origin=origin,
        )
    except ValueError as exc:
        raise EntityParseError(f"entity validation failed: {exc}") from exc


def entity_to_yaml(entity: Entity) -> str:
    """Serialise an `Entity` into the v1 entity YAML body.

    Inverse of `parse_entity_yaml`: any `Entity` that round-trips
    through this function then `parse_entity_yaml` is value-equal to
    the input (modulo the two store-derived fields below).

    Trust-signal fields (`inference_method`, `validation_state`) are
    deliberately NOT emitted. The grammar parser does not accept them
    — they are store-side derived at apply time (defaulting to
    `manually_authored` + `applied` for hand-authored YAML, overridden
    by the LLM-suggest writer and the dbt importer). Round-tripping
    an LLM-suggested entity through export → edit → apply correctly
    resets the trust signal to manually-authored, because the operator
    DID author the edit.

    `description` is omitted when empty so the YAML body stays compact
    for hand-edited or minimal entities — the parser fills the field
    with the empty string by default.

    Uses `yaml.safe_dump` for scalar values so a description containing
    YAML-special characters (colons, hashes, indented lines) is
    properly quoted/escaped. Hand-rolled string concatenation would
    open a YAML-injection path where a malicious description fragments
    the document on re-parse. The returned body is rstrip'd (no
    trailing newline) so callers compose their own line endings —
    matches the convention `_format_entity_yaml_body` already uses.
    """
    body: dict[str, object] = {
        "version": _SUPPORTED_VERSION,
        "name": entity.name,
    }
    if entity.description:
        body["description"] = entity.description
    body["binding"] = {"single_table": entity.qualified_table}
    body["identity"] = entity.identity
    body["origin"] = entity.origin
    return yaml.safe_dump(
        body,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).rstrip()


def parse_entity_yaml_file(path: Path) -> Entity:
    """Read `path` and parse its contents as an entity YAML document.

    `FileNotFoundError` / `IsADirectoryError` propagate verbatim so
    the caller can distinguish "missing file" from "malformed YAML."
    `PermissionError` and `UnicodeDecodeError` (from a non-UTF-8 file
    masquerading as YAML) are wrapped as `EntityParseError` so the
    CLI surface stays uniform — both are user-fixable conditions,
    not "missing file".
    Everything else surfaces as `EntityParseError`.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError):
        raise
    except PermissionError as exc:
        raise EntityParseError(
            f"cannot read {path}: permission denied ({exc.strerror or exc})"
        ) from exc
    except UnicodeDecodeError as exc:
        raise EntityParseError(
            f"{path} is not a valid UTF-8 text file (entity YAML must be UTF-8): {exc.reason}"
        ) from exc
    return parse_entity_yaml(text)


# ----- field validators -------------------------------------------------------


def _check_keys(
    data: dict[str, Any],
    *,
    allowed: frozenset[str],
    required: frozenset[str],
) -> None:
    keys = set(data.keys())
    missing = required - keys
    if missing:
        raise EntityParseError(f"missing required field(s): {sorted(missing)}")
    unknown = keys - allowed
    if unknown:
        raise EntityParseError(f"unknown top-level keys: {sorted(unknown)}")


def _check_version(value: Any) -> None:
    # YAML's `version: 1` parses as int. We reject the string form
    # `version: "1"` and the float form `version: 1.0` strictly —
    # both indicate a hand-typed file that drifted from the grammar,
    # and accepting them now would create ambiguity at v2 when
    # versioned dispatch arrives.
    if not isinstance(value, int) or isinstance(value, bool):
        raise EntityParseError(
            f"version must be the integer 1 (got {value!r}, type {type(value).__name__})"
        )
    if value != _SUPPORTED_VERSION:
        raise EntityParseError(
            f"version must be {_SUPPORTED_VERSION} (got {value}); "
            f"this parser only supports v1 entity files"
        )


def _require_str(data: dict[str, Any], field: str) -> str:
    # Caller (`parse_entity_yaml`) runs `_check_keys` first, which
    # raises on any missing required field — so this helper is only
    # invoked for fields known to be present. Rejecting empty
    # strings here gives a clearer error than the downstream
    # Entity validation would.
    value = data[field]
    if not isinstance(value, str):
        raise EntityParseError(f"{field} must be a string (got {type(value).__name__}: {value!r})")
    if not value:
        raise EntityParseError(f"{field} must be non-empty")
    return value


def _optional_str(data: dict[str, Any], field: str, *, default: str) -> str:
    if field not in data:
        return default
    value = data[field]
    if not isinstance(value, str):
        raise EntityParseError(f"{field} must be a string (got {type(value).__name__}: {value!r})")
    return value


def _parse_binding(raw: Any) -> SingleTableBinding:
    if not isinstance(raw, dict):
        raise EntityParseError(f"binding must be a mapping (got {type(raw).__name__})")
    if not raw:
        raise EntityParseError("binding must specify exactly one shape; got empty mapping")

    keys = set(raw.keys())
    unknown = keys - _KNOWN_BINDING_KEYS
    if unknown:
        raise EntityParseError(
            f"unknown binding shape(s): {sorted(unknown)}; allowed: {sorted(_KNOWN_BINDING_KEYS)}"
        )
    if "multi_table" in keys and "single_table" in keys:
        raise EntityParseError(
            "binding must specify exactly one shape; got both single_table and multi_table"
        )
    if "multi_table" in keys:
        # multi_table is a v2 concern. The contributor error message
        # points at the deferred-to-v2 hint explicitly so users know
        # the rejection is intentional, not missing.
        raise EntityParseError(
            "multi_table binding is deferred to v2; use single_table for v1 entities"
        )

    # At this point keys ⊆ {"single_table", "multi_table"}, multi_table
    # is absent, and the dict is non-empty — so single_table must be
    # the sole remaining key.
    qualified_table = raw["single_table"]
    if not isinstance(qualified_table, str):
        raise EntityParseError(
            f"binding.single_table must be a string "
            f"(got {type(qualified_table).__name__}: {qualified_table!r})"
        )

    try:
        return SingleTableBinding(qualified_table=qualified_table)
    except ValueError as exc:
        raise EntityParseError(f"binding.single_table: {exc}") from exc


def _parse_origin(raw: Any) -> Origin:
    # Sources from `_VALID_ORIGINS` (set in `core.entity`) so the
    # parser stays in lock-step with the Literal — adding a new
    # `Origin` value won't require touching this function.
    if not isinstance(raw, str):
        raise EntityParseError(f"origin must be a string (got {type(raw).__name__}: {raw!r})")
    if raw not in _VALID_ORIGINS:
        raise EntityParseError(f"origin must be one of {sorted(_VALID_ORIGINS)} (got {raw!r})")
    # The Literal narrowing happens via the runtime check above; mypy
    # sees a `str`, but the value is provably one of the three.
    return raw  # type: ignore[return-value]
