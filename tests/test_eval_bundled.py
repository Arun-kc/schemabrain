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
from schemabrain.eval.bundled import (
    bundled_entities_fixture_dir,
    bundled_joins_fixture_dir,
    bundled_metrics_fixture_dir,
    list_bundled_files,
    resolve_bundled_path,
)


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


class TestPackRegistry:
    """The ADD-model pack registry: named packs, ecommerce the default.

    A pack is the three demo-YAML dir roots (entities/joins/metrics).
    Phase 1 adds `saas` as a one-line `_PACKS["saas"] = _make_pack("saas")`
    drop-in and flips `DEFAULT_PACK`; these tests pin the contract that
    makes that drop-in safe.
    """

    def test_default_pack_is_ecommerce(self) -> None:
        assert bundled.DEFAULT_PACK == "ecommerce"
        assert "ecommerce" in bundled._PACKS

    def test_get_pack_none_resolves_to_default(self) -> None:
        # Zero-arg / None must resolve to the default pack — this is
        # what every existing zero-arg getter call depends on.
        assert bundled._get_pack().name == "ecommerce"
        assert bundled._get_pack(None) is bundled._get_pack("ecommerce")

    def test_get_pack_explicit_ecommerce(self) -> None:
        assert bundled._get_pack("ecommerce").name == "ecommerce"

    def test_get_pack_unknown_raises_value_error_listing_available(self) -> None:
        # The ADD-model error path: asking for a pack that hasn't been
        # registered yet (saas, before Phase 1) fails loudly and lists
        # what IS available.
        with pytest.raises(ValueError, match="unknown demo pack") as exc:
            bundled._get_pack("saas")
        assert "ecommerce" in str(exc.value)

    def test_get_pack_empty_string_is_not_default(self) -> None:
        # Only None means "use the default". An empty string is an
        # explicit (unregistered) pack name and must fail loudly rather
        # than silently falling back to ecommerce.
        with pytest.raises(ValueError, match="unknown demo pack"):
            bundled._get_pack("")

    def test_pack_dir_path_depths(self) -> None:
        # The 'ecommerce' segment lives at three different physical
        # depths; pin each so the factory's per-field join can't drift.
        pack = bundled._get_pack("ecommerce")
        assert pack.entities_dir.parts[-3:] == ("fixtures", "entities", "ecommerce")
        assert pack.joins_dir.parts[-3:] == ("joins", "fixtures", "ecommerce")
        assert pack.metrics_dir.parts[-3:] == ("metrics", "fixtures", "ecommerce")


class TestFixtureDirBackcompat:
    """The three dir getters: zero-arg, pack=None, and pack='ecommerce'
    are all identical, so every existing zero-arg caller is unaffected.
    """

    @pytest.mark.parametrize(
        "getter,tail",
        [
            (bundled_entities_fixture_dir, ("fixtures", "entities", "ecommerce")),
            (bundled_joins_fixture_dir, ("joins", "fixtures", "ecommerce")),
            (bundled_metrics_fixture_dir, ("metrics", "fixtures", "ecommerce")),
        ],
    )
    def test_zero_arg_default_and_explicit_ecommerce_agree(
        self, getter: object, tail: tuple[str, ...]
    ) -> None:
        zero_arg = getter()  # type: ignore[operator]
        explicit_none = getter(pack=None)  # type: ignore[operator]
        explicit_ecommerce = getter(pack="ecommerce")  # type: ignore[operator]
        assert zero_arg == explicit_none == explicit_ecommerce
        assert zero_arg.parts[-3:] == tail

    def test_unknown_pack_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="unknown demo pack"):
            bundled_entities_fixture_dir(pack="saas")
