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

import hashlib
import math
import struct
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from schemabrain.core.embedding import ColumnEmbedding

# The single embedding model we ship in v0. Change requires a store
# wipe (for now); a future slice can detect drift and re-embed
# automatically.
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_EMBEDDING_DIM = 384


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
        model = self._ensure_model()
        # fastembed.embed() is batch-oriented and yields numpy arrays;
        # we materialize the single result and convert to tuple at the
        # boundary so the rest of the codebase stays numpy-free.
        vectors: Iterable[object] = model.embed([text])
        first = next(iter(vectors))
        return tuple(float(x) for x in first)

    def _ensure_model(self) -> Any:
        if self._model is None:
            # Imported lazily so test-only paths (FakeEmbedder) never pay
            # the cost of importing onnxruntime.
            from fastembed import TextEmbedding

            self._model = TextEmbedding(model_name=self.model_name)
        return self._model


def fastembed_default() -> FastEmbedEmbedder:
    """Convenience factory for the v0 default (BAAI/bge-small-en-v1.5)."""
    return FastEmbedEmbedder()


__all__ = [
    "DEFAULT_EMBEDDING_DIM",
    "DEFAULT_EMBEDDING_MODEL",
    "Embedder",
    "FastEmbedEmbedder",
    "embedding_for",
    "fastembed_default",
]
# `FakeEmbedder` is intentionally NOT in `__all__`. It's a test double
# that lives in this module for symmetry with `FakeLLMClient`, but
# shouldn't show up in `from schemabrain.enrichment.embeddings import *`
# in user code. Tests import it by name.
