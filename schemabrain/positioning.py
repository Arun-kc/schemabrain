"""Canonical positioning constants — the single source of truth for how
SchemaBrain describes itself.

Every surface that states *what SchemaBrain is* reads from here so the framing
can never drift out of sync:

* package metadata — ``pyproject.toml`` ``project.description`` (propagated by
  ``scripts/sync_positioning.py``; CI runs ``--check``),
* the package docstring (``schemabrain.__doc__``),
* the CLI ``--help`` banner (``cli._build_parser``),
* the MCP server instructions lead sentence (``mcp/server.py``),
* the dashboard (mirrored into ``web/lib/positioning.ts``, guarded by the
  TS↔Python parity test ``tests/dashboard/test_positioning_parity.py``),
* the docs / README (swept by ``scripts/sync_positioning.py`` in later passes).

Positioning thesis (locked 2026-05-31): SchemaBrain is the **trust and
intelligence layer** between AI agents and your database — the way Cloudflare
sits between the internet and your servers. The SQL-firewall guarantee is *one
proof-point of six*, never the headline: it's safe because it's smart.

Honesty contract
----------------
"Inspection" here always means inspecting **your schema** — index-time PII
classification + schema resolution, surfaced by the read-only ``schemabrain
inspect`` command. It NEVER means inspecting, parsing, or proxying the SQL an
agent writes: the agent never writes raw SQL at all; SchemaBrain compiles it
from definitions you control. The mental-model "inspection" stage is therefore
always paired with its gloss (:data:`INSPECTION_GLOSS`). Copy that implies
traffic inspection or agent-SQL parsing is banned and enforced by
``tests/test_positioning.py::test_no_banned_phrases``.
"""

from __future__ import annotations

from typing import NamedTuple

#: Hero / one-line positioning. Doubles as the package + PyPI description.
TAGLINE = "The trust and intelligence layer between AI agents and your database."

#: ``pyproject.toml`` ``project.description`` + package docstring + CLI banner.
#: Kept identical to :data:`TAGLINE` so there is exactly one canonical sentence.
SHORT_DESCRIPTION = TAGLINE

#: The "why it exists" sentence — the Cloudflare analogy.
MISSION = (
    "Like Cloudflare sits between the internet and your servers, SchemaBrain "
    "sits between AI agents and your database — making your schema "
    "understandable, keeping access safe, and keeping every query trustworthy."
)

#: The behavioral invariant, stated positively. The agent never authors SQL.
DEF_DRIVEN_CONTRACT = (
    "The agent never writes raw SQL; SchemaBrain compiles it from definitions you control."
)

#: "inspection" is schema inspection, never agent-SQL inspection. The
#: mental-model "inspection" stage is always shown paired with this gloss.
INSPECTION_GLOSS = "classify PII · resolve schema"


class Stage(NamedTuple):
    """One stage of the trust-boundary pipeline (the four-layer mental model)."""

    name: str
    gloss: str


#: The four-stage pipeline a request flows through inside the trust boundary:
#: AI AGENT → inspection → trust → execution → intelligence → YOUR DATABASE.
#: Stage 01 "inspection" is glossed as SCHEMA inspection (never agent SQL).
MENTAL_MODEL: tuple[Stage, ...] = (
    Stage("inspection", INSPECTION_GLOSS),
    Stage("trust", "policy · refusal · audit"),
    Stage("execution", "compile read-only SQL"),
    Stage("intelligence", "semantic · graph · RAG"),
)


class ProofPoint(NamedTuple):
    """One safety proof-point. ``is_floor`` marks the non-negotiable floor."""

    title: str
    detail: str
    is_floor: bool = False


#: The six safety proof-points. The SQL firewall is the LAST entry — "one
#: guarantee among many," never the headline. The catastrophic floor is the
#: only ``is_floor`` member.
PROOF_POINTS: tuple[ProofPoint, ...] = (
    ProofPoint(
        "PII classification",
        "Twelve sensitivity categories, classified per column with a confidence band.",
    ),
    ProofPoint(
        "Catastrophic floor",
        "Credentials, government IDs, and card data are hard-blocked, regardless of policy.",
        is_floor=True,
    ),
    ProofPoint(
        "Editable policy",
        "Tag or clear any column; preview the diff before it applies.",
    ),
    ProofPoint(
        "Tamper-evident audit",
        "Every query writes a SHA-256 hash-chained row. Verify the chain in milliseconds.",
    ),
    ProofPoint(
        "Def-driven compilation",
        "Agents never write SQL — it is compiled from definitions you own.",
    ),
    ProofPoint(
        "SQL firewall",
        "Read-only connections, statement timeouts, and row caps — one guarantee among many.",
    ),
)

#: Canonical install ladder. Primary is uvx; fallbacks keep pipx/pip and the
#: ``[ui]`` extra. No Node toolchain is ever required to run SchemaBrain.
INSTALL_PRIMARY = "uvx schemabrain init"
INSTALL_FALLBACKS: tuple[str, ...] = (
    "pipx install schemabrain",
    "pip install schemabrain",
    "pip install 'schemabrain[ui]'",
)

#: SPDX license label. Apache-2.0 since the 2026-06-02 relicense (PR #179).
LICENSE_LABEL = "Apache-2.0"
