"""Tests for `schemabrain.setup.preflight`.

The preflight is a one-shot env check that runs at the top of
`schemabrain init` to refuse fast on the Apple Silicon + Python 3.12+
+ missing-fastembed combination. Pinning the detector's gating
prevents both false positives (refusing operators on supported
combos) and false negatives (letting the wizard crash at stage 2
with an opaque ImportError instead of surfacing the gap upfront).
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator

import pytest

from schemabrain.setup.preflight import detect_apple_silicon_fastembed_gap


@pytest.fixture()
def force_fastembed_missing(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make `importlib.util.find_spec("fastembed")` return None.

    Mirrors the real condition on Apple Silicon + Python 3.12+ where
    `pip install schemabrain` succeeds but `fastembed` is silently
    absent because its `onnxruntime` dependency has no wheel.
    """
    real = importlib.util.find_spec

    def fake(name: str, *args: object, **kwargs: object) -> object | None:
        if name == "fastembed":
            return None
        return real(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("schemabrain.setup.preflight.importlib.util.find_spec", fake)
    yield


@pytest.fixture()
def force_fastembed_present(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make `importlib.util.find_spec("fastembed")` return a non-None spec.

    Mirrors the case where the operator worked around the gap (conda,
    manual wheel install, pyenv-switched to 3.11). When fastembed is
    importable the preflight must NOT refuse — second-guessing a
    working install would block the very operators who fixed the gap.
    """

    def fake(name: str, *args: object, **kwargs: object) -> object:
        if name == "fastembed":
            return object()  # truthy non-None — the detector only checks `is None`
        return importlib.util.find_spec(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("schemabrain.setup.preflight.importlib.util.find_spec", fake)
    yield


class TestDetectAppleSiliconFastembedGap:
    def test_returns_explanation_on_arm64_py312_without_fastembed(
        self, monkeypatch: pytest.MonkeyPatch, force_fastembed_missing: None
    ) -> None:
        """All three signals: arm64 + py>=3.12 + no fastembed → refuse."""
        monkeypatch.setattr("schemabrain.setup.preflight.platform.machine", lambda: "arm64")
        monkeypatch.setattr(
            "schemabrain.setup.preflight.sys.version_info",
            (3, 12, 0, "final", 0),
        )
        gap = detect_apple_silicon_fastembed_gap()
        assert gap is not None
        # The message names the specific problematic combination so
        # operators reading the refusal know it's not a generic
        # "missing dep" error.
        assert "Apple Silicon" in gap
        assert "arm64" in gap
        assert "fastembed" in gap
        assert "onnxruntime" in gap

    def test_returns_none_on_x86_64_mac(
        self, monkeypatch: pytest.MonkeyPatch, force_fastembed_missing: None
    ) -> None:
        """Intel Mac has working onnxruntime wheels — no gap to surface."""
        monkeypatch.setattr("schemabrain.setup.preflight.platform.machine", lambda: "x86_64")
        monkeypatch.setattr(
            "schemabrain.setup.preflight.sys.version_info",
            (3, 12, 0, "final", 0),
        )
        assert detect_apple_silicon_fastembed_gap() is None

    def test_returns_none_on_linux(
        self, monkeypatch: pytest.MonkeyPatch, force_fastembed_missing: None
    ) -> None:
        """Linux x86_64 — onnxruntime ships wheels. No gap regardless of py version."""
        monkeypatch.setattr("schemabrain.setup.preflight.platform.machine", lambda: "x86_64")
        monkeypatch.setattr(
            "schemabrain.setup.preflight.sys.version_info",
            (3, 12, 0, "final", 0),
        )
        assert detect_apple_silicon_fastembed_gap() is None

    def test_returns_none_on_arm64_py311(
        self, monkeypatch: pytest.MonkeyPatch, force_fastembed_missing: None
    ) -> None:
        """Python 3.11 on arm64 — onnxruntime ships a wheel. No gap."""
        monkeypatch.setattr("schemabrain.setup.preflight.platform.machine", lambda: "arm64")
        monkeypatch.setattr(
            "schemabrain.setup.preflight.sys.version_info",
            (3, 11, 10, "final", 0),
        )
        assert detect_apple_silicon_fastembed_gap() is None

    def test_returns_none_when_fastembed_importable(
        self, monkeypatch: pytest.MonkeyPatch, force_fastembed_present: None
    ) -> None:
        """When fastembed IS importable the operator already worked
        around the gap (manual wheel, conda, etc.). Don't second-guess.
        """
        monkeypatch.setattr("schemabrain.setup.preflight.platform.machine", lambda: "arm64")
        monkeypatch.setattr(
            "schemabrain.setup.preflight.sys.version_info",
            (3, 13, 0, "final", 0),
        )
        assert detect_apple_silicon_fastembed_gap() is None

    def test_message_includes_running_python_version(
        self, monkeypatch: pytest.MonkeyPatch, force_fastembed_missing: None
    ) -> None:
        """The refusal copy names the running Python version so the
        operator can verify the detection wasn't a stale-cache misread.
        """
        monkeypatch.setattr("schemabrain.setup.preflight.platform.machine", lambda: "arm64")
        monkeypatch.setattr(
            "schemabrain.setup.preflight.sys.version_info",
            (3, 13, 1, "final", 0),
        )
        gap = detect_apple_silicon_fastembed_gap()
        assert gap is not None
        assert "3.13" in gap

    def test_running_environment_returns_none(self) -> None:
        """The actual running test environment must NOT trigger the
        refusal — otherwise the whole pytest suite fails on Apple
        Silicon. This is the smoke test that pins the detector
        actually returns None when fastembed is installed
        (the maintainer's local-dev target).

        Note: this asserts via `if/else` rather than a single
        equality because in CI on Linux this trivially passes; on
        maintainer macOS arm64 it asserts the dev-install actually
        has fastembed available.
        """
        gap = detect_apple_silicon_fastembed_gap()
        # If we're on arm64 + py>=3.12, fastembed MUST be importable
        # for the test suite to even start (the suite imports
        # `schemabrain` which transitively touches embedding code
        # paths via collection). So the gap should be None.
        assert gap is None or sys.version_info < (3, 12)
