"""Tests for the eval golden-set loader.

A golden set is a JSON file pinning a known schema's expected answers
to natural-language questions. The loader parses + validates it into
typed, immutable models so the runner can score retrieval against it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from schemabrain.eval.golden import GoldenQuestion, GoldenSet, load_golden


def _write_json(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "golden.json"
    p.write_text(json.dumps(payload))
    return p


def _valid_payload() -> dict:
    return {
        "version": "1",
        "schema_description": "Synthetic e-commerce schema",
        "questions": [
            {
                "id": "q001",
                "question": "Where do we store user emails?",
                "expected_tables": ["public.users"],
            },
            {
                "id": "q002",
                "question": "Where can I find order history?",
                "expected_tables": ["public.orders", "public.order_items"],
                "notes": "multi-table",
            },
        ],
    }


class TestGoldenQuestion:
    def test_is_frozen(self) -> None:
        q = GoldenQuestion(
            id="q001",
            question="x?",
            expected_tables=("public.users",),
        )
        with pytest.raises(ValidationError):
            q.id = "q999"  # type: ignore[misc]

    def test_default_notes_is_empty_string(self) -> None:
        q = GoldenQuestion(id="q001", question="x?", expected_tables=("public.users",))
        assert q.notes == ""

    def test_id_must_be_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            GoldenQuestion(id="", question="x?", expected_tables=("public.users",))

    def test_question_must_be_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            GoldenQuestion(id="q001", question="", expected_tables=("public.users",))

    def test_expected_tables_must_have_at_least_one(self) -> None:
        with pytest.raises(ValidationError):
            GoldenQuestion(id="q001", question="x?", expected_tables=())

    def test_expected_tables_must_be_qualified(self) -> None:
        # "users" without schema prefix is rejected — every retriever in
        # this codebase keys on (schema, name), so unqualified names are
        # always a bug in the golden set, never a feature.
        with pytest.raises(ValidationError, match="qualified"):
            GoldenQuestion(id="q001", question="x?", expected_tables=("users",))

    def test_expected_tables_dedupes_within_question(self) -> None:
        # Same table listed twice in expected — clearly an authoring slip.
        with pytest.raises(ValidationError, match="duplicate"):
            GoldenQuestion(
                id="q001",
                question="x?",
                expected_tables=("public.users", "public.users"),
            )


class TestLoadGolden:
    def test_loads_valid_file(self, tmp_path: Path) -> None:
        path = _write_json(tmp_path, _valid_payload())
        gs = load_golden(path)
        assert isinstance(gs, GoldenSet)
        assert gs.version == "1"
        assert gs.schema_description == "Synthetic e-commerce schema"
        assert len(gs.questions) == 2
        assert gs.questions[0].id == "q001"
        assert gs.questions[1].notes == "multi-table"

    def test_loads_str_path_too(self, tmp_path: Path) -> None:
        path = _write_json(tmp_path, _valid_payload())
        gs = load_golden(str(path))
        assert len(gs.questions) == 2

    def test_missing_file_raises_filenotfound(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_golden(tmp_path / "nope.json")

    def test_invalid_json_raises_value_error(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("{not valid json")
        with pytest.raises(ValueError, match="parse"):
            load_golden(p)

    def test_missing_required_field_raises(self, tmp_path: Path) -> None:
        payload = _valid_payload()
        del payload["questions"]
        path = _write_json(tmp_path, payload)
        with pytest.raises(ValidationError):
            load_golden(path)

    def test_duplicate_question_ids_rejected(self, tmp_path: Path) -> None:
        payload = _valid_payload()
        payload["questions"][1]["id"] = payload["questions"][0]["id"]
        path = _write_json(tmp_path, payload)
        with pytest.raises(ValueError, match="duplicate question"):
            load_golden(path)

    def test_empty_questions_list_rejected(self, tmp_path: Path) -> None:
        # A golden set with zero questions is meaningless — caught at load.
        payload = _valid_payload()
        payload["questions"] = []
        path = _write_json(tmp_path, payload)
        with pytest.raises(ValidationError):
            load_golden(path)


class TestBundledGoldenSet:
    def test_repo_default_loads_cleanly(self) -> None:
        # The bundled `golden_sets/ecommerce.json` must always load.
        # If a contributor breaks it, the eval CLI's default flow breaks.
        from schemabrain.eval.golden import DEFAULT_GOLDEN_PATH

        assert DEFAULT_GOLDEN_PATH.exists(), f"default golden set missing at {DEFAULT_GOLDEN_PATH}"
        # The default lives under `golden_sets/` so future bundled
        # domains (HR, analytics, etc.) drop in alongside it without
        # restructuring; verify the convention holds.
        assert DEFAULT_GOLDEN_PATH.parent.name == "golden_sets"
        gs = load_golden(DEFAULT_GOLDEN_PATH)
        assert len(gs.questions) >= 5, "preview launch needs >=5 questions"
        # Every expected_tables entry must be qualified (`schema.table`).
        for q in gs.questions:
            for t in q.expected_tables:
                assert "." in t, f"unqualified table in {q.id}: {t!r}"

    def test_bundled_sql_fixture_is_present_and_non_empty(self) -> None:
        # `fixtures/ecommerce.sql` is the schema users apply to their
        # Postgres before running the bundled eval — quickstart depends
        # on it. Test the same `Path(__file__).parent / ...` pattern that
        # would resolve inside an installed wheel, so accidental wheel-
        # exclusion via a future pyproject.toml edit fails this test.
        from schemabrain.eval import golden

        fixture = Path(golden.__file__).parent / "fixtures" / "ecommerce.sql"
        assert fixture.exists(), f"bundled SQL fixture missing at {fixture}"
        assert fixture.parent.name == "fixtures"
        # Sanity check the contents — DDL for at least the five tables
        # the golden set's questions assume. Match permissively (with or
        # without `IF NOT EXISTS`, with or without schema prefix) so a
        # stylistic edit to the SQL doesn't false-positive this test.
        text = fixture.read_text()
        for table in ("users", "products", "categories", "orders", "order_items"):
            assert (
                f"TABLE IF NOT EXISTS public.{table}" in text or f"TABLE public.{table}" in text
            ), f"fixture missing CREATE TABLE for {table!r}"
