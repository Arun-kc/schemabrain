"""Local-embedding adapter and Protocol for column descriptions.

The pipeline programs against the `Embedder` Protocol so tests can
substitute `FakeEmbedder` (no model download, no ONNX runtime). The
production adapter wraps `fastembed.TextEmbedding` with the
`BAAI/bge-small-en-v1.5` model — 384-dim float32, ~67MB on disk, ~10ms
per embedding warm.

The fastembed adapter is created lazily (module load is cheap; the model
load + ~70MB download happens on first call to `embed`). Callers that
want eager warm-up can just call `embed("warmup")` once at startup.

Privacy: this entire layer runs locally. No description text leaves the
machine. That's the whole reason we don't use OpenAI/Voyage embeddings.
"""

from __future__ import annotations

import contextlib
import hashlib
import math
import os
import shutil
import struct
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from schemabrain.core.embedding import ColumnEmbedding

# The single embedding model we ship in v0. Change requires a store
# wipe (for now); a future slice can detect drift and re-embed
# automatically.
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_EMBEDDING_DIM = 384

# fastembed maps the model name to a HuggingFace Hub repo
# internally — `BAAI/bge-small-en-v1.5` → `qdrant/bge-small-en-v1.5-onnx-q`
# with file `model_optimized.onnx`. We use this mapping for an eager
# pre-fetch via huggingface_hub.snapshot_download because fastembed's
# own download has a silent-failure mode on large blobs: the small
# config + tokenizer files complete fine but the ~67 MB ONNX blob
# can end up as a zero-byte ``.incomplete`` file with the symlink
# left dangling. fastembed's loader then walks the snapshot dir,
# finds the symlink missing the resolved target, and crashes deep
# inside ONNXRuntime with ``NoSuchFile``. The pre-fetch closes that
# loophole by using huggingface_hub's snapshot_download (which has
# explicit integrity checking + retry) instead of relying on
# fastembed's internal download path.
#
# Keyed by the model name we pass to ``TextEmbedding(...)``. Each
# row pins (repo_id, model_filename, min_expected_size_bytes,
# revision_sha). ``revision_sha`` is a HF Hub commit SHA — pinning is
# required by bandit's B615 check (CWE-494, "Download of code without
# integrity check"): if Qdrant's org account is ever compromised, an
# unpinned ``snapshot_download`` would silently consume whatever the
# attacker pushed to ``main``. Pinning to the SHA that produced the
# 66 MB model we observed in an end-to-end smoke run is content-addressable
# safety. Bumping the model means updating BOTH the SHA and re-verifying
# the size floor.
_HF_REPO_MAP: dict[str, tuple[str, str, int, str]] = {
    # model_name → (hf_repo_id, model_filename, min_expected_size_bytes, revision_sha)
    "BAAI/bge-small-en-v1.5": (
        "qdrant/bge-small-en-v1.5-onnx-q",
        "model_optimized.onnx",
        # The model is ~66 MB; 10 MB floor catches the zero-byte
        # partial-download case without being so tight we'd false-flag
        # a legitimately smaller future variant.
        10_000_000,
        # HF Hub commit SHA observed on 2026-05-29 — the snapshot that
        # produces a verified 66,465,124-byte ``model_optimized.onnx``.
        "52398278842ec682c6f32300af41344b1c0b0bb2",
    ),
}

# fastembed defaults the cache to `$TMPDIR/fastembed_cache/`,
# which on macOS resolves to `/var/folders/.../T/` — a system-managed
# directory subject to periodic cleanup (~3-day TTL on `cleanup-T`,
# plus reboot purge). Once macOS evicts the model, the snapshot dir
# survives as a skeleton (tokenizer config etc. remain) but the actual
# `model_optimized.onnx` blob is gone, so the next embedding call
# crashes with `NoSuchFile`. That's catastrophic for HN visitors who
# install today and come back to a working dashboard on day 3.
#
# We override to `~/.cache/fastembed/` — a persistent, user-owned
# location that matches the convention used by huggingface_hub,
# transformers, and other Python ML tools. Operators can override via
# the `SCHEMABRAIN_FASTEMBED_CACHE` env var if they have storage policy
# constraints (containerized deployments often want `/data/cache/...`).
_FASTEMBED_CACHE_ENV = "SCHEMABRAIN_FASTEMBED_CACHE"


def _resolve_fastembed_cache_dir() -> Path:
    """Pick the on-disk location fastembed should use for model snapshots.

    Precedence: `$SCHEMABRAIN_FASTEMBED_CACHE` (operator override) →
    `~/.cache/fastembed` (the v0 default — persistent, cross-platform).
    Caller is responsible for ensuring the directory is writable; we
    only resolve the path.
    """
    override = os.environ.get(_FASTEMBED_CACHE_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / "fastembed"


def _ensure_model_files_present(cache_dir: Path, model_name: str) -> None:
    """Eager-fetch the model files via ``huggingface_hub.snapshot_download``.

    fastembed's internal download path silently fails on large
    blobs for the bge-small-en-v1.5 model. After the (partial) download,
    the snapshot dir has the small config/tokenizer files but the
    ~67 MB ``model_optimized.onnx`` is left as a zero-byte
    ``.incomplete`` orphan with a dangling symlink. fastembed's loader
    then walks into ONNXRuntime which raises ``NoSuchFile``. The first
    smoke iteration's ``_heal_partial_download`` cleared the orphan
    but the next download attempt repeated the same failure pattern.

    The cure: bypass fastembed's auto-download and use
    ``huggingface_hub.snapshot_download`` directly. It has explicit
    integrity checking via ETag, retries on transient failures, and
    is the canonical HF Hub download primitive. Idempotent — if the
    files are already present (full size, matching ETag), this is a
    cheap no-op walk.

    After download we verify the model file exists at the expected
    path under a ``snapshots`` subdir AND meets a minimum size floor
    (catches the zero-byte case ``.incomplete`` would have raised).
    A miss here raises ``EmbeddingUnavailableError`` so the MCP
    wrapper can return a recovery envelope rather than fall through
    to fastembed's silent failure.

    Models not in the ``_HF_REPO_MAP`` (e.g. a custom model the
    operator passed via the embedder factory) skip the eager fetch —
    fastembed's default path runs unchanged. Adding a model means
    adding a row to ``_HF_REPO_MAP``.
    """
    mapping = _HF_REPO_MAP.get(model_name)
    if mapping is None:
        # Unknown model — let fastembed handle it. We don't know its
        # repo mapping so we can't pre-fetch; this is the v0 hardcoded
        # registry pattern, not a generic resolver.
        return

    repo_id, model_filename, min_size, revision = mapping

    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        # huggingface_hub is a hard dependency (transitive via
        # fastembed); a missing import here means an install regression
        # the operator must see, not something we silently work around.
        raise EmbeddingUnavailableError(f"huggingface_hub not importable: {exc}") from exc

    try:
        snapshot_path = snapshot_download(
            repo_id=repo_id,
            # Pin the revision (bandit B615 / CWE-494): without this an
            # attacker who compromises the HF repo could push a malicious
            # model and our download would silently consume it.
            # Content-addressable safety via the SHA in ``_HF_REPO_MAP``.
            revision=revision,
            cache_dir=str(cache_dir),
            # Narrow allow_patterns so we don't pull the unquantized
            # model variant or sample data that some repos ship
            # alongside the ONNX export.
            allow_patterns=["*.onnx", "*.json", "tokenizer*", "*.txt"],
        )
    except Exception as exc:
        # Any HF Hub error (network down, repo gone, auth required for
        # a future model behind a gate) becomes ``EmbeddingUnavailableError``
        # so MCP wrappers can produce a recovery envelope steering the
        # agent at ``list_entities``.
        raise EmbeddingUnavailableError(
            f"snapshot_download failed for {repo_id}: {type(exc).__name__}: {exc}"
        ) from exc

    # Integrity check: the model file must exist AND meet a size floor.
    # The size floor catches the zero-byte partial-download case that
    # ``snapshot_download`` itself doesn't always surface as an error
    # (some failure modes leave a stale 0-byte file behind).
    model_path = Path(snapshot_path) / model_filename
    if not model_path.exists():
        raise EmbeddingUnavailableError(
            f"model file {model_filename!r} missing from snapshot {snapshot_path!r} "
            f"after snapshot_download — repo {repo_id!r} layout may have changed"
        )
    size = model_path.stat().st_size
    if size < min_size:
        # Wipe the corrupted blob so the NEXT call re-downloads from
        # scratch — leaving the 0-byte file in place would prevent
        # snapshot_download from retrying (it would see the file
        # present and skip).
        with contextlib.suppress(OSError):
            model_path.unlink()
        raise EmbeddingUnavailableError(
            f"model file {model_filename!r} downloaded to {size} bytes "
            f"(expected >= {min_size}); cache wiped for retry"
        )


def _heal_partial_download(cache_dir: Path, model_name: str) -> None:
    """Self-heal a partial-download corruption before fastembed loads.

    HuggingFace Hub writes blob files atomically: every byte goes to
    `blobs/<hash>.incomplete` first, then `os.rename` to `blobs/<hash>`
    on completion. An interrupted download (network drop, Ctrl+C, OOM
    kill) leaves the `.incomplete` blob behind. fastembed's loader
    doesn't notice — it walks the snapshot dir's symlinks, finds the
    `model_optimized.onnx` symlink pointing at a non-existent blob, and
    crashes with `NoSuchFile` deep inside ONNXRuntime.

    We sniff for `*.incomplete` blobs under the model's cache subtree
    and wipe the whole `models--<owner>--<name>` directory if any are
    found. fastembed re-downloads from scratch on next instantiation.
    """
    # HF Hub mangles the model name as `models--<owner>--<name>` —
    # mirror the convention exactly so we don't accidentally wipe a
    # different model's cache (`bge-small-en-v1.5` vs the larger
    # variants we might add later).
    mangled = "models--" + model_name.replace("/", "--")
    model_root = cache_dir / mangled
    if not model_root.is_dir():
        return

    blobs_dir = model_root / "blobs"
    if not blobs_dir.is_dir():
        return

    partials = list(blobs_dir.glob("*.incomplete"))
    if not partials:
        return

    # Any partial → wipe the whole model tree. fastembed will re-fetch
    # on next `TextEmbedding(...)` construction. Cheaper to redownload
    # than to surgically reattach symlinks to a half-finished blob and
    # discover later that the .onnx is corrupt at inference time.
    shutil.rmtree(model_root, ignore_errors=True)


@runtime_checkable
class Embedder(Protocol):
    """Single-text embedding producer.

    Implementations return a float32-equivalent tuple of length
    `dimension`. Stable for the same input (modulo ONNX nondeterminism,
    which is well below cosine-similarity-relevance thresholds).
    """

    @property
    def model_name(self) -> str:
        """Stable identifier for this embedder (persisted on stored vectors)."""
        ...

    @property
    def dimension(self) -> int:
        """Length of every vector this embedder produces."""
        ...

    def embed(self, text: str) -> tuple[float, ...]:
        """Embed one piece of text. Length == `dimension`."""
        ...


def embedding_for(text: str, *, embedder: Embedder) -> ColumnEmbedding:
    """Produce a stored-form `ColumnEmbedding` for `text`.

    Validates non-empty text at the boundary so a caller that forgot to
    enrich first (or passed a stripped description) fails loudly rather
    than persisting a zero-vector that quietly tanks retrieval.
    """
    if not text or not text.strip():
        raise ValueError("text must be non-empty for embedding")
    vector = embedder.embed(text)
    # ColumnEmbedding's constructor validates dimension == len(vector);
    # if the embedder lied about its shape, that's where it explodes.
    return ColumnEmbedding(
        vector=vector,
        model=embedder.model_name,
        dimension=embedder.dimension,
    )


@dataclass
class FakeEmbedder:
    """Test double for `Embedder`.

    Produces a deterministic, hash-derived vector of the requested
    dimension so test assertions can compare embeddings for equality
    and so different texts produce different vectors (mimicking a real
    embedder's distinguishing behavior).
    """

    dimension: int
    model_name: str = "fake-embedder"
    calls: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.dimension <= 0:
            raise ValueError(f"dimension must be positive, got {self.dimension}")

    def embed(self, text: str) -> tuple[float, ...]:
        self.calls.append(text)
        # Hash the text into deterministic float32 bytes, then unpack
        # enough of them to fill `dimension`. Repeats the hash if the
        # requested dimension exceeds one digest's worth of floats.
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        floats: list[float] = []
        salt = 0
        while len(floats) < self.dimension:
            block = hashlib.sha256(digest + salt.to_bytes(4, "little")).digest()
            # 32 bytes / 4 bytes per float = 8 float32 per block.
            for i in range(0, 32, 4):
                floats.append(struct.unpack("<f", block[i : i + 4])[0])
                if len(floats) == self.dimension:
                    break
            salt += 1
        # Replace any NaN/inf that struct unpacking can produce from
        # arbitrary bytes — tests should compare clean numbers.
        cleaned = tuple(0.0 if (math.isnan(f) or math.isinf(f)) else f for f in floats)
        return cleaned


@dataclass
class FastEmbedEmbedder:
    """Production `Embedder` backed by fastembed's ONNX runtime.

    The `fastembed.TextEmbedding` instance is constructed lazily so that
    importing this module (and running unit tests that don't touch
    fastembed) doesn't trigger a 70MB model download. The first call to
    `embed` pays the load + download cost; subsequent calls are warm.

    `_model` is excluded from `__init__`, `__eq__`, and `__repr__`
    because it's lazy state — two FastEmbedEmbedders with the same
    `model_name` and `dimension` should compare equal regardless of
    whether either has been warmed up, and the live ONNX object would
    produce useless `<TextEmbedding object at 0x...>` noise in repr.
    """

    model_name: str = DEFAULT_EMBEDDING_MODEL
    dimension: int = DEFAULT_EMBEDDING_DIM
    _model: Any = field(default=None, init=False, repr=False, compare=False)

    def embed(self, text: str) -> tuple[float, ...]:
        try:
            model = self._ensure_model()
            # fastembed.embed() is batch-oriented and yields numpy arrays;
            # we materialize the single result and convert to tuple at
            # the boundary so the rest of the codebase stays numpy-free.
            vectors: Iterable[object] = model.embed([text])
            first = next(iter(vectors))
            return tuple(float(x) for x in first)
        except EmbeddingUnavailableError:
            # Already classified — let it bubble up to the MCP wrapper.
            raise
        except Exception as exc:
            # any failure inside fastembed / ONNX (missing
            # model file, network down, corrupt blob, OOM) needs to be
            # promoted to ``EmbeddingUnavailableError`` so the MCP tool
            # wrappers can produce an envelope with a useful recovery
            # hint (``suggested_tool="list_entities"``) — without that
            # the agent sees ``internal_error`` with an empty
            # ``Recovery()`` and falls back to its own training-prior
            # heuristic (write raw SQL, bypass the firewall). The
            # original exception is chained so server logs still see
            # the underlying cause for debugging.
            raise EmbeddingUnavailableError(
                f"embedding model unavailable: {type(exc).__name__}: {exc}"
            ) from exc

    def _ensure_model(self) -> Any:
        if self._model is None:
            # Imported lazily so test-only paths (FakeEmbedder) never pay
            # the cost of importing onnxruntime.
            from fastembed import TextEmbedding

            # fastembed defaults the cache to `$TMPDIR/fastembed_cache/`
            # which macOS purges every ~3 days. Pin to `~/.cache/fastembed/`
            # so the model survives system housekeeping. Then sniff for a
            # partial download corruption (`.incomplete` blob orphans from
            # a prior interrupted download) and wipe-and-retry once if found.
            # We do the heal BEFORE construction so the redownload is
            # transparent to the caller; we don't try/except the
            # construction itself because fastembed's own download retry
            # logic handles transient network errors, and we'd lose the
            # distinction between "broken cache" (heal-able) and "no
            # network" (a real env problem the operator must see).
            cache_dir = _resolve_fastembed_cache_dir()
            cache_dir.mkdir(parents=True, exist_ok=True)
            _heal_partial_download(cache_dir, self.model_name)
            # pre-fetch + integrity check via huggingface_hub
            # so fastembed's silent-failure download path can't leave
            # us with a dangling ``model_optimized.onnx`` symlink.
            _ensure_model_files_present(cache_dir, self.model_name)
            self._model = TextEmbedding(
                model_name=self.model_name,
                cache_dir=str(cache_dir),
            )
        return self._model


def fastembed_default() -> FastEmbedEmbedder:
    """Convenience factory for the v0 default (BAAI/bge-small-en-v1.5)."""
    return FastEmbedEmbedder()


class EmbeddingUnavailableError(RuntimeError):
    """Embedding pipeline failure that MCP wrappers can recognize.

    Raised by ``FastEmbedEmbedder.embed`` when the underlying fastembed
    runtime fails — missing model file (the heal step couldn't recover),
    corrupt cache, network unreachable on cold start, ONNX runtime
    crash. The MCP tool wrappers (``find_relevant_tables``,
    ``find_relevant_entities``) catch this specifically and return an
    ``internal_error`` envelope with a populated ``Recovery`` pointing
    the agent at ``list_entities`` / ``list_tables`` — non-embedding
    discovery tools that work without the model.

    Distinct from a generic ``Exception`` so the broad
    ``except Exception`` catch-all in the MCP server's boundary path
    stays the path-of-last-resort. Anything classified as
    ``EmbeddingUnavailableError`` gets a tailored envelope; anything
    else falls through to the ``internal_error`` generic catch.
    """


__all__ = [
    "DEFAULT_EMBEDDING_DIM",
    "DEFAULT_EMBEDDING_MODEL",
    "Embedder",
    "EmbeddingUnavailableError",
    "FastEmbedEmbedder",
    "embedding_for",
    "fastembed_default",
]
# `FakeEmbedder` is intentionally NOT in `__all__`. It's a test double
# that lives in this module for symmetry with `FakeLLMClient`, but
# shouldn't show up in `from schemabrain.enrichment.embeddings import *`
# in user code. Tests import it by name.
