"""CLI tests for `schemabrain entities suggest`.

The suggest CLI orchestrates:
  store schema -> pipeline -> three output modes (dry-run / out-dir / apply)
  plus a cost-ceiling guard and ANTHROPIC_API_KEY check.

Tests inject a `FakeLLMClient` via a `--provider stub` flag so the
CLI surface stays exercised without an API key. The production
default (constructing `AnthropicClient` from `ANTHROPIC_API_KEY`) is
covered by checking the error message when the env var is missing —
no real network call is ever made in CI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from schemabrain.cli import _make_source_id, main
from schemabrain.core.models import Column, ForeignKey, Table
from schemabrain.core.store import SQLiteStore

_TEST_URL = "postgresql+psycopg://user:pw@localhost:5432/db"


def _users_table() -> Table:
    return Table(
        name="users",
        schema_name="public",
        columns=(
            Column(
                name="id",
                table_name="users",
                schema_name="public",
                data_type="bigint",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
            Column(
                name="email",
                table_name="users",
                schema_name="public",
                data_type="text",
                nullable=False,
                ordinal_position=2,
            ),
        ),
    )


def _orders_table() -> Table:
    return Table(
        name="orders",
        schema_name="public",
        columns=(
            Column(
                name="id",
                table_name="orders",
                schema_name="public",
                data_type="bigint",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
            Column(
                name="user_id",
                table_name="orders",
                schema_name="public",
                data_type="bigint",
                nullable=False,
                ordinal_position=2,
            ),
        ),
        foreign_keys=(
            ForeignKey(
                name="orders_user_id_fkey",
                source_columns=("user_id",),
                target_schema="public",
                target_table="users",
                target_columns=("id",),
            ),
        ),
    )


def _seed_store(store_path: Path) -> None:
    source_id = _make_source_id(_TEST_URL)
    with SQLiteStore(store_path) as store:
        store.write_table(_users_table(), source_connection_id=source_id)
        store.write_table(_orders_table(), source_connection_id=source_id)


_TWO_CANDIDATES = """\
candidates:
  - name: customer
    description: A registered customer
    binding:
      single_table: public.users
    identity: id
    confidence: high
    rationale: users has id PK and NOT NULL email
    pii_hints:
      email: pii
  - name: order
    description: One placed order
    binding:
      single_table: public.orders
    identity: id
    confidence: medium
    rationale: orders has id PK and FK into users
    pii_hints: {}
"""


@pytest.fixture
def stub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the stub-canned-response env var that `--provider stub` reads.

    The stub provider returns whatever YAML is in
    `SCHEMABRAIN_STUB_RESPONSE`. Keeping the canned response out of
    argv (where it'd be visible to `ps`) is consistent with how
    `--url-env` works.
    """
    monkeypatch.setenv("SCHEMABRAIN_STUB_RESPONSE", _TWO_CANDIDATES)
    # Make sure no real key leaks into the stub run.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


# ----- arg parsing -----------------------------------------------------------


class TestArgParsing:
    def test_missing_source_and_url_env_exits_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Same shape as `entities apply`: one of --source/--url-env
        # is required. This is a structural failure (exit 2).
        exit_code = main(
            [
                "entities",
                "suggest",
                "--store-path",
                str(tmp_path / "store.db"),
                "--dry-run",
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 2

    def test_missing_action_mode_exits_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], stub_env: None
    ) -> None:
        # One of --dry-run / --out-dir / --apply is required so the
        # user can't accidentally burn LLM spend without choosing what
        # to do with the output. argparse's required-mutex-group check
        # raises SystemExit before main() returns.
        _seed_store(tmp_path / "store.db")
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "entities",
                    "suggest",
                    "--source",
                    _TEST_URL,
                    "--store-path",
                    str(tmp_path / "store.db"),
                    "--provider",
                    "stub",
                ]
            )
        assert exc_info.value.code == 2

    def test_mutually_exclusive_modes_exits_two(self, tmp_path: Path, stub_env: None) -> None:
        # --dry-run and --apply are mutually exclusive: either preview
        # or commit, never both. argparse's mutex-violation check
        # raises SystemExit before main() returns.
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "entities",
                    "suggest",
                    "--source",
                    _TEST_URL,
                    "--store-path",
                    str(tmp_path / "store.db"),
                    "--dry-run",
                    "--apply",
                    "--provider",
                    "stub",
                ]
            )
        assert exc_info.value.code == 2


# ----- prerequisite checks ---------------------------------------------------


class TestPrerequisites:
    def test_missing_anthropic_key_exits_two(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Without --provider stub, the default Anthropic provider
        # needs an API key — fail fast and friendly.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        _seed_store(tmp_path / "store.db")
        exit_code = main(
            [
                "entities",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(tmp_path / "store.db"),
                "--dry-run",
            ]
        )
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "ANTHROPIC_API_KEY" in err

    def test_no_tables_in_store_exits_one(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        stub_env: None,
    ) -> None:
        # An empty / un-indexed store has nothing to analyse. Surface
        # a guided error pointing the user at `schemabrain index`
        # rather than calling the LLM with empty schema.
        store_path = tmp_path / "store.db"
        # Initialize an empty store (no tables).
        with SQLiteStore(store_path):
            pass

        exit_code = main(
            [
                "entities",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--dry-run",
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "index" in err.lower()


# ----- dry-run mode ----------------------------------------------------------


class TestDryRun:
    def test_dry_run_prints_candidates_to_stdout(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        stub_env: None,
    ) -> None:
        _seed_store(tmp_path / "store.db")
        exit_code = main(
            [
                "entities",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(tmp_path / "store.db"),
                "--dry-run",
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        # Both candidate names + the confidence/rationale envelope
        # fields appear in dry-run output.
        assert "customer" in out
        assert "order" in out
        assert "confidence" in out
        assert "rationale" in out

    def test_dry_run_does_not_write_to_store(
        self,
        tmp_path: Path,
        stub_env: None,
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store(store_path)
        main(
            [
                "entities",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--dry-run",
                "--provider",
                "stub",
            ]
        )
        # Store has no entity rows even though we ran the pipeline.
        with SQLiteStore(store_path) as store:
            entities = store.list_entities(source_connection_id=_make_source_id(_TEST_URL))
        assert entities == []


# ----- out-dir mode ----------------------------------------------------------


class TestOutDir:
    def test_out_dir_writes_one_yaml_per_candidate(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        stub_env: None,
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store(store_path)
        out_dir = tmp_path / "suggestions"
        exit_code = main(
            [
                "entities",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--out-dir",
                str(out_dir),
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 0
        # One file per candidate, named <entity_name>.yaml.
        assert (out_dir / "customer.yaml").is_file()
        assert (out_dir / "order.yaml").is_file()
        # And a sidecar metadata file with envelope fields.
        sidecar = out_dir / "_suggestion_metadata.json"
        assert sidecar.is_file()

    def test_out_dir_yaml_has_apply_ready_shape(
        self,
        tmp_path: Path,
        stub_env: None,
    ) -> None:
        # The per-candidate YAML must round-trip through
        # `parse_entity_yaml_file` — i.e., the suggest CLI emits files
        # the existing `entities apply` loader can ingest verbatim.
        from schemabrain.entities.yaml_grammar import parse_entity_yaml_file

        store_path = tmp_path / "store.db"
        _seed_store(store_path)
        out_dir = tmp_path / "suggestions"
        main(
            [
                "entities",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--out-dir",
                str(out_dir),
                "--provider",
                "stub",
            ]
        )
        entity = parse_entity_yaml_file(out_dir / "customer.yaml")
        assert entity.name == "customer"
        assert entity.qualified_table == "public.users"
        assert entity.origin == "suggested"

    def test_out_dir_refuses_to_overwrite_sidecar_only(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        stub_env: None,
    ) -> None:
        # Edge: candidate YAML doesn't exist but sidecar does. We
        # still refuse — sidecar overwrite would lose review-state
        # context tied to a previous run.
        store_path = tmp_path / "store.db"
        _seed_store(store_path)
        out_dir = tmp_path / "suggestions"
        out_dir.mkdir()
        sidecar = out_dir / "_suggestion_metadata.json"
        sidecar.write_text('{"prior": "metadata"}')

        exit_code = main(
            [
                "entities",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--out-dir",
                str(out_dir),
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 1
        # Sidecar untouched.
        assert sidecar.read_text() == '{"prior": "metadata"}'
        err = capsys.readouterr().err
        assert "_suggestion_metadata.json" in err

    def test_out_dir_refuses_to_overwrite_existing_files(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        stub_env: None,
    ) -> None:
        # A user who hand-edited a previous suggest run's YAML must not
        # lose those edits silently on a re-run. The conflict check
        # fires before any write — partial overwrite is impossible.
        store_path = tmp_path / "store.db"
        _seed_store(store_path)
        out_dir = tmp_path / "suggestions"
        out_dir.mkdir()
        # Pre-existing file collides with the LLM's first candidate.
        edited = out_dir / "customer.yaml"
        edited.write_text("user-edit-content")

        exit_code = main(
            [
                "entities",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--out-dir",
                str(out_dir),
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 1
        # User edit is intact.
        assert edited.read_text() == "user-edit-content"
        # And no sidecar was created from a partial write.
        assert not (out_dir / "_suggestion_metadata.json").exists()
        err = capsys.readouterr().err
        assert "customer.yaml" in err

    def test_out_dir_sidecar_carries_envelope_fields(
        self,
        tmp_path: Path,
        stub_env: None,
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store(store_path)
        out_dir = tmp_path / "suggestions"
        main(
            [
                "entities",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--out-dir",
                str(out_dir),
                "--provider",
                "stub",
            ]
        )
        data = json.loads((out_dir / "_suggestion_metadata.json").read_text())
        # Sidecar is keyed by entity name; each entry has the envelope
        # fields the persisted YAML deliberately leaves out.
        assert "customer" in data
        customer_meta = data["customer"]
        assert customer_meta["confidence"] == "high"
        assert customer_meta["rationale"].startswith("users has")
        assert customer_meta["pii_hints"] == {"email": "pii"}


# ----- apply mode ------------------------------------------------------------


class TestApply:
    def test_apply_writes_candidates_to_store_with_suggested_origin(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        stub_env: None,
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store(store_path)
        exit_code = main(
            [
                "entities",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--apply",
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 0
        # Both candidates landed in the store with origin="suggested".
        with SQLiteStore(store_path) as store:
            entities = store.list_entities(source_connection_id=_make_source_id(_TEST_URL))
        names = {e.name for e in entities}
        origins = {e.origin for e in entities}
        assert names == {"customer", "order"}
        assert origins == {"suggested"}


# ----- top-k cap -------------------------------------------------------------


class TestTopK:
    def test_top_k_caps_emitted_candidates(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Stub returns 2 candidates; --top-k 1 caps to 1.
        monkeypatch.setenv("SCHEMABRAIN_STUB_RESPONSE", _TWO_CANDIDATES)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        store_path = tmp_path / "store.db"
        _seed_store(store_path)
        out_dir = tmp_path / "suggestions"

        exit_code = main(
            [
                "entities",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--out-dir",
                str(out_dir),
                "--top-k",
                "1",
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 0
        # Only the first (`customer`) made it through.
        files = sorted(p.name for p in out_dir.glob("*.yaml"))
        assert files == ["customer.yaml"]


# ----- cost ceiling ----------------------------------------------------------


class TestCostCeiling:
    def test_breaching_ceiling_exits_with_guided_error(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # An aggressively low ceiling trips pre-flight before the LLM
        # is called. CLI surfaces a guided message and exits non-zero.
        monkeypatch.setenv("SCHEMABRAIN_STUB_RESPONSE", _TWO_CANDIDATES)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        store_path = tmp_path / "store.db"
        _seed_store(store_path)

        exit_code = main(
            [
                "entities",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--dry-run",
                "--provider",
                "stub",
                "--max-cost-usd",
                "0.0000001",
            ]
        )
        assert exit_code == 1
        err = capsys.readouterr().err
        # Guided message names the ceiling so the user knows how to
        # re-run.
        assert "max-cost-usd" in err or "cost" in err.lower()

    def test_env_var_sets_default_ceiling(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # `SCHEMABRAIN_MAX_LLM_COST_USD` provides the default; CLI flag
        # overrides. Verify env var alone is honored.
        monkeypatch.setenv("SCHEMABRAIN_STUB_RESPONSE", _TWO_CANDIDATES)
        monkeypatch.setenv("SCHEMABRAIN_MAX_LLM_COST_USD", "0.0000001")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        store_path = tmp_path / "store.db"
        _seed_store(store_path)

        exit_code = main(
            [
                "entities",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--dry-run",
                "--provider",
                "stub",
            ]
        )
        # Env-var ceiling trips just like the flag did.
        assert exit_code == 1


# ----- LLM error propagation -------------------------------------------------


class TestEdgeCases:
    def test_malformed_cost_env_var_exits_two(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # `SCHEMABRAIN_MAX_LLM_COST_USD=not-a-number` is a configuration
        # error — surface as exit 2 with a guided message rather than
        # crashing on the float() conversion.
        monkeypatch.setenv("SCHEMABRAIN_STUB_RESPONSE", _TWO_CANDIDATES)
        monkeypatch.setenv("SCHEMABRAIN_MAX_LLM_COST_USD", "definitely-not-a-number")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        store_path = tmp_path / "store.db"
        _seed_store(store_path)

        exit_code = main(
            [
                "entities",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--dry-run",
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "SCHEMABRAIN_MAX_LLM_COST_USD" in err

    def test_dry_run_with_zero_candidates_prints_message(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # When the LLM returns no candidates, dry-run prints a message
        # (rather than silent empty output) so the user knows the run
        # succeeded but found nothing.
        monkeypatch.setenv("SCHEMABRAIN_STUB_RESPONSE", "candidates: []")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        store_path = tmp_path / "store.db"
        _seed_store(store_path)

        exit_code = main(
            [
                "entities",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--dry-run",
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "no candidates" in out.lower()

    def test_apply_dbt_owned_entity_exits_one(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Pre-seed a dbt-owned `customer` entity. The suggest --apply
        # path proposes `customer` (origin='suggested'); the store's
        # dbt-guard refuses the overwrite. CLI surfaces exit 1.
        from schemabrain.core.entity import Entity, SingleTableBinding

        store_path = tmp_path / "store.db"
        _seed_store(store_path)
        # Add a dbt-owned customer entity pointing at public.users.
        source_id = _make_source_id(_TEST_URL)
        with SQLiteStore(store_path) as store:
            store.write_entity(
                Entity(
                    name="customer",
                    description="Owned upstream by dbt",
                    binding=SingleTableBinding(qualified_table="public.users"),
                    identity="id",
                    origin="dbt_import",
                ),
                source_connection_id=source_id,
            )

        monkeypatch.setenv("SCHEMABRAIN_STUB_RESPONSE", _TWO_CANDIDATES)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        exit_code = main(
            [
                "entities",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--apply",
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "dbt" in err.lower()

    def test_apply_partial_write_message_lists_committed_entities(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # `write_entity` commits per call. If candidate 2 fails, the
        # user must be told that candidate 1 already landed so they
        # know the state of the store. Without this message they'd
        # have to query the store manually to see what got through.
        bad_yaml = """\
candidates:
  - name: customer
    binding:
      single_table: public.users
    identity: id
    confidence: high
    rationale: r
    pii_hints: {}
  - name: order
    binding:
      single_table: public.orders
    identity: id
    confidence: high
    rationale: r
    pii_hints: {}
  - name: hallucinated
    binding:
      single_table: public.does_not_exist
    identity: id
    confidence: low
    rationale: r
    pii_hints: {}
"""
        monkeypatch.setenv("SCHEMABRAIN_STUB_RESPONSE", bad_yaml)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        store_path = tmp_path / "store.db"
        _seed_store(store_path)

        exit_code = main(
            [
                "entities",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--apply",
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 1
        err = capsys.readouterr().err
        # The message must name BOTH committed entity names so the
        # user knows exactly what's in the store.
        assert "'customer'" in err
        assert "'order'" in err
        # And the count is explicit.
        assert "2 of 3" in err

    def test_apply_fk_violation_exits_one(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # If the LLM hallucinates a candidate bound to a table that
        # isn't in the indexed store, the FK constraint on the
        # entities table fires. CLI surfaces a guided message
        # pointing the user back at `schemabrain index`.
        bad_yaml = """\
candidates:
  - name: hallucinated
    binding:
      single_table: public.does_not_exist
    identity: id
    confidence: high
    rationale: r
    pii_hints: {}
"""
        monkeypatch.setenv("SCHEMABRAIN_STUB_RESPONSE", bad_yaml)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        store_path = tmp_path / "store.db"
        _seed_store(store_path)

        exit_code = main(
            [
                "entities",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--apply",
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "index" in err.lower()


class TestStubFallbackWarning:
    def test_unset_stub_env_var_warns_on_stderr(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # --provider stub without SCHEMABRAIN_STUB_RESPONSE silently
        # used to return an empty candidate list and exit 0 — a CI
        # job that forgot to set the env var would pass deceptively.
        # The warning makes the misconfiguration visible to the
        # operator without changing the exit code (still 0 with no
        # candidates, since the caller might genuinely want to test
        # the empty-result code path).
        monkeypatch.delenv("SCHEMABRAIN_STUB_RESPONSE", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        store_path = tmp_path / "store.db"
        _seed_store(store_path)

        exit_code = main(
            [
                "entities",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--dry-run",
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 0
        err = capsys.readouterr().err
        assert "SCHEMABRAIN_STUB_RESPONSE" in err
        assert "warning" in err.lower()


class TestRenderSanitization:
    def test_description_newlines_collapsed_in_yaml_body(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # An LLM that returns a multi-line description must not break
        # the YAML body when written to --out-dir. The file must still
        # round-trip cleanly through `parse_entity_yaml_file`.
        from schemabrain.entities.yaml_grammar import parse_entity_yaml_file

        multiline_yaml = """\
candidates:
  - name: customer
    description: "first line\\nsecond line injected: malicious"
    binding:
      single_table: public.users
    identity: id
    confidence: high
    rationale: r
    pii_hints: {}
"""
        monkeypatch.setenv("SCHEMABRAIN_STUB_RESPONSE", multiline_yaml)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        store_path = tmp_path / "store.db"
        _seed_store(store_path)
        out_dir = tmp_path / "suggestions"

        exit_code = main(
            [
                "entities",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--out-dir",
                str(out_dir),
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 0
        # Critical: the written YAML must parse cleanly. With the
        # `yaml.safe_dump`-based renderer, newlines inside the
        # description are properly quoted/escaped — the file round-
        # trips through the entity parser and the description value
        # is preserved verbatim (including the newline). What would
        # otherwise be an "injected: malicious" fake YAML key never
        # escapes the description scalar.
        entity = parse_entity_yaml_file(out_dir / "customer.yaml")
        assert entity.name == "customer"
        assert "first line" in entity.description
        assert "second line" in entity.description
        # The "injected: malicious" fragment stays inside the
        # description value rather than parsing as a separate field.
        assert "injected: malicious" in entity.description

    def test_rationale_newlines_collapsed_in_dry_run_comments(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Dry-run renders rationale as a `# rationale: ...` comment line.
        # A newline in the rationale would break the comment-prefix
        # invariant — subsequent characters would appear as live YAML.
        multiline_rationale = """\
candidates:
  - name: customer
    binding:
      single_table: public.users
    identity: id
    confidence: high
    rationale: "line one\\nfake_key: injected"
    pii_hints: {}
"""
        monkeypatch.setenv("SCHEMABRAIN_STUB_RESPONSE", multiline_rationale)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        store_path = tmp_path / "store.db"
        _seed_store(store_path)

        exit_code = main(
            [
                "entities",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--dry-run",
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        # `fake_key:` must not appear at the start of a line (which
        # would make it parseable as a top-level YAML key in a
        # copy-paste). The "# rationale:" line absorbs everything.
        for line in out.splitlines():
            if line.lstrip().startswith("fake_key"):
                pytest.fail(f"newline injection escaped comment prefix: {line!r}")


class TestLLMErrors:
    def test_malformed_llm_output_exits_one(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # If the stub returns unparseable YAML, the CLI surfaces a
        # guided error rather than crashing with a stack trace.
        monkeypatch.setenv("SCHEMABRAIN_STUB_RESPONSE", ":\n: : :")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        store_path = tmp_path / "store.db"
        _seed_store(store_path)

        exit_code = main(
            [
                "entities",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--dry-run",
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 1
        err = capsys.readouterr().err
        # Error message names the failure mode (YAML parse) so the
        # user knows it's not their input.
        assert "YAML" in err or "parse" in err.lower()


class TestLlmFailureShape:
    """F5: Anthropic SDK errors render Shape C, not a raw traceback.

    Pre-F5, an `OverloadedError` (529) / `APIConnectionError` / etc.
    bubbled out of `pipeline.propose_from_tables` through to
    `main()` as an unhandled exception — the user saw a 50-line
    Python traceback. Now the CLI catches them, classifies via
    `errors_render.classify_llm_failure`, renders Shape C, and
    exits 2.

    These tests monkeypatch `EnrichmentPipeline.propose_from_tables`
    directly so the failure surface is exercised without needing
    a real Anthropic round-trip.
    """

    def _run_with_pipeline_raising(
        self,
        *,
        exc: BaseException,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[int, str]:
        """Drive `entities suggest --apply` with a patched pipeline that raises `exc`.

        Returns `(exit_code, stderr)`. The pipeline import inside
        `_cmd_entities_suggest` happens lazily, so the monkeypatch
        targets the canonical module path.
        """
        from schemabrain.entities.suggest import EntitySuggestionPipeline

        store_path = tmp_path / "store.db"
        _seed_store(store_path)

        def _raise(self: object, *args: object, **kwargs: object) -> None:
            raise exc

        monkeypatch.setattr(EntitySuggestionPipeline, "propose_from_tables", _raise)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

        from schemabrain.cli import main as cli_main

        exit_code = cli_main(
            [
                "entities",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--apply",
            ]
        )
        # capsys is fed by the parent test; we read it there.
        return exit_code, ""

    def test_overloaded_renders_shape_c_and_exits_two(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import anthropic

        # 529 surfaces as APIStatusError with status_code=529 in
        # SDK versions without a dedicated `OverloadedError` class.
        exc = anthropic.APIStatusError.__new__(anthropic.APIStatusError)
        exc.status_code = 529
        exc.message = "Overloaded"

        exit_code, _ = self._run_with_pipeline_raising(
            exc=exc, tmp_path=tmp_path, monkeypatch=monkeypatch
        )
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "◆ error" in err
        assert "Anthropic is overloaded" in err
        # Fallback command must NOT appear — standalone suggest has
        # no structure-only escape hatch.
        assert "--no-enrich" not in err
        # Raw Python traceback must NOT appear — that's the F5 bug.
        assert "Traceback" not in err

    def test_rate_limited_renders_shape_c_and_exits_two(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import anthropic

        exc = anthropic.RateLimitError.__new__(anthropic.RateLimitError)
        exc.status_code = 429
        exc.message = "rate limit exceeded"

        exit_code, _ = self._run_with_pipeline_raising(
            exc=exc, tmp_path=tmp_path, monkeypatch=monkeypatch
        )
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "rate-limited by Anthropic" in err

    def test_connection_error_renders_shape_c_and_exits_two(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import anthropic

        exc = anthropic.APIConnectionError.__new__(anthropic.APIConnectionError)
        exc.message = "DNS resolution failed"

        exit_code, _ = self._run_with_pipeline_raising(
            exc=exc, tmp_path=tmp_path, monkeypatch=monkeypatch
        )
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "couldn't reach Anthropic" in err

    def test_non_sdk_exception_still_propagates(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A local programming bug (not an Anthropic SDK error) must
        # still surface as a traceback — the F5 fix narrows the
        # catch to known SDK error types, not all exceptions.
        with pytest.raises(RuntimeError, match="local bug"):
            self._run_with_pipeline_raising(
                exc=RuntimeError("local bug"),
                tmp_path=tmp_path,
                monkeypatch=monkeypatch,
            )


class TestSuggestProgressIntegration:
    """F1: `entities suggest` shows the wizard-parity cost preamble + spinner.

    Pre-F1 the standalone suggest commands were silent for the full
    ~20s LLM round-trip (user thought it was frozen). Now the helper
    `_suggest_llm_progress` fires before the LLM call when:

    * provider != "stub" (the stub returns instantly; preamble's
      cost framing would lie)
    * stderr is a TTY (CI / piped stderr auto-suppresses)

    Tests pin the wiring decision (skip on stub) rather than the
    rendered output (the helper's own unit tests in
    test_cli.py::TestSuggestLlmProgressHelper cover that surface).
    """

    def test_stub_provider_skips_progress_helper(
        self,
        tmp_path: Path,
        stub_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # `--provider stub` must NOT call `_suggest_llm_progress` —
        # the stub returns instantly and the cost framing
        # ("~$0.01 · claude-sonnet-4-6") would be wrong.
        progress_calls: list[dict[str, object]] = []

        from contextlib import nullcontext

        def _spy_progress(**kwargs):
            progress_calls.append(kwargs)
            return nullcontext()

        monkeypatch.setattr("schemabrain.cli._suggest_llm_progress", _spy_progress)

        store_path = tmp_path / "store.db"
        _seed_store(store_path)

        exit_code = main(
            [
                "entities",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--dry-run",
                "--provider",
                "stub",
            ]
        )
        assert exit_code == 0
        assert progress_calls == [], (
            f"stub provider must skip _suggest_llm_progress; got {len(progress_calls)} invocations"
        )

    def test_anthropic_provider_invokes_progress_helper(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # `--provider anthropic` must call `_suggest_llm_progress`
        # exactly once before the LLM round-trip. Spy on the call
        # and stub the pipeline so the test stays offline.
        progress_calls: list[dict[str, object]] = []

        from contextlib import nullcontext

        def _spy_progress(**kwargs):
            progress_calls.append(kwargs)
            return nullcontext()

        monkeypatch.setattr("schemabrain.cli._suggest_llm_progress", _spy_progress)

        # Stub the LLM call so we don't make a real Anthropic request.
        from schemabrain.entities.suggest import (
            EntitySuggestionPipeline,
            SuggestionResult,
        )

        monkeypatch.setattr(
            EntitySuggestionPipeline,
            "propose_from_tables",
            lambda self, *args, **kwargs: SuggestionResult(
                candidates=[], total_cost_usd=0.0, llm_model="claude-sonnet-4-6"
            ),
        )

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")

        store_path = tmp_path / "store.db"
        _seed_store(store_path)

        exit_code = main(
            [
                "entities",
                "suggest",
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
                "--dry-run",
            ]
        )
        assert exit_code == 0
        assert len(progress_calls) == 1
        call = progress_calls[0]
        assert call["model"] == "claude-sonnet-4-6"
        assert call["cost_estimate_usd"] == 0.01
        # The cap defaults to _DEFAULT_SUGGEST_MAX_COST_USD ($1.00)
        # when no --max-cost-usd / env var is set.
        assert call["cap_usd"] == 1.0
        # Action string names the surface so the operator can tell
        # entity-suggest from metric-suggest at a glance.
        assert "identify business entities" in str(call["action"])
        # Includes the table count threaded through from the loaded
        # schema (_seed_store inserts 2 tables: users + orders).
        assert "2 tables" in str(call["action"])
