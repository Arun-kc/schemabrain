"""Doc-truth regression pins (v0.5.0 pre-publish doc sweep, PR-F).

Guards two classes of documentation drift that shipped silently before
the sweep:

  1. Store-path default. The real CLI default is ``./schemabrain.db``
     (``schemabrain/cli.py`` ``_DEFAULT_STORE_PATH``), but ~20 docs
     claimed ``~/.schemabrain/store.db``. ADRs (immutable point-in-time
     records) and the gitignored ``docs/internal/`` tree are excluded;
     the events file genuinely lives at ``~/.schemabrain/events.jsonl``
     and the Docker ``-v ~/.schemabrain:/data`` convention is correct,
     so the pin targets only the two WRONG store literals.

  2. PII-enforcement wording. The catastrophic-leak floor
     (``credential`` / ``payment_card`` / ``government_id``) is
     ALWAYS-ON: neither ``block: []`` nor ``--pii-block ''`` nor
     omitting the flag can disable it (every read gate unions the floor
     — see ``describe_table.py`` / ``describe_column.py`` /
     ``get_metric``). Two safety docs previously claimed otherwise.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOCS = _REPO_ROOT / "docs"

# The OLD (wrong) store-path literals. The real default is
# ``./schemabrain.db``. ``~/.schemabrain/events.jsonl`` and the bare
# ``~/.schemabrain/`` directory are NOT in this list — those are
# legitimate.
_WRONG_STORE_LITERALS = ("~/.schemabrain/store.db", "~/.schemabrain.db")

# ADRs are immutable records; ``internal/`` is gitignored.
_EXCLUDED_TOP_DIRS = frozenset({"adr", "internal"})


def _committed_doc_files() -> list[Path]:
    out: list[Path] = []
    for path in _DOCS.rglob("*"):
        if path.suffix not in {".md", ".mdx"}:
            continue
        rel = path.relative_to(_DOCS)
        if rel.parts and rel.parts[0] in _EXCLUDED_TOP_DIRS:
            continue
        out.append(path)
    return out


@pytest.mark.parametrize("wrong", _WRONG_STORE_LITERALS)
def test_no_committed_doc_uses_wrong_store_path_default(wrong: str) -> None:
    offenders = [
        str(p.relative_to(_REPO_ROOT))
        for p in _committed_doc_files()
        if wrong in p.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"{wrong!r} is the OLD store-path default; the real default is "
        f"./schemabrain.db (cli.py _DEFAULT_STORE_PATH). Update: {offenders}"
    )


def test_cli_overview_documents_correct_store_default() -> None:
    text = (_DOCS / "reference/cli/overview.mdx").read_text(encoding="utf-8")
    assert "./schemabrain.db" in text


# A positive claim that PII enforcement can be turned off: "disable(s)
# [up to 3 words] enforcement". The catastrophic-leak floor is always-on,
# so any such claim is false — scanned repo-wide, not per-file, so the
# overclaim can't survive in an un-checked doc (serve.mdx / init.mdx did).
_ENFORCEMENT_OVERCLAIM = re.compile(r"disabl\w*(?:\s+\S+){0,3}\s+enforcement", re.IGNORECASE)
# Correct, NON-disablable phrasings that legitimately contain the words
# above. Markdown emphasis (`**`) is stripped before this check.
_CORRECT_NEGATED_FORMS = ("not disable", "cannot be disabled", "cannot disable")


def test_no_committed_doc_claims_pii_enforcement_can_be_disabled() -> None:
    offenders: list[str] = []
    for path in _committed_doc_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not _ENFORCEMENT_OVERCLAIM.search(line):
                continue
            normalized = line.lower().replace("*", "")
            if any(form in normalized for form in _CORRECT_NEGATED_FORMS):
                continue
            offenders.append(f"{path.relative_to(_REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Doc(s) claim PII enforcement can be disabled, but the catastrophic-leak "
        "floor (credential/payment_card/government_id) is ALWAYS-ON — every serve "
        "resolution and read-gate unions it (cli.py + describe_*/get_metric "
        "effective_block). Reword to 'clears the operator policy; the floor still "
        "refuses'. Offenders:\n  " + "\n  ".join(offenders)
    )


def test_safety_docs_use_floor_accurate_wording() -> None:
    """The corrected always-on-floor framing must be present (not just the
    false claims absent), so a future edit can't quietly drop it."""
    pii_policy = (_DOCS / "pii-policy.md").read_text(encoding="utf-8")
    assert "always-on" in pii_policy
    for cat in ("credential", "payment_card", "government_id"):
        assert cat in pii_policy, f"floor category {cat!r} missing from pii-policy.md"
    observability = (_DOCS / "observability.md").read_text(encoding="utf-8")
    assert "always-on catastrophic-leak floor" in observability
