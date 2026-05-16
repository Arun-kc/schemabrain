"""Tests for `parse_suggestions` — LLM YAML output to EntityCandidate list.

The pipeline asks the LLM to emit a strict YAML document with a
top-level `candidates` list. Each item is an entity body (matching
`yaml_grammar` shape) plus envelope fields (confidence, rationale,
pii_hints). This parser enforces the shape strictly — a single
malformed candidate fails the whole batch, since partial-acceptance
would silently let the LLM dictate schema drift.

Round-trip implication: the persisted Entity inside each candidate
gets `origin="suggested"` here, so by the time CLI `--apply` calls
into the store, the origin is already correct.
"""

from __future__ import annotations

import pytest

from schemabrain.entities.suggest import (
    EntityCandidate,
    SuggestionParseError,
    parse_suggestions,
)

# ----- happy paths -----------------------------------------------------------


class TestParseSuggestionsHappyPath:
    def test_single_minimal_candidate(self) -> None:
        text = """
candidates:
  - name: customer
    description: A registered customer
    binding:
      single_table: public.users
    identity: id
    confidence: high
    rationale: users has id PK and NOT NULL email
    pii_hints:
      email: pii
"""
        result = parse_suggestions(text)
        assert len(result) == 1
        candidate = result[0]
        assert isinstance(candidate, EntityCandidate)
        assert candidate.entity.name == "customer"
        assert candidate.entity.description == "A registered customer"
        assert candidate.entity.binding.qualified_table == "public.users"
        assert candidate.entity.identity == "id"
        # The parser stamps origin="suggested" on every candidate —
        # the LLM doesn't need to specify it (and shouldn't be able to
        # forge "dbt_import" by accident).
        assert candidate.entity.origin == "suggested"
        assert candidate.confidence == "high"
        assert candidate.rationale.startswith("users has")
        assert candidate.pii_hints == {"email": "pii"}

    def test_multiple_candidates_preserve_order(self) -> None:
        text = """
candidates:
  - name: customer
    binding:
      single_table: public.users
    identity: id
    confidence: high
    rationale: r1
    pii_hints: {}
  - name: order
    binding:
      single_table: public.orders
    identity: id
    confidence: medium
    rationale: r2
    pii_hints: {}
  - name: product
    binding:
      single_table: public.products
    identity: id
    confidence: low
    rationale: r3
    pii_hints: {}
"""
        result = parse_suggestions(text)
        assert [c.entity.name for c in result] == ["customer", "order", "product"]
        assert [c.confidence for c in result] == ["high", "medium", "low"]

    def test_description_is_optional(self) -> None:
        # Entity dataclass already permits empty description. The
        # parser should not require it from the LLM output either —
        # some candidates land without a description and that's fine.
        text = """
candidates:
  - name: customer
    binding:
      single_table: public.users
    identity: id
    confidence: high
    rationale: r
    pii_hints: {}
"""
        result = parse_suggestions(text)
        assert result[0].entity.description == ""

    def test_rationale_is_optional(self) -> None:
        # The LLM may omit a rationale; UI shows "(no rationale)" in that
        # case. Empty string is the canonical absent-value shape.
        text = """
candidates:
  - name: customer
    binding:
      single_table: public.users
    identity: id
    confidence: high
    pii_hints: {}
"""
        result = parse_suggestions(text)
        assert result[0].rationale == ""

    def test_pii_hints_is_optional(self) -> None:
        # Schemas where no column looks PII-flagged: LLM may omit
        # pii_hints entirely. Default to an empty dict.
        text = """
candidates:
  - name: customer
    binding:
      single_table: public.users
    identity: id
    confidence: high
    rationale: r
"""
        result = parse_suggestions(text)
        assert result[0].pii_hints == {}

    def test_empty_candidates_list_returns_empty_list(self) -> None:
        # An LLM run may return no entity candidates — pipeline returns
        # an empty list, not None. CLI prints "no candidates suggested."
        text = "candidates: []"
        result = parse_suggestions(text)
        assert result == []


# ----- structural / top-level errors -----------------------------------------


class TestParseSuggestionsTopLevelStructure:
    def test_invalid_yaml_raises(self) -> None:
        with pytest.raises(SuggestionParseError, match="YAML"):
            parse_suggestions(":\n: : :")

    def test_empty_input_raises(self) -> None:
        with pytest.raises(SuggestionParseError, match="empty"):
            parse_suggestions("")

    def test_non_mapping_top_level_raises(self) -> None:
        # The LLM might output a bare list. We want a clear error
        # rather than "no candidates field found".
        with pytest.raises(SuggestionParseError, match="mapping"):
            parse_suggestions("- name: customer")

    def test_missing_candidates_key_raises(self) -> None:
        with pytest.raises(SuggestionParseError, match="candidates"):
            parse_suggestions("other_key: foo")

    def test_candidates_not_a_list_raises(self) -> None:
        with pytest.raises(SuggestionParseError, match="candidates"):
            parse_suggestions("candidates: not-a-list")

    def test_unknown_top_level_key_raises(self) -> None:
        # Strict-keys policy — same shape as yaml_grammar's strict mode.
        # An unknown top-level field is almost always an LLM hallucination
        # we shouldn't silently absorb.
        text = """
candidates: []
metadata:
  llm_version: foo
"""
        with pytest.raises(SuggestionParseError, match="unknown top-level"):
            parse_suggestions(text)


# ----- per-candidate errors --------------------------------------------------


class TestParseSuggestionsCandidateErrors:
    def test_candidate_not_a_mapping_raises(self) -> None:
        text = """
candidates:
  - "just a string"
"""
        with pytest.raises(SuggestionParseError, match="mapping"):
            parse_suggestions(text)

    def test_candidate_missing_name_raises(self) -> None:
        text = """
candidates:
  - binding:
      single_table: public.users
    identity: id
    confidence: high
    rationale: r
    pii_hints: {}
"""
        with pytest.raises(SuggestionParseError, match="name"):
            parse_suggestions(text)

    def test_candidate_missing_binding_raises(self) -> None:
        text = """
candidates:
  - name: customer
    identity: id
    confidence: high
    rationale: r
    pii_hints: {}
"""
        with pytest.raises(SuggestionParseError, match="binding"):
            parse_suggestions(text)

    def test_candidate_missing_identity_raises(self) -> None:
        text = """
candidates:
  - name: customer
    binding:
      single_table: public.users
    confidence: high
    rationale: r
    pii_hints: {}
"""
        with pytest.raises(SuggestionParseError, match="identity"):
            parse_suggestions(text)

    def test_candidate_missing_confidence_raises(self) -> None:
        text = """
candidates:
  - name: customer
    binding:
      single_table: public.users
    identity: id
    rationale: r
    pii_hints: {}
"""
        with pytest.raises(SuggestionParseError, match="confidence"):
            parse_suggestions(text)

    def test_candidate_invalid_confidence_value_raises(self) -> None:
        text = """
candidates:
  - name: customer
    binding:
      single_table: public.users
    identity: id
    confidence: super-high
    rationale: r
    pii_hints: {}
"""
        with pytest.raises(SuggestionParseError, match="confidence"):
            parse_suggestions(text)

    def test_candidate_invalid_name_shape_raises(self) -> None:
        text = """
candidates:
  - name: "1bad name"
    binding:
      single_table: public.users
    identity: id
    confidence: high
    rationale: r
    pii_hints: {}
"""
        with pytest.raises(SuggestionParseError, match="name"):
            parse_suggestions(text)

    def test_candidate_invalid_binding_shape_raises(self) -> None:
        text = """
candidates:
  - name: customer
    binding:
      single_table: not-qualified
    identity: id
    confidence: high
    rationale: r
    pii_hints: {}
"""
        with pytest.raises(SuggestionParseError, match="binding"):
            parse_suggestions(text)

    def test_candidate_multi_table_binding_rejected(self) -> None:
        # multi_table is a v2 concern — same rejection shape as the
        # canonical entity-yaml parser, surfaced through the suggest
        # parser too. If the LLM proposes a multi-table entity, we
        # refuse rather than silently coerce.
        text = """
candidates:
  - name: customer
    binding:
      multi_table: [public.users, public.profiles]
    identity: id
    confidence: high
    rationale: r
    pii_hints: {}
"""
        with pytest.raises(SuggestionParseError, match="multi_table"):
            parse_suggestions(text)

    def test_candidate_unknown_key_raises(self) -> None:
        text = """
candidates:
  - name: customer
    binding:
      single_table: public.users
    identity: id
    confidence: high
    rationale: r
    pii_hints: {}
    bogus_field: 42
"""
        with pytest.raises(SuggestionParseError, match="unknown"):
            parse_suggestions(text)

    def test_candidate_invalid_pii_hint_value_raises(self) -> None:
        text = """
candidates:
  - name: customer
    binding:
      single_table: public.users
    identity: id
    confidence: high
    rationale: r
    pii_hints:
      email: super_secret
"""
        with pytest.raises(SuggestionParseError, match="pii_hints"):
            parse_suggestions(text)

    def test_candidate_pii_hints_not_a_mapping_raises(self) -> None:
        text = """
candidates:
  - name: customer
    binding:
      single_table: public.users
    identity: id
    confidence: high
    rationale: r
    pii_hints: not-a-dict
"""
        with pytest.raises(SuggestionParseError, match="pii_hints"):
            parse_suggestions(text)

    def test_candidate_non_string_name_raises(self) -> None:
        # YAML will happily parse `name: 42` as an int. Reject before
        # it reaches Entity's regex check so the error message names
        # the type clearly.
        text = """
candidates:
  - name: 42
    binding:
      single_table: public.users
    identity: id
    confidence: high
    rationale: r
    pii_hints: {}
"""
        with pytest.raises(SuggestionParseError, match="name must be a string"):
            parse_suggestions(text)

    def test_candidate_empty_name_raises(self) -> None:
        # Quoted empty string — distinct from "missing" (caught by
        # required-keys check) and from "wrong type" (caught above).
        text = """
candidates:
  - name: ""
    binding:
      single_table: public.users
    identity: id
    confidence: high
    rationale: r
    pii_hints: {}
"""
        with pytest.raises(SuggestionParseError, match="non-empty"):
            parse_suggestions(text)

    def test_candidate_non_string_description_raises(self) -> None:
        # description is optional — when present, it must be a string.
        text = """
candidates:
  - name: customer
    description: 42
    binding:
      single_table: public.users
    identity: id
    confidence: high
    rationale: r
    pii_hints: {}
"""
        with pytest.raises(SuggestionParseError, match="description"):
            parse_suggestions(text)

    def test_candidate_non_string_confidence_raises(self) -> None:
        # `confidence: 5` would otherwise reach the Literal check with
        # the int 5 — and `5 not in {"high", "medium", "low"}` is True,
        # so the message would be confusing. Type check first.
        text = """
candidates:
  - name: customer
    binding:
      single_table: public.users
    identity: id
    confidence: 5
    rationale: r
    pii_hints: {}
"""
        with pytest.raises(SuggestionParseError, match="confidence must be a string"):
            parse_suggestions(text)

    def test_candidate_binding_not_a_mapping_raises(self) -> None:
        text = """
candidates:
  - name: customer
    binding: "public.users"
    identity: id
    confidence: high
    rationale: r
    pii_hints: {}
"""
        with pytest.raises(SuggestionParseError, match="binding must be a mapping"):
            parse_suggestions(text)

    def test_candidate_binding_empty_mapping_raises(self) -> None:
        text = """
candidates:
  - name: customer
    binding: {}
    identity: id
    confidence: high
    rationale: r
    pii_hints: {}
"""
        with pytest.raises(SuggestionParseError, match="empty mapping"):
            parse_suggestions(text)

    def test_candidate_binding_unknown_shape_raises(self) -> None:
        # A shape we don't recognise — neither single_table nor
        # multi_table. Same defensive ethos as yaml_grammar.
        text = """
candidates:
  - name: customer
    binding:
      view: public.user_view
    identity: id
    confidence: high
    rationale: r
    pii_hints: {}
"""
        with pytest.raises(SuggestionParseError, match="unknown shape"):
            parse_suggestions(text)

    def test_candidate_binding_single_table_non_string_raises(self) -> None:
        # `single_table: 42` — type check before regex check.
        text = """
candidates:
  - name: customer
    binding:
      single_table: 42
    identity: id
    confidence: high
    rationale: r
    pii_hints: {}
"""
        with pytest.raises(SuggestionParseError, match="must be a string"):
            parse_suggestions(text)

    def test_candidate_pii_hints_non_string_key_raises(self) -> None:
        # YAML allows non-string mapping keys (e.g. `42: pii`). Our
        # contract is column-name strings, so reject other shapes.
        text = """
candidates:
  - name: customer
    binding:
      single_table: public.users
    identity: id
    confidence: high
    rationale: r
    pii_hints:
      42: pii
"""
        with pytest.raises(SuggestionParseError, match="pii_hints keys"):
            parse_suggestions(text)
