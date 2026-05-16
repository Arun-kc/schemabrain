"""Tests for EntityCandidate + SuggestionResult — the suggestion envelope.

The envelope carries TRANSIENT inference artifacts (confidence, rationale,
pii_hints) that surround a persisted-clean `Entity` dataclass. Once a
user runs `entities suggest --apply`, only the Entity persists; the
envelope metadata flows to an audit log row, not into the entity YAML.

Design pins:
  - confidence is categorical Literal["high", "medium", "low"], NOT float
  - rationale carries the LLM's own reasoning verbatim
  - pii_hints maps column name to proposed Sensitivity, rides on the
    envelope only — does NOT auto-promote to `Entity.pii_sensitivity`
    until a future PII validator gate lands

These dataclass invariants are what the suggest pipeline (step 2) and
CLI surface (step 5) program against. Tests pin the shape so a future
refactor can't silently break the envelope contract.
"""

from __future__ import annotations

import dataclasses

import pytest

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.entities.suggest import EntityCandidate, SuggestionResult


def _sample_entity(name: str = "customer", table: str = "public.users") -> Entity:
    return Entity(
        name=name,
        description="A registered customer with email",
        binding=SingleTableBinding(qualified_table=table),
        identity="id",
        origin="suggested",
    )


# ----- EntityCandidate -------------------------------------------------------


class TestEntityCandidate:
    def test_accepts_well_formed_candidate(self) -> None:
        candidate = EntityCandidate(
            entity=_sample_entity(),
            confidence="high",
            rationale="users table has id PK and email NOT NULL",
            pii_hints={"email": "pii"},
        )
        assert candidate.entity.name == "customer"
        assert candidate.confidence == "high"
        assert candidate.rationale.startswith("users table")
        assert candidate.pii_hints == {"email": "pii"}

    def test_is_frozen(self) -> None:
        candidate = EntityCandidate(
            entity=_sample_entity(),
            confidence="medium",
            rationale="...",
            pii_hints={},
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            candidate.confidence = "low"  # type: ignore[misc]

    @pytest.mark.parametrize("confidence", ["high", "medium", "low"])
    def test_accepts_all_three_confidence_levels(self, confidence: str) -> None:
        candidate = EntityCandidate(
            entity=_sample_entity(),
            confidence=confidence,  # type: ignore[arg-type]
            rationale="...",
            pii_hints={},
        )
        assert candidate.confidence == confidence

    @pytest.mark.parametrize("bad", ["", "HIGH", "very-high", "0.87", "unknown"])
    def test_rejects_invalid_confidence(self, bad: str) -> None:
        with pytest.raises(ValueError, match="confidence"):
            EntityCandidate(
                entity=_sample_entity(),
                confidence=bad,  # type: ignore[arg-type]
                rationale="...",
                pii_hints={},
            )

    def test_accepts_empty_rationale(self) -> None:
        # The LLM doesn't always supply a rationale — accept empty
        # rather than force the pipeline to fabricate one. The CLI
        # review surface displays "(no rationale provided)" when empty.
        candidate = EntityCandidate(
            entity=_sample_entity(),
            confidence="low",
            rationale="",
            pii_hints={},
        )
        assert candidate.rationale == ""

    def test_accepts_empty_pii_hints(self) -> None:
        candidate = EntityCandidate(
            entity=_sample_entity(),
            confidence="high",
            rationale="...",
            pii_hints={},
        )
        assert candidate.pii_hints == {}

    @pytest.mark.parametrize(
        "sensitivity",
        ["public", "internal", "confidential", "pii"],
    )
    def test_accepts_all_four_sensitivity_values(self, sensitivity: str) -> None:
        candidate = EntityCandidate(
            entity=_sample_entity(),
            confidence="high",
            rationale="...",
            pii_hints={"col": sensitivity},  # type: ignore[dict-item]
        )
        assert candidate.pii_hints["col"] == sensitivity

    @pytest.mark.parametrize("bad", ["PII", "secret", "private", ""])
    def test_rejects_invalid_pii_hint_sensitivity(self, bad: str) -> None:
        with pytest.raises(ValueError, match="pii_hints"):
            EntityCandidate(
                entity=_sample_entity(),
                confidence="high",
                rationale="...",
                pii_hints={"email": bad},  # type: ignore[dict-item]
            )

    def test_pii_hints_cannot_be_mutated_after_construction(self) -> None:
        # `frozen=True` blocks `candidate.pii_hints = {}` but not
        # `candidate.pii_hints["x"] = "pii"` if the field were a plain
        # dict. The wrap-with-MappingProxyType in __post_init__ makes
        # that mutation raise TypeError instead — closing the silent
        # bypass of the validation loop.
        candidate = EntityCandidate(
            entity=_sample_entity(),
            confidence="high",
            rationale="...",
            pii_hints={"email": "pii"},
        )
        with pytest.raises(TypeError):
            candidate.pii_hints["email"] = "public"  # type: ignore[index]
        with pytest.raises(TypeError):
            candidate.pii_hints["new_field"] = "pii"  # type: ignore[index]

    def test_pii_hints_source_dict_mutation_does_not_leak(self) -> None:
        # `__post_init__` copies the source dict before wrapping —
        # otherwise a caller mutating the dict they passed in would
        # see (and be able to modify) the inside of the candidate.
        source = {"email": "pii"}
        candidate = EntityCandidate(
            entity=_sample_entity(),
            confidence="high",
            rationale="...",
            pii_hints=source,
        )
        source["email"] = "public"  # mutate the source AFTER construction
        # The candidate's view stays at its construction-time value.
        assert candidate.pii_hints["email"] == "pii"

    def test_two_equal_candidates_compare_equal(self) -> None:
        a = EntityCandidate(
            entity=_sample_entity(),
            confidence="high",
            rationale="...",
            pii_hints={"email": "pii"},
        )
        b = EntityCandidate(
            entity=_sample_entity(),
            confidence="high",
            rationale="...",
            pii_hints={"email": "pii"},
        )
        assert a == b


# ----- SuggestionResult ------------------------------------------------------


class TestSuggestionResult:
    def test_accepts_well_formed_result(self) -> None:
        candidate = EntityCandidate(
            entity=_sample_entity(),
            confidence="high",
            rationale="...",
            pii_hints={},
        )
        result = SuggestionResult(
            candidates=(candidate,),
            total_cost_usd=0.0234,
            llm_model="claude-sonnet-4-6",
        )
        assert len(result.candidates) == 1
        assert result.total_cost_usd == pytest.approx(0.0234)
        assert result.llm_model == "claude-sonnet-4-6"

    def test_is_frozen(self) -> None:
        result = SuggestionResult(
            candidates=(),
            total_cost_usd=0.0,
            llm_model="stub",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.total_cost_usd = 1.0  # type: ignore[misc]

    def test_accepts_zero_candidates(self) -> None:
        # An LLM run may return no entity candidates — the suggest
        # pipeline returns an empty result, not None. CLI prints
        # "no candidates suggested" rather than crashing.
        result = SuggestionResult(
            candidates=(),
            total_cost_usd=0.0012,
            llm_model="claude-sonnet-4-6",
        )
        assert result.candidates == ()

    def test_rejects_negative_cost(self) -> None:
        with pytest.raises(ValueError, match="total_cost_usd"):
            SuggestionResult(
                candidates=(),
                total_cost_usd=-0.01,
                llm_model="stub",
            )

    def test_rejects_empty_model_name(self) -> None:
        with pytest.raises(ValueError, match="llm_model"):
            SuggestionResult(
                candidates=(),
                total_cost_usd=0.0,
                llm_model="",
            )

    def test_candidates_field_is_a_tuple(self) -> None:
        # tuple (not list) so the dataclass stays hashable and the
        # frozen=True invariant holds. A list would mutate under the
        # caller's nose.
        candidate = EntityCandidate(
            entity=_sample_entity(),
            confidence="high",
            rationale="...",
            pii_hints={},
        )
        result = SuggestionResult(
            candidates=(candidate,),
            total_cost_usd=0.01,
            llm_model="stub",
        )
        assert isinstance(result.candidates, tuple)
