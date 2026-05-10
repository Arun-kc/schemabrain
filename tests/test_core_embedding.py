"""Tests for schemabrain.core.embedding.ColumnEmbedding."""

from __future__ import annotations

import pytest

from schemabrain.core.embedding import ColumnEmbedding


class TestColumnEmbeddingInvariants:
    def test_round_trips_basic_fields(self) -> None:
        e = ColumnEmbedding(vector=(0.1, 0.2, 0.3), model="bge-small", dimension=3)
        assert e.vector == (0.1, 0.2, 0.3)
        assert e.model == "bge-small"
        assert e.dimension == 3

    def test_dimension_must_match_vector_length(self) -> None:
        with pytest.raises(ValueError, match="dimension 4 does not match vector length 3"):
            ColumnEmbedding(vector=(0.1, 0.2, 0.3), model="bge-small", dimension=4)

    def test_zero_dimension_rejected(self) -> None:
        with pytest.raises(ValueError, match="dimension must be positive"):
            ColumnEmbedding(vector=(), model="bge-small", dimension=0)

    def test_negative_dimension_rejected(self) -> None:
        with pytest.raises(ValueError, match="dimension must be positive"):
            ColumnEmbedding(vector=(0.1,), model="bge-small", dimension=-1)

    def test_empty_model_rejected(self) -> None:
        with pytest.raises(ValueError, match="model name must be non-empty"):
            ColumnEmbedding(vector=(0.1, 0.2), model="", dimension=2)

    def test_is_hashable(self) -> None:
        e1 = ColumnEmbedding(vector=(0.1, 0.2), model="m", dimension=2)
        e2 = ColumnEmbedding(vector=(0.1, 0.2), model="m", dimension=2)
        assert hash(e1) == hash(e2)
        assert {e1, e2} == {e1}

    def test_frozen(self) -> None:
        e = ColumnEmbedding(vector=(0.1,), model="m", dimension=1)
        with pytest.raises(Exception):  # noqa: B017 — dataclass.FrozenInstanceError, but stable hierarchy varies
            e.vector = (0.2,)  # type: ignore[misc]
