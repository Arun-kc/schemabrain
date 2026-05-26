"""Coverage tests for the ``schemabrain dashboard`` CLI launcher.

The launcher's blocking step is ``uvicorn.run`` — covered by mocking
that out so the test can drive the early-return + happy-path branches
without binding a real port. The full bind-and-serve path is exercised
by the manual smoke (`scripts/dashboard_demo.py`); these tests cover
the validation + error-surface branches that the smoke doesn't.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from schemabrain.dashboard.sidecar import is_ui_available

pytestmark = pytest.mark.skipif(
    not is_ui_available(),
    reason="`schemabrain[ui]` extra not installed",
)

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def mock_uvicorn(monkeypatch) -> Iterator[list[dict]]:
    """Replace ``uvicorn.run`` with a recording stub so tests don't bind ports."""
    calls: list[dict] = []

    def _fake_run(app, *, host: str, port: int, **kwargs) -> None:
        calls.append({"host": host, "port": port, **kwargs})

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", _fake_run)
    yield calls


def test_run_dashboard_exits_2_when_uvicorn_missing(store_path: Path, monkeypatch, capsys) -> None:
    """Pretend uvicorn isn't installed — surface code 2 + actionable msg."""
    import builtins

    real_import = builtins.__import__

    def _block_uvicorn(name, *args, **kwargs):
        if name == "uvicorn":
            raise ImportError("uvicorn not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_uvicorn)

    from schemabrain.dashboard.cli import run_dashboard

    code = run_dashboard(store_path=store_path, port=7878, open_browser=False)
    assert code == 2
    err = capsys.readouterr().err
    assert "pip install schemabrain[ui]" in err


def test_run_dashboard_exits_1_on_invalid_store_path(tmp_path: Path, capsys) -> None:
    from schemabrain.dashboard.cli import run_dashboard

    code = run_dashboard(
        store_path=tmp_path / "missing.db",
        port=7878,
        open_browser=False,
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "store_path does not exist" in err


def test_run_dashboard_happy_path_invokes_uvicorn(store_path: Path, mock_uvicorn, capsys) -> None:
    from schemabrain.dashboard.cli import run_dashboard

    code = run_dashboard(
        store_path=store_path,
        port=7879,
        open_browser=False,
    )
    assert code == 0
    assert len(mock_uvicorn) == 1
    call = mock_uvicorn[0]
    assert call["host"] == "127.0.0.1"
    assert call["port"] == 7879
    err = capsys.readouterr().err
    assert "127.0.0.1:7879" in err


def test_run_dashboard_open_browser_does_not_crash_when_browser_missing(
    store_path: Path, mock_uvicorn, monkeypatch
) -> None:
    """webbrowser.open raises on headless CI — must be suppressed."""
    import webbrowser

    def _boom(_url: str) -> bool:
        raise RuntimeError("no browser available")

    monkeypatch.setattr(webbrowser, "open", _boom)
    from schemabrain.dashboard.cli import run_dashboard

    code = run_dashboard(
        store_path=store_path,
        port=7880,
        open_browser=True,
    )
    assert code == 0
    assert len(mock_uvicorn) == 1
