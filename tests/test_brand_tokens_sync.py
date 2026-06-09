"""Drift guard for the brand layer mirrored into the standalone ``site/``.

Companion to ``scripts/sync_brand_tokens.py``: the script copies the design
tokens, fonts, ``BrainMark``, and mirrored positioning strings from ``web/``
(the single source) into ``site/``; this test fails CI if any copy is stale.

Workflow: edit a brand file in ``web/`` → run
``python scripts/sync_brand_tokens.py`` → commit. Skip the sync and
``test_brand_layer_in_sync`` goes red with the exact files to fix.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "sync_brand_tokens.py"


def _load_sync_module():
    spec = importlib.util.spec_from_file_location("sync_brand_tokens", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_sync = _load_sync_module()


class TestBrandTokensSync:
    def test_brand_layer_in_sync(self) -> None:
        # `write=False` returns the assets that WOULD change — the stale
        # copies. Empty == site/ matches web/ byte-for-byte.
        drifts = _sync.sync(write=False)
        assert not drifts, (
            "site/ brand layer is stale vs web/: "
            + ", ".join(f"{d.path} ({d.description})" for d in drifts)
            + " — run `python scripts/sync_brand_tokens.py`."
        )

    def test_every_source_exists(self) -> None:
        # A renamed/removed web/ source would make the copy silently impossible;
        # surface it as a clear failure rather than a copy that never runs.
        web = _REPO_ROOT / "web"
        for asset in _sync.ASSETS:
            src = web / asset.src
            assert src.exists(), f"brand source missing: web/{asset.src} ({asset.description})"
