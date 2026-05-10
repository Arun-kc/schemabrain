"""Tests for schemabrain.enrichment.embeddings.

The Embedder Protocol + FakeEmbedder give us a unit-test surface that
never touches the ONNX runtime. The FastEmbedEmbedder adapter's lazy
loader and tuple-conversion paths are exercised here via a stubbed
`fastembed.TextEmbedding` so we can hit 100% coverage without
downloading a 67MB model in unit tests. A real end-to-end run against
the actual fastembed model is documented in the slice's manual E2E
log.
"""

from __future__ import annotations

import pytest

from schemabrain.core.embedding import ColumnEmbedding
from schemabrain.enrichment.embeddings import (
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_EMBEDDING_MODEL,
    Embedder,
    FakeEmbedder,
    FastEmbedEmbedder,
    embedding_for,
    fastembed_default,
)


class TestFakeEmbedder:
    def test_implements_embedder_protocol(self) -> None:
        e = FakeEmbedder(dimension=4)
        assert isinstance(e, Embedder)

    def test_embed_returns_correct_dimension(self) -> None:
        e = FakeEmbedder(dimension=8)
        v = e.embed("hello world")
        assert len(v) == 8

    def test_embed_is_deterministic_for_same_text(self) -> None:
        e = FakeEmbedder(dimension=16)
        a = e.embed("customer email address")
        b = e.embed("customer email address")
        assert a == b

    def test_embed_differs_for_different_text(self) -> None:
        e = FakeEmbedder(dimension=16)
        a = e.embed("customer email address")
        b = e.embed("order line item quantity")
        assert a != b

    def test_embed_returns_tuple_of_floats(self) -> None:
        e = FakeEmbedder(dimension=4)
        v = e.embed("anything")
        assert isinstance(v, tuple)
        assert all(isinstance(x, float) for x in v)

    def test_model_property_exposed(self) -> None:
        e = FakeEmbedder(dimension=4, model_name="fake-emb")
        assert e.model_name == "fake-emb"

    def test_dimension_property_exposed(self) -> None:
        e = FakeEmbedder(dimension=12)
        assert e.dimension == 12

    def test_records_calls_for_assertions(self) -> None:
        e = FakeEmbedder(dimension=4)
        e.embed("first")
        e.embed("second")
        assert e.calls == ["first", "second"]

    def test_zero_dimension_rejected(self) -> None:
        with pytest.raises(ValueError, match="dimension must be positive"):
            FakeEmbedder(dimension=0)

    def test_negative_dimension_rejected(self) -> None:
        with pytest.raises(ValueError, match="dimension must be positive"):
            FakeEmbedder(dimension=-1)


class TestEmbeddingFor:
    def test_returns_column_embedding_with_vector_and_metadata(self) -> None:
        e = FakeEmbedder(dimension=8, model_name="fake-emb")
        ce = embedding_for("Customer's email address", embedder=e)
        assert isinstance(ce, ColumnEmbedding)
        assert ce.dimension == 8
        assert len(ce.vector) == 8
        assert ce.model == "fake-emb"

    def test_passes_text_through_to_embedder(self) -> None:
        e = FakeEmbedder(dimension=4)
        embedding_for("hello", embedder=e)
        assert e.calls == ["hello"]

    def test_empty_text_rejected(self) -> None:
        # An embedding of an empty description is meaningless; surface it
        # at the boundary so caller bugs (forgot to enrich first) fail
        # loudly rather than silently storing a zero-vector.
        e = FakeEmbedder(dimension=4)
        with pytest.raises(ValueError, match="text must be non-empty"):
            embedding_for("", embedder=e)

    def test_whitespace_only_text_rejected(self) -> None:
        e = FakeEmbedder(dimension=4)
        with pytest.raises(ValueError, match="text must be non-empty"):
            embedding_for("   \n\t  ", embedder=e)

    def test_dimension_mismatch_from_embedder_raises(self) -> None:
        # If an embedder reports dimension N but actually produces a
        # vector of different length, we want the constructor of
        # ColumnEmbedding to reject it (defense in depth — fastembed
        # could in principle change behavior under us).
        class LiarEmbedder:
            model_name = "liar"
            dimension = 4

            def embed(self, text: str) -> tuple[float, ...]:
                return (0.1, 0.2, 0.3)  # 3 floats, claims 4

        with pytest.raises(ValueError, match="dimension 4 does not match vector length 3"):
            embedding_for("anything", embedder=LiarEmbedder())  # type: ignore[arg-type]


class _StubTextEmbedding:
    """Minimal stand-in for `fastembed.TextEmbedding`.

    `embed(texts)` is a generator that yields one numpy-ish object per
    input — represented here as a tuple of floats so we don't even need
    numpy for the test.

    `init_calls` is class-level so tests can inspect it after the
    instance was constructed inside `FastEmbedEmbedder._ensure_model`.
    Each test resets it via `monkeypatch.setattr` so pytest restores it
    on teardown — safer than manual reset.
    """

    init_calls: list[dict] = []  # noqa: RUF012 — class-level test capture; reset per-test via monkeypatch

    def __init__(self, *, model_name: str) -> None:
        type(self).init_calls.append({"model_name": model_name})
        self._model_name = model_name

    def embed(self, texts):
        for t in texts:
            # Length doesn't have to match DEFAULT_EMBEDDING_DIM here;
            # FastEmbedEmbedder's tuple conversion just trusts the
            # underlying model. Tests that check the cross-cutting
            # ColumnEmbedding shape use a 4-dim FastEmbedEmbedder
            # explicitly.
            yield (0.1 * len(t), 0.2, 0.3, 0.4)


class TestFastEmbedEmbedder:
    def test_default_model_and_dimension(self) -> None:
        e = FastEmbedEmbedder()
        assert e.model_name == DEFAULT_EMBEDDING_MODEL
        assert e.dimension == DEFAULT_EMBEDDING_DIM

    def test_factory_returns_fastembed_embedder(self) -> None:
        assert isinstance(fastembed_default(), FastEmbedEmbedder)

    def test_embed_lazily_loads_model_then_returns_tuple_of_floats(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Patch where the import happens — fastembed.TextEmbedding —
        # since FastEmbedEmbedder imports it inside _ensure_model.
        # Reset init_calls via monkeypatch so pytest restores it.
        import fastembed

        monkeypatch.setattr(_StubTextEmbedding, "init_calls", [])
        monkeypatch.setattr(fastembed, "TextEmbedding", _StubTextEmbedding)

        e = FastEmbedEmbedder(dimension=4)
        v = e.embed("hello")

        # Model name was passed to the underlying loader.
        assert _StubTextEmbedding.init_calls == [{"model_name": DEFAULT_EMBEDDING_MODEL}]
        # tuple-of-floats at the boundary, not numpy.
        assert isinstance(v, tuple)
        assert all(isinstance(x, float) for x in v)
        assert v == (
            pytest.approx(0.5),  # 0.1 * len("hello") = 0.5
            pytest.approx(0.2),
            pytest.approx(0.3),
            pytest.approx(0.4),
        )

    def test_embed_only_loads_model_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Subsequent embed() calls must reuse the model — otherwise we'd
        # pay the (real) ~10s ONNX load on every call.
        import fastembed

        monkeypatch.setattr(_StubTextEmbedding, "init_calls", [])
        monkeypatch.setattr(fastembed, "TextEmbedding", _StubTextEmbedding)

        e = FastEmbedEmbedder(dimension=4)
        e.embed("first")
        e.embed("second")
        e.embed("third")

        assert len(_StubTextEmbedding.init_calls) == 1

    def test_implements_embedder_protocol(self) -> None:
        # Constructed instance must satisfy the Protocol.
        e = FastEmbedEmbedder()
        assert isinstance(e, Embedder)

    def test_eq_and_repr_unaffected_by_lazy_model_load(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression for HIGH 1: two embedders with same model_name +
        # dimension must compare equal regardless of warm-up state, and
        # repr must NOT include the live ONNX object.
        import fastembed

        monkeypatch.setattr(_StubTextEmbedding, "init_calls", [])
        monkeypatch.setattr(fastembed, "TextEmbedding", _StubTextEmbedding)

        cold = FastEmbedEmbedder(dimension=4)
        warm = FastEmbedEmbedder(dimension=4)
        warm.embed("warm me up")

        assert cold == warm, "warm-up state must not affect equality"
        assert "TextEmbedding" not in repr(warm), "live model object must not leak into repr"
        assert "_model" not in repr(warm)
