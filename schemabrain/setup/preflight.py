"""One-shot environment preflight checks run BEFORE the wizard starts.

Distinct from ``schemabrain/setup/checks.py``: those are the per-check
DTOs + runner used by ``schemabrain doctor`` (warn-and-continue,
collected into a report). Preflight checks here run AT THE TOP of
``schemabrain init``, before the wizard does any work, and produce
either ``None`` (clean — proceed) or a refusal message (block — exit 2
with guided recovery copy).

The Apple Silicon + Python 3.12+ + fastembed gap is the load-bearing
case today. ``onnxruntime`` (a transitive dependency of ``fastembed``)
ships no wheel for the (arm64, py>=3.12) combination — installs on
the canonical Mac-developer platform silently lack the embedder, then
the wizard's stage 2 indexer crashes with an opaque ImportError. The
preflight surfaces the gap with two recovery paths before the wizard
makes any commitment.
"""

from __future__ import annotations

import importlib.util
import platform
import sys


def detect_apple_silicon_fastembed_gap() -> str | None:
    """Return a one-line explanation if the operator is on
    (arm64, Python>=3.12) without ``fastembed`` installed; ``None``
    otherwise.

    Three signals all required:

    - ``platform.machine() == "arm64"`` — Apple Silicon. ``x86_64``
      Macs and Linux boxes have working onnxruntime wheels and don't
      need this gate.
    - ``sys.version_info >= (3, 12)`` — the version cutoff where
      onnxruntime stopped shipping arm64 wheels. Python 3.11 on
      Apple Silicon works fine.
    - ``importlib.util.find_spec("fastembed") is None`` — the
      package failed to install (which is what happens silently on
      the broken combo). When ``fastembed`` IS importable the
      operator already worked around the gap (downloaded a wheel
      themselves, used conda-forge, etc.) and the preflight
      shouldn't second-guess them.

    Returns the explanation string for ``_cmd_init`` to render as
    part of the refusal copy; the caller composes the recovery
    options separately so this module stays focused on detection.
    """
    if platform.machine() != "arm64":
        return None
    if sys.version_info < (3, 12):
        return None
    if importlib.util.find_spec("fastembed") is not None:
        return None
    return (
        "Apple Silicon (arm64) + Python "
        f"{sys.version_info[0]}.{sys.version_info[1]} without `fastembed` installed. "
        "`onnxruntime` (a transitive dependency of `fastembed`) ships no "
        "wheel for this combination, so the wizard's index stage cannot "
        "build embeddings without help."
    )
