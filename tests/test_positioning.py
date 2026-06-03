"""Guards for the canonical positioning constants (`schemabrain.positioning`).

These tests pin the *honesty* and *structure* of the public framing so a
well-meaning copy edit can't silently re-introduce the "SQL firewall is the
headline" positioning or imply traffic / agent-SQL inspection.
"""

from __future__ import annotations

import schemabrain
from schemabrain import positioning as P

# Phrases that would imply SchemaBrain inspects/parses/proxies the SQL an agent
# writes. It does not — the agent never writes raw SQL; SchemaBrain compiles it
# from definitions. "inspection" alone is allowed because it always means SCHEMA
# inspection and is shown paired with INSPECTION_GLOSS. Apostrophe variants
# (straight + curly) are both listed.
BANNED_PHRASES: tuple[str, ...] = (
    "traffic inspection",
    "inspect the agent's sql",
    "inspect the agent\u2019s sql",  # curly-apostrophe variant
    "parse the agent's sql",
    "parse the agent\u2019s sql",  # curly-apostrophe variant
    "inspect agent sql",
    "inspects agent sql",
    "inspecting agent sql",
    "inspect the sql the agent",
)


def _all_canonical_text() -> list[str]:
    """Every human-facing string exported by the module (auto-discovers new
    public constants so the banned-phrase gate keeps covering them)."""
    texts: list[str] = []
    for name in dir(P):
        if name.startswith("_"):
            continue
        value = getattr(P, name)
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, tuple):
            for item in value:
                if isinstance(item, str):
                    texts.append(item)
                elif isinstance(item, tuple):  # NamedTuple rows
                    texts.extend(field for field in item if isinstance(field, str))
    return texts


def test_no_banned_phrases() -> None:
    """No canonical string may imply agent-SQL or traffic inspection."""
    haystack = " ⊕ ".join(_all_canonical_text()).lower()
    offenders = [phrase for phrase in BANNED_PHRASES if phrase in haystack]
    assert not offenders, (
        f"banned positioning phrase(s) present: {offenders}. "
        "SchemaBrain compiles SQL from definitions; it never inspects/parses the "
        "agent's SQL or proxies traffic."
    )


def test_tagline_is_trust_and_intelligence() -> None:
    assert "trust and intelligence layer" in P.TAGLINE.lower()
    assert "firewall" not in P.TAGLINE.lower()


def test_short_description_is_the_single_canonical_sentence() -> None:
    # One canonical sentence powers PyPI, the package docstring, and the CLI banner.
    assert P.SHORT_DESCRIPTION == P.TAGLINE
    assert "firewall" not in P.SHORT_DESCRIPTION.lower()


def test_mission_uses_the_cloudflare_analogy() -> None:
    assert "cloudflare" in P.MISSION.lower()


def test_mental_model_is_the_four_stage_pipeline() -> None:
    names = [stage.name for stage in P.MENTAL_MODEL]
    assert names == ["inspection", "trust", "execution", "intelligence"]


def test_inspection_stage_is_glossed_as_schema_inspection() -> None:
    inspection = P.MENTAL_MODEL[0]
    assert inspection.name == "inspection"
    # The gloss earns the word: it means classifying PII and resolving the
    # schema, never inspecting the agent's SQL.
    assert inspection.gloss == P.INSPECTION_GLOSS == "classify PII · resolve schema"


def test_def_driven_contract_is_stated_positively() -> None:
    contract = P.DEF_DRIVEN_CONTRACT.lower()
    assert "never write" in contract
    assert "definitions" in contract


def test_six_proof_points_with_firewall_last_and_not_the_headline() -> None:
    assert len(P.PROOF_POINTS) == 6
    # The firewall is demoted to the final proof-point — "one guarantee among many".
    last = P.PROOF_POINTS[-1]
    assert "firewall" in last.title.lower()
    assert "one guarantee among many" in last.detail.lower()


def test_exactly_one_catastrophic_floor_proof_point() -> None:
    floors = [pp for pp in P.PROOF_POINTS if pp.is_floor]
    assert len(floors) == 1
    assert "floor" in floors[0].title.lower()


def test_install_ladder_is_uvx_first_with_no_node_dependency() -> None:
    assert P.INSTALL_PRIMARY == "uvx schemabrain init"
    joined = " ".join(P.INSTALL_FALLBACKS).lower()
    assert "pipx install schemabrain" in joined
    assert "schemabrain[ui]" in joined
    assert "npm" not in joined and "node" not in joined


def test_license_label_is_apache() -> None:
    assert P.LICENSE_LABEL == "Apache-2.0"


def test_package_docstring_matches_short_description() -> None:
    # `schemabrain.__doc__` is a literal (a module docstring can't import), so
    # this equality is its drift guard — the analogue of the pyproject sweep.
    assert schemabrain.__doc__ == P.SHORT_DESCRIPTION


def test_cli_banner_uses_canonical_short_description() -> None:
    from schemabrain.cli import _build_parser

    assert _build_parser().description == P.SHORT_DESCRIPTION


def test_mcp_server_instructions_lead_is_trust_and_intelligence() -> None:
    # Only the lead sentence is repositioned; the behavioral contract
    # ("never write raw SQL") and the tool call-order stay intact.
    from schemabrain.mcp.server import _SERVER_INSTRUCTIONS

    assert _SERVER_INSTRUCTIONS.startswith(
        "SchemaBrain — the trust and intelligence layer between AI agents"
    )
    assert "You never write raw SQL" in _SERVER_INSTRUCTIONS
    lead = _SERVER_INSTRUCTIONS.split(".")[0].lower()
    assert "firewall" not in lead
