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


def test_run_dashboard_friendly_message_on_port_collision(
    store_path: Path, monkeypatch, capsys
) -> None:
    """regression: when the operator launches a second dashboard
    against a port that's already bound, surface ONE actionable
    message instead of uvicorn's raw ``[Errno 48]`` log line, and
    return exit code 1.

    Implementation note: ``uvicorn.run`` swallows ``OSError`` internally
    (logs via its own logger and returns exit 0 without raising), so
    the friendly handling lives in a pre-flight ``socket.bind`` probe
    BEFORE ``uvicorn.run`` is invoked. We patch the probe to simulate
    the collision without actually binding a real socket — keeps the
    test deterministic on shared CI runners.
    """
    from schemabrain.dashboard import cli as dashboard_cli

    monkeypatch.setattr(dashboard_cli, "_port_is_available", lambda host, port: False)
    from schemabrain.dashboard.cli import run_dashboard

    code = run_dashboard(
        store_path=store_path,
        port=7878,
        open_browser=False,
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "port 7878 is already in use" in err
    assert "--port" in err  # actionable: suggests the override
    # Stack trace is NOT in stderr — confirms we handled the error.
    assert "Traceback" not in err


def test_port_is_available_returns_true_for_free_port(monkeypatch) -> None:
    """Port-probe primitive: a port that successfully binds returns
    True. Implementation uses a throwaway socket so the probe doesn't
    rely on a real free port (CI runners have unpredictable
    availability).
    """
    from schemabrain.dashboard.cli import _port_is_available

    # 0 means OS-assigned port — guaranteed available; we don't care
    # which one ends up bound, just that the probe semantics work.
    assert _port_is_available("127.0.0.1", 0) is True


def test_port_is_available_returns_false_on_eaddrinuse(monkeypatch) -> None:
    """Port-probe primitive: when ``socket.bind`` raises EADDRINUSE,
    the probe returns False so the CLI can surface the friendly
    message instead of trying uvicorn and getting silently swallowed.
    """
    import errno
    import socket

    from schemabrain.dashboard.cli import _port_is_available

    original_socket = socket.socket

    class _FakeSocket:
        def __init__(self, *args, **kwargs) -> None:
            self._real = original_socket(*args, **kwargs)

        def bind(self, address) -> None:
            raise OSError(errno.EADDRINUSE, "address already in use")

        def close(self) -> None:
            self._real.close()

    monkeypatch.setattr(socket, "socket", _FakeSocket)

    assert _port_is_available("127.0.0.1", 7878) is False


def test_port_is_available_returns_false_on_unexpected_oserror(monkeypatch) -> None:
    """Defensive: any other ``OSError`` (permission denied, address
    family not supported, etc.) also yields False — the port is
    logically unavailable, the operator gets the friendly EADDRINUSE
    message which still points at ``--port`` as the escape hatch even
    if the root cause was different.
    """
    import errno
    import socket

    from schemabrain.dashboard.cli import _port_is_available

    original_socket = socket.socket

    class _FakeSocket:
        def __init__(self, *args, **kwargs) -> None:
            self._real = original_socket(*args, **kwargs)

        def bind(self, address) -> None:
            raise OSError(errno.EACCES, "permission denied")

        def close(self) -> None:
            self._real.close()

    monkeypatch.setattr(socket, "socket", _FakeSocket)

    assert _port_is_available("127.0.0.1", 7878) is False
