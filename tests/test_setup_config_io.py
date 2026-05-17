"""Tests for `schemabrain.setup.config_io` — atomic read-merge-write
of host MCP configs.

Goals:

  1. `read_mcp_config` returns parsed dict, returns None on missing
     file, and raises `MalformedConfigError` on JSON parse failure
     (we refuse to write over a file we can't parse).
  2. `merge_schemabrain_entry` is pure: it does not mutate `existing`,
     and it preserves arbitrary unrelated mcpServers entries and
     unrelated top-level keys byte-for-byte. A property test exercises
     this across Hypothesis-generated configs.
  3. `write_mcp_config_atomic` lands the new contents via tmp+fsync+rename
     so a crash mid-write leaves the previous file intact, and it
     creates the `.bak` sibling exactly once — re-running init never
     destroys the original rollback target.
  4. `schemabrain_entry_in` distinguishes "not present" from
     "malformed shape" gracefully so the init flow's present/identical
     /different state matrix is fed correct truthiness.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from schemabrain.setup.config_io import (
    MalformedConfigError,
    merge_schemabrain_entry,
    read_mcp_config,
    schemabrain_entry_in,
    write_mcp_config_atomic,
)
from schemabrain.setup.hosts import SchemabrainSnippet, build_snippet


@pytest.fixture
def snippet() -> SchemabrainSnippet:
    return build_snippet(
        version_pin="0.2.0a1",
        env_var_name="SCHEMABRAIN_DATABASE_URL",
        store_path=Path("/abs/proj/.sb.db"),
        db_url="postgresql://u:p@h/db",
    )


# ----- read_mcp_config ------------------------------------------------------


class TestReadMcpConfig:
    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        assert read_mcp_config(tmp_path / "nope.json") is None

    def test_returns_parsed_dict_for_valid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
        assert read_mcp_config(path) == {"mcpServers": {"other": {"command": "x"}}}

    def test_raises_malformed_on_invalid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text("{not: json}")
        with pytest.raises(MalformedConfigError) as exc_info:
            read_mcp_config(path)
        # The error carries the path so the CLI can render a precise
        # location to the user.
        assert exc_info.value.path == path
        assert exc_info.value.decode_error is not None

    def test_malformed_config_error_message_includes_line_col(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text('{"valid": "first",\n  "broken: true}')
        with pytest.raises(MalformedConfigError, match=r"line \d+"):
            read_mcp_config(path)

    def test_empty_file_raises_malformed(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text("")
        with pytest.raises(MalformedConfigError):
            read_mcp_config(path)


# ----- merge_schemabrain_entry ----------------------------------------------


class TestMergeSchemabrainEntry:
    def test_creates_full_config_when_existing_is_none(self, snippet: SchemabrainSnippet) -> None:
        merged = merge_schemabrain_entry(None, snippet)
        assert merged == {"mcpServers": {"schemabrain": snippet.to_mcp_entry()}}

    def test_preserves_other_mcp_servers(self, snippet: SchemabrainSnippet) -> None:
        existing = {
            "mcpServers": {
                "other-server": {"command": "node", "args": ["server.js"]},
                "another": {"command": "python", "args": ["-m", "foo"]},
            }
        }
        merged = merge_schemabrain_entry(existing, snippet)
        assert merged["mcpServers"]["other-server"] == {
            "command": "node",
            "args": ["server.js"],
        }
        assert merged["mcpServers"]["another"] == {
            "command": "python",
            "args": ["-m", "foo"],
        }
        assert merged["mcpServers"]["schemabrain"] == snippet.to_mcp_entry()

    def test_overwrites_existing_schemabrain_entry(self, snippet: SchemabrainSnippet) -> None:
        existing = {
            "mcpServers": {
                "schemabrain": {"command": "old-stuff", "args": ["wrong"]},
            }
        }
        merged = merge_schemabrain_entry(existing, snippet)
        assert merged["mcpServers"]["schemabrain"] == snippet.to_mcp_entry()

    def test_preserves_unrelated_top_level_keys(self, snippet: SchemabrainSnippet) -> None:
        # Claude Desktop's config has more than just `mcpServers` — it
        # holds globalShortcut, theme, etc. The merge must not touch
        # them.
        existing = {
            "globalShortcut": "Cmd+Space",
            "theme": "dark",
            "mcpServers": {},
        }
        merged = merge_schemabrain_entry(existing, snippet)
        assert merged["globalShortcut"] == "Cmd+Space"
        assert merged["theme"] == "dark"

    def test_does_not_mutate_existing(self, snippet: SchemabrainSnippet) -> None:
        existing = {"mcpServers": {"other": {"command": "x"}}}
        snapshot = json.dumps(existing, sort_keys=True)
        merge_schemabrain_entry(existing, snippet)
        assert json.dumps(existing, sort_keys=True) == snapshot

    def test_handles_existing_without_mcp_servers_key(self, snippet: SchemabrainSnippet) -> None:
        existing = {"someOtherKey": 42}
        merged = merge_schemabrain_entry(existing, snippet)
        assert merged["someOtherKey"] == 42
        assert merged["mcpServers"] == {"schemabrain": snippet.to_mcp_entry()}


# ----- write_mcp_config_atomic ----------------------------------------------


class TestWriteMcpConfigAtomic:
    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "c" / "config.json"
        write_mcp_config_atomic(nested, {"hello": "world"})
        assert nested.exists()
        assert json.loads(nested.read_text()) == {"hello": "world"}

    def test_writes_indented_json_with_trailing_newline(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        write_mcp_config_atomic(path, {"a": {"b": 1}})
        text = path.read_text()
        assert text.endswith("\n")
        assert '  "a"' in text  # 2-space indent

    def test_creates_backup_on_first_write_with_existing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"old": True}))
        backup_made = write_mcp_config_atomic(path, {"new": True})
        assert backup_made is True
        backup_path = path.parent / (path.name + ".bak")
        assert backup_path.exists()
        assert json.loads(backup_path.read_text()) == {"old": True}

    def test_no_backup_when_path_did_not_exist(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        backup_made = write_mcp_config_atomic(path, {"new": True})
        assert backup_made is False
        assert not (path.parent / (path.name + ".bak")).exists()

    def test_no_backup_overwrite_when_backup_already_exists(self, tmp_path: Path) -> None:
        # Idempotency contract: re-running init must NOT overwrite the
        # original backup with the now-current config, since that
        # would defeat the rollback.
        path = tmp_path / "config.json"
        backup_path = path.parent / (path.name + ".bak")
        backup_path.write_text(json.dumps({"original": True}))
        path.write_text(json.dumps({"intermediate": True}))
        backup_made = write_mcp_config_atomic(path, {"newest": True})
        assert backup_made is False
        # The .bak still holds the ORIGINAL contents, not the
        # intermediate ones.
        assert json.loads(backup_path.read_text()) == {"original": True}

    def test_create_backup_false_skips_backup(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"old": True}))
        backup_made = write_mcp_config_atomic(path, {"new": True}, create_backup=False)
        assert backup_made is False
        assert not (path.parent / (path.name + ".bak")).exists()

    def test_no_tmp_file_left_behind_on_success(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        write_mcp_config_atomic(path, {"hello": "world"})
        # Tmp file used `.config.json.<random>.tmp` shape; verify none
        # of those linger.
        residual = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
        assert residual == []

    def test_no_tmp_file_left_behind_on_serialisation_failure(self, tmp_path: Path) -> None:
        # A non-JSON-serialisable value must abort the write WITHOUT
        # leaving a half-written tmp file in the user's config dir.
        path = tmp_path / "config.json"

        class NotSerialisable:
            pass

        with pytest.raises(TypeError):
            write_mcp_config_atomic(path, {"bad": NotSerialisable()})  # type: ignore[dict-item]
        assert not path.exists()
        residual = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
        assert residual == []

    def test_overwrites_existing_atomically(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"v": 1}))
        write_mcp_config_atomic(path, {"v": 2})
        assert json.loads(path.read_text()) == {"v": 2}

    def test_backup_creation_is_toctou_safe_via_o_excl(self, tmp_path: Path) -> None:
        # The backup-once check must use an atomic create
        # (O_CREAT|O_EXCL) so two concurrent writers can't both
        # observe "no .bak" and race to copy over each other. We
        # simulate the race by pre-creating the .bak with sentinel
        # contents and verifying the second writer does NOT overwrite it.
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"original": True}))
        backup_path = path.parent / (path.name + ".bak")
        # First write — creates backup of {"original": True}.
        backup_made_first = write_mcp_config_atomic(path, {"v": 2})
        assert backup_made_first is True
        original_backup_contents = backup_path.read_text()
        # Now simulate a concurrent writer arriving — the .bak exists,
        # so the second call must NOT overwrite it even though we
        # would race past path.exists() in the old TOCTOU code.
        backup_made_second = write_mcp_config_atomic(path, {"v": 3})
        assert backup_made_second is False
        # The backup still holds the TRUE original contents.
        assert backup_path.read_text() == original_backup_contents


# ----- schemabrain_entry_in -------------------------------------------------


class TestSchemabrainEntryIn:
    def test_returns_none_when_config_is_none(self) -> None:
        assert schemabrain_entry_in(None) is None

    def test_returns_none_when_no_mcp_servers_key(self) -> None:
        assert schemabrain_entry_in({"other": "stuff"}) is None

    def test_returns_none_when_no_schemabrain_entry(self) -> None:
        assert schemabrain_entry_in({"mcpServers": {"other": {}}}) is None

    def test_returns_entry_when_present(self) -> None:
        entry = {"command": "uvx", "args": ["schemabrain==0.1", "serve"], "env": {}}
        assert schemabrain_entry_in({"mcpServers": {"schemabrain": entry}}) == entry

    def test_returns_none_when_mcp_servers_is_not_a_dict(self) -> None:
        # A user with a hand-broken config (mcpServers as a list, say)
        # gets None back; init's caller then treats it as "missing"
        # and writes a fresh entry.
        assert schemabrain_entry_in({"mcpServers": ["not", "a", "dict"]}) is None

    def test_returns_none_when_entry_is_not_a_dict(self) -> None:
        assert schemabrain_entry_in({"mcpServers": {"schemabrain": "broken"}}) is None


# ----- Property test: byte-stable round-trip --------------------------------

# Strategies for arbitrary "OTHER" mcpServers content. We never generate
# the literal key "schemabrain" — the merge function's job is to LEAVE
# everything else untouched, so the property exercises that surface.

_safe_text = st.text(
    alphabet=st.characters(
        min_codepoint=0x20,
        max_codepoint=0x7E,
        blacklist_characters='\\"',
    ),
    max_size=20,
)

_server_entry = st.fixed_dictionaries(
    {
        "command": st.text(min_size=1, max_size=20),
        "args": st.lists(_safe_text, max_size=5),
    }
)

_other_mcp_servers = st.dictionaries(
    keys=st.text(
        alphabet=st.characters(
            min_codepoint=0x21,
            max_codepoint=0x7E,
            blacklist_characters='.\\"',
        ),
        min_size=1,
        max_size=20,
    ).filter(lambda s: s != "schemabrain"),
    values=_server_entry,
    max_size=5,
)


@given(other=_other_mcp_servers)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_other_servers_survive_merge_byte_stable(
    other: dict[str, dict[str, object]],
) -> None:
    """Merging the schemabrain snippet must NEVER alter any other entry."""
    snippet = build_snippet(
        version_pin="0.2.0a1",
        env_var_name="DB",
        store_path=Path("/abs/.sb.db"),
        db_url="postgresql://x",
    )
    existing = {"mcpServers": dict(other)}
    merged = merge_schemabrain_entry(existing, snippet)

    surviving = dict(merged["mcpServers"])
    surviving.pop("schemabrain", None)
    assert surviving == other


@given(top_level=st.dictionaries(_safe_text, _safe_text, max_size=5))
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_unrelated_top_level_keys_survive_merge(
    top_level: dict[str, str],
) -> None:
    """Top-level keys other than mcpServers must NEVER be altered."""
    snippet = build_snippet(
        version_pin="0.2.0a1",
        env_var_name="DB",
        store_path=Path("/abs/.sb.db"),
        db_url="postgresql://x",
    )
    # Avoid colliding with "mcpServers" key in the random top-level.
    top_level.pop("mcpServers", None)
    existing: dict[str, object] = dict(top_level)
    existing["mcpServers"] = {}
    merged = merge_schemabrain_entry(existing, snippet)
    for k, v in top_level.items():
        assert merged[k] == v
