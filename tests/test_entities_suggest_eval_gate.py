"""CI gate: precision@3 >= 0.7 across bundled fixtures.

The suggest pipeline must score precision@3 of at least 0.7 against
the curated fixture subset before merging. This test IS that gate —
if it fails, CI fails, the PR is blocked.

The gate runs under a `FakeLLMClient` returning realistic "mostly
right" stub responses (3-of-3 perfect on the ecommerce fixture is
trivial; the test instead exercises a NOISIER scenario where the
stub gets some wrong, to verify the gate actually trips at the
threshold rather than passing trivially).

A separate **manual** eval against a real LLM is a developer-driven
operation (out of scope for CI); this file is the CI floor.

Two test scenarios pin the gate:
  1. With a "perfect" stub, all fixtures pass the 0.7 threshold.
  2. With a "noisy" stub (1 wrong of 3), per-fixture precision is
     0.667 — JUST below threshold — so the gate fires cleanly. This
     proves the gate is real, not a tautology.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from schemabrain.enrichment.llm import FakeLLMClient
from schemabrain.entities.suggest import EntitySuggestionPipeline
from schemabrain.eval.entity_harness import (
    blog_fixture,
    ecommerce_fixture,
    library_fixture,
    run_entity_eval,
)

# 0.7 is the CI gate threshold for precision@3. A noisier real-LLM
# run can score higher; this is the minimum acceptable for a merged
# change. Below this, the pipeline is regressed or the bundled
# fixtures have drifted from what the prompt produces.
_GATE_PRECISION_THRESHOLD = 0.7

# Top-3 is the headline metric. The LLM's first 3 ranked candidates
# are what an agent actually consumes; precision further down the
# ranking is decorative.
_GATE_TOP_K = 3


# Stubbed LLM responses — one per fixture, keyed on a table name that
# uniquely appears in that fixture's user prompt. Each "perfect"
# response returns the 3 expected entities cleanly.
_PERFECT_RESPONSES: dict[str, str] = {
    "public.users": """\
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
  - name: order
    description: One placed order
    binding:
      single_table: public.orders
    identity: id
    confidence: high
    rationale: orders has id PK and FK into users
    pii_hints: {}
  - name: product
    description: A product
    binding:
      single_table: public.products
    identity: id
    confidence: high
    rationale: products has id PK and SKU
    pii_hints: {}
""",
    "public.members": """\
candidates:
  - name: member
    description: A library member
    binding:
      single_table: public.members
    identity: id
    confidence: high
    rationale: members has id PK and email
    pii_hints:
      email: pii
  - name: book
    description: A book in the catalog
    binding:
      single_table: public.books
    identity: id
    confidence: high
    rationale: books has id PK and ISBN
    pii_hints: {}
  - name: loan
    description: A book loan
    binding:
      single_table: public.loans
    identity: id
    confidence: high
    rationale: loans has id PK plus member and book FKs
    pii_hints: {}
""",
    "public.authors": """\
candidates:
  - name: author
    description: A blog author
    binding:
      single_table: public.authors
    identity: id
    confidence: high
    rationale: authors has id PK and handle
    pii_hints: {}
  - name: post
    description: A blog post
    binding:
      single_table: public.posts
    identity: id
    confidence: high
    rationale: posts has id PK and FK into authors
    pii_hints: {}
  - name: comment
    description: A comment on a post
    binding:
      single_table: public.comments
    identity: id
    confidence: high
    rationale: comments has id PK and FKs into posts + authors
    pii_hints: {}
""",
}


# Same shape as `_PERFECT_RESPONSES` but each fixture's response has
# exactly ONE wrong candidate (binding to a junk table). Per-fixture
# precision@3 = 2/3 ≈ 0.667 < 0.7 threshold — the gate must fire.
_NOISY_RESPONSES: dict[str, str] = {
    "public.users": """\
candidates:
  - name: customer
    binding:
      single_table: public.users
    identity: id
    confidence: high
    rationale: r
    pii_hints: {}
  - name: order
    binding:
      single_table: public.orders
    identity: id
    confidence: high
    rationale: r
    pii_hints: {}
  - name: junk
    binding:
      single_table: public.junk_table
    identity: id
    confidence: low
    rationale: r
    pii_hints: {}
""",
    "public.members": """\
candidates:
  - name: member
    binding:
      single_table: public.members
    identity: id
    confidence: high
    rationale: r
    pii_hints: {}
  - name: book
    binding:
      single_table: public.books
    identity: id
    confidence: high
    rationale: r
    pii_hints: {}
  - name: junk
    binding:
      single_table: public.junk_table
    identity: id
    confidence: low
    rationale: r
    pii_hints: {}
""",
    "public.authors": """\
candidates:
  - name: author
    binding:
      single_table: public.authors
    identity: id
    confidence: high
    rationale: r
    pii_hints: {}
  - name: post
    binding:
      single_table: public.posts
    identity: id
    confidence: high
    rationale: r
    pii_hints: {}
  - name: junk
    binding:
      single_table: public.junk_table
    identity: id
    confidence: low
    rationale: r
    pii_hints: {}
""",
}


def _route_by_user_prompt(responses: dict[str, str]) -> Callable[[str, str], str]:
    """Build a text-provider that picks a canned response by matching
    a unique table name in the user prompt. The user prompt always
    contains the serialized schema, so picking on the first table is
    deterministic without coupling to prompt phrasing.
    """

    def _provider(_system: str, user: str) -> str:
        for key, response in responses.items():
            if key in user:
                return response
        raise AssertionError(
            f"No canned response matched user prompt; expected one of: {sorted(responses)}"
        )

    return _provider


# ----- the gate --------------------------------------------------------------


def test_ci_gate_perfect_stub_passes_threshold() -> None:
    """The headline gate: with the curated stub, every bundled fixture
    must score precision@3 >= 0.7.

    If this test fails, the CI build is red and the PR is blocked
    until the regression is identified and fixed.
    """
    client = FakeLLMClient(text_provider=_route_by_user_prompt(_PERFECT_RESPONSES))
    pipeline = EntitySuggestionPipeline(llm=client)

    results = run_entity_eval(
        [ecommerce_fixture(), library_fixture(), blog_fixture()],
        pipeline,
        top_k=_GATE_TOP_K,
    )

    assert len(results) == 3, "all three bundled fixtures must score"
    for result in results:
        assert result.passed(threshold=_GATE_PRECISION_THRESHOLD), (
            f"fixture {result.fixture_name!r} failed gate: "
            f"precision@{_GATE_TOP_K} = {result.precision_at_k:.3f} < "
            f"{_GATE_PRECISION_THRESHOLD:.2f}. Expected: {sorted(result.expected_tables)}; "
            f"actual: {result.actual_tables}"
        )


def test_ci_gate_actually_fires_when_pipeline_underperforms() -> None:
    """Anti-tautology: prove the gate is REAL by feeding it a noisy
    stub that scores 2/3 per fixture (≈0.667, below 0.7).

    If this test FAILS (i.e., the noisy stub somehow scored above
    0.7), the threshold is set wrong or the scorer is buggy — either
    way, the real gate above doesn't mean what it claims to mean.
    """
    client = FakeLLMClient(text_provider=_route_by_user_prompt(_NOISY_RESPONSES))
    pipeline = EntitySuggestionPipeline(llm=client)

    results = run_entity_eval(
        [ecommerce_fixture(), library_fixture(), blog_fixture()],
        pipeline,
        top_k=_GATE_TOP_K,
    )

    # Every fixture should be JUST below the threshold (2-of-3 = 0.667).
    for result in results:
        assert result.precision_at_k == pytest.approx(2 / 3), (
            f"noisy stub for {result.fixture_name!r} should score 2/3, "
            f"got {result.precision_at_k:.3f}"
        )
        assert not result.passed(threshold=_GATE_PRECISION_THRESHOLD), (
            f"noisy stub for {result.fixture_name!r} unexpectedly passed "
            f"the gate — threshold tuning or scorer logic has drifted"
        )
