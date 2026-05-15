"""Tests for the project's `SECURITY.md` disclosure policy.

These pin the structural shape of the policy so a careless edit can't
silently drop the contact channels documented to external researchers.
Treat additions as additive and removals as breaking — anyone holding a
URL like `/security/advisories/new` may have linked it from a write-up.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SECURITY_MD = _REPO_ROOT / "SECURITY.md"


class TestSecurityPolicy:
    """Structural pins on `SECURITY.md`.

    The policy lives at the repo root (GitHub's expected location) so the
    Security tab auto-detects it. Tests are deliberately narrow: shape
    over wording, contact channels over copy editing.
    """

    def _read(self) -> str:
        return _SECURITY_MD.read_text()

    def test_file_exists_at_repo_root(self) -> None:
        # GitHub auto-detects SECURITY.md at the repo root, in `docs/`,
        # or in `.github/`. Pin the chosen location so a "tidy-up" PR
        # doesn't accidentally move it somewhere GitHub stops finding.
        assert _SECURITY_MD.exists()
        assert _SECURITY_MD.is_file()

    def test_advertises_github_private_vulnerability_reporting(self) -> None:
        # GitHub PVR is the preferred channel — it creates a private
        # advisory thread visible only to maintainers and produces a CVE
        # link if escalated. A working URL beats prose every time.
        text = self._read()
        assert "security/advisories/new" in text

    def test_advertises_email_fallback(self) -> None:
        # If GitHub is down or the reporter doesn't have a GitHub account,
        # email is the backup channel. Pin the actual address so a
        # search-and-replace can't silently drop it.
        assert "arunkc91@gmail.com" in self._read()

    def test_states_acknowledgement_sla(self) -> None:
        # An external researcher needs to know when to escalate. Pin the
        # presence of an explicit acknowledgement SLA (7 days today).
        text = self._read()
        assert "7" in text and "days" in text.lower()

    @pytest.mark.parametrize(
        "phrase",
        [
            "Supported Versions",
            "Reporting",
            "Coordinated Disclosure",
            "In Scope",
            "Out of Scope",
        ],
    )
    def test_required_sections_present(self, phrase: str) -> None:
        # The five sections an external reader expects to find on a
        # serious security policy. If a contributor restructures the
        # doc and drops one, this fires before the policy goes live
        # with a hole in it.
        assert phrase in self._read(), f"SECURITY.md missing required section: {phrase!r}"

    def test_does_not_promise_a_bug_bounty(self) -> None:
        # Solo project, no budget for bounties. Promising one we can't
        # honour would be worse than not having one. Pin the explicit
        # disclaimer so a future doc edit doesn't accidentally imply
        # otherwise.
        assert "not currently offer a bug bounty" in self._read()
