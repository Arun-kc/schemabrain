"""Drift guard for positioning references in non-importable surfaces.

Companion to ``scripts/sync_positioning.py``: the script rewrites every
positioning reference from the canonical :mod:`schemabrain.positioning`; this
test fails CI if any of them is stale. Same shape as
``test_docs_version_sync.py``.

Workflow: edit ``schemabrain/positioning.py`` → run
``python scripts/sync_positioning.py`` → commit. Skip the sync and
``test_surfaces_in_sync`` goes red with the exact files to fix.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

from schemabrain import positioning

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "sync_positioning.py"


def _load_sync_module():
    spec = importlib.util.spec_from_file_location("sync_positioning", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_sync = _load_sync_module()


class TestPositioningSync:
    def test_surfaces_in_sync(self) -> None:
        # `write=False` returns the references that WOULD change — the stale
        # ones. Empty == everything matches the canonical constants.
        drifts = _sync.sync(write=False)
        assert not drifts, (
            "positioning references are stale vs schemabrain.positioning: "
            + ", ".join(f"{d.path} ({d.description})" for d in drifts)
            + " — run `python scripts/sync_positioning.py`."
        )

    def test_every_rule_matches_at_least_one_target(self) -> None:
        # If a surface is reformatted so a rule's pattern no longer matches,
        # the rule has gone dead and would silently stop syncing. Fail loudly.
        for rule in _sync.RULES:
            targets = _sync._rule_targets(rule, _REPO_ROOT)
            assert targets, f"rule {rule.description!r} matched no files (glob {rule.glob!r})"
            hit = any(re.search(rule.pattern, p.read_text(), flags=re.MULTILINE) for p in targets)
            assert hit, (
                f"rule {rule.description!r} (pattern {rule.pattern!r}) matched no content "
                "in its targets — the reference was removed/reformatted; update or drop the rule."
            )

    def test_rewrite_is_self_healing_and_idempotent(self) -> None:
        # An arbitrary old positioning line is replaced with the canonical one,
        # and re-running the rewrite is a no-op (the output re-matches).
        rule = next(r for r in _sync.RULES if r.description == "pyproject project.description")
        stale = 'description = "The SQL firewall between AI agents and your production database."'
        out, n = _sync._rewrite(stale, rule)
        assert n == 1
        assert positioning.SHORT_DESCRIPTION in out
        assert "firewall" not in out
        out2, n2 = _sync._rewrite(out, rule)
        assert n2 == 1
        assert out2 == out
