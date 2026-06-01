"""Drift guard for version references in README + docs.

Companion to ``scripts/sync_versions.py``: the script rewrites every
current-version reference from the canonical ``pyproject.toml`` version;
this test fails CI if any of them is stale. Same shape as
``test_changelog.py`` — a tiny, load-bearing invariant rather than a
broad structural check.

The release flow is: bump ``pyproject.toml`` → run
``python scripts/sync_versions.py`` → commit. If you skip the sync,
``test_docs_in_sync_with_pyproject`` goes red with the exact files to fix.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "sync_versions.py"


def _load_sync_module():
    spec = importlib.util.spec_from_file_location("sync_versions", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register before exec so the module's dataclasses can resolve their
    # own annotations via sys.modules during class creation.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_sync = _load_sync_module()


class TestDocsVersionSync:
    def test_docs_in_sync_with_pyproject(self) -> None:
        # `write=False` returns the references that WOULD change — i.e.
        # the stale ones. Empty list == everything matches pyproject.
        drifts = _sync.sync(write=False)
        assert not drifts, (
            "version references are stale vs pyproject "
            f"({_sync.canonical_version()}): "
            + ", ".join(f"{d.path} ({d.description})" for d in drifts)
            + " — run `python scripts/sync_versions.py`."
        )

    def test_every_rule_matches_at_least_one_target(self) -> None:
        # Guards against silent rot: if a doc is reformatted so a rule's
        # pattern no longer matches anywhere, the rule has gone dead and
        # would never sync that surface again. Force it to fail loudly.
        for rule in _sync.RULES:
            targets = _sync._rule_targets(rule, _REPO_ROOT)
            assert targets, f"rule {rule.description!r} matched no files (glob {rule.glob!r})"
            hit = any(re.search(rule.pattern, p.read_text()) for p in targets)
            assert hit, (
                f"rule {rule.description!r} (pattern {rule.pattern!r}) matched no content "
                "in its targets — the reference was removed/reformatted; update or drop the rule."
            )

    def test_rewrite_updates_any_old_version(self) -> None:
        # The rewrite must be self-healing: an arbitrary old version is
        # replaced with the canonical one.
        version = _sync.canonical_version()
        sample = "install with schemabrain==0.1.2 today"
        rule = next(r for r in _sync.RULES if "install pin" in r.description)
        out, n = _sync._rewrite(sample, rule, version)
        assert n == 1
        assert f"schemabrain=={version}" in out
        assert "0.1.2" not in out
