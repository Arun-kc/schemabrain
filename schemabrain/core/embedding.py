"""Stored representation of one local-embedding vector for a column.

Lives in `core` because the embedder (producer) and the store (persister)
both depend on it. Vector type is `tuple[float, ...]` deliberately — the
fastembed adapter converts numpy arrays at the boundary so the store and
its tests stay numpy-free.

`dimension` is redundant with `len(vector)` but is materialized as a
column on the store so the schema can express the shape without parsing
every BLOB. The constructor enforces `dimension == len(vector)` so the
two cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ColumnEmbedding:
    """One embedding vector for one column's description.

    `vector` is the float32 vector produced by the embedder, materialized
    as a tuple so the type is hashable + immutable. `model` records which
    embedder produced it so a future model swap can detect drift.
    """

    vector: tuple[float, ...]
    model: str
    dimension: int

    def __post_init__(self) -> None:
        if self.dimension <= 0:
            raise ValueError(f"dimension must be positive, got {self.dimension}")
        if self.dimension != len(self.vector):
            raise ValueError(
                f"dimension {self.dimension} does not match vector length {len(self.vector)}"
            )
        if not self.model:
            raise ValueError("model name must be non-empty")
