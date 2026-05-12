"""Tests for schemabrain.eval.bundled — resolver for bundled fixture files.

The resolver is the backing implementation of `schemabrain fixture-path`.
Its contract: given a bare filename, return the absolute path to the
matching file inside `fixtures/` or `golden_sets/`, or raise a useful
error. Path-traversal and separator-in-name attempts must be rejected
at the boundary; this module is reachable from the CLI which accepts
arbitrary user strings.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.eval import bundled
from schemabrain.eval.bundled import list_bundled_files, resolve_bundled_path


class TestResolveBundledPath:
    def test_resolves_sql_fixture(self) -> None:
        path = resolve_bundled_path("ecommerce.sql")
        assert path.is_file()
        assert path.suffix == ".sql"
        assert path.parent == bundled._BUNDLED_FIXTURES_DIR
        assert path.is_absolute()

    def test_resolves_golden_set_json(self) -> None:
        path = resolve_bundled_path("ecommerce.json")
        assert path.is_file()
        assert path.suffix == ".json"
        assert path.parent == bundled._BUNDLED_GOLDEN_DIR
        assert path.is_absolute()

    def test_missing_name_raises_file_not_found(self) -> None:
        with pytest.raises(FileNotFoundError) as exc:
            resolve_bundled_path("nonexistent.txt")
        # The error must list what IS available so the user can correct
        # their command on the first try — a bare "not found" forces
        # them to guess.
        msg = str(exc.value)
        assert "ecommerce.sql" in msg
        assert "ecommerce.json" in msg

    def test_empty_name_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            resolve_bundled_path("")

    @pytest.mark.parametrize(
        "bad_name",
        [
            "../etc/passwd",  # traversal up
            "subdir/foo.sql",  # forward slash
            "subdir\\foo.sql",  # backslash (Windows-style)
            "..",  # double-dot alone
        ],
    )
    def test_rejects_path_traversal_or_separator(self, bad_name: str) -> None:
        # Defense in depth: even though the only valid bundled names are
        # bare basenames, the resolver must reject any input that could
        # escape the bundled directories. Without this, a `(base / name)`
        # composition with name="../../etc/passwd" would resolve to an
        # absolute path outside the package and `.exists()` would happily
        # return True if such a file exists on the host.
        with pytest.raises(ValueError):
            resolve_bundled_path(bad_name)

    def test_rejects_symlink_escaping_bundled_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Belt-and-braces: a maliciously crafted wheel could ship a
        # symlink inside fixtures/ pointing at /etc/passwd or any other
        # host-system file. `is_file()` follows symlinks, so without an
        # explicit containment check the symlink target would be
        # returned. The containment check rejects it; FileNotFoundError
        # is the right signal because no *valid bundled* match exists.
        fake_fixtures = tmp_path / "fixtures"
        fake_fixtures.mkdir()
        outside_target = tmp_path / "secret.txt"
        outside_target.write_text("SECRET")
        (fake_fixtures / "escape.sql").symlink_to(outside_target)

        monkeypatch.setattr(bundled, "_BUNDLED_DIRS", (fake_fixtures,))

        with pytest.raises(FileNotFoundError):
            resolve_bundled_path("escape.sql")

    def test_fixtures_dir_wins_on_basename_collision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If the same basename ever ends up in BOTH bundled dirs (e.g. a
        # contributor accidentally ships ecommerce.sql in golden_sets/),
        # fixtures/ wins because it comes first in _BUNDLED_DIRS. Pin
        # the contract so a future tuple reorder is a deliberate test
        # change, not invisible drift.
        fixtures = tmp_path / "fixtures"
        golden = tmp_path / "golden_sets"
        fixtures.mkdir()
        golden.mkdir()
        (fixtures / "dup.txt").write_text("from fixtures")
        (golden / "dup.txt").write_text("from golden")

        monkeypatch.setattr(bundled, "_BUNDLED_DIRS", (fixtures, golden))

        resolved = resolve_bundled_path("dup.txt")
        assert resolved.parent == fixtures.resolve()
        assert resolved.read_text() == "from fixtures"


class TestListBundledFiles:
    def test_lists_known_fixtures(self) -> None:
        files = list_bundled_files()
        assert "ecommerce.sql" in files
        assert "ecommerce.json" in files

    def test_returns_sorted_list(self) -> None:
        # Deterministic ordering matters for the error message in
        # resolve_bundled_path (which embeds this list) — sorted output
        # makes the message stable across runs and across filesystems
        # with different default iteration order.
        files = list_bundled_files()
        assert files == sorted(files)

    def test_returns_basenames_only(self) -> None:
        # Output is what users pass back to `resolve_bundled_path` and
        # to the CLI; embedded path separators would be unusable.
        for name in list_bundled_files():
            assert "/" not in name
            assert "\\" not in name
