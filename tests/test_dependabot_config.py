"""Tests for `.github/dependabot.yml`.

Pins the structural shape so a careless edit can't drop an ecosystem
(pip or github-actions), drop the security-update group (which would
delay CVE patches), or push the open-PR limit to a number that
generates a PR storm.

We don't run Dependabot here — these tests are pure config-shape
assertions. The cost is ~1 KB of test code; the payoff is catching a
broken config before it goes live and silently drops security updates
for a week.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEPENDABOT_YML = _REPO_ROOT / ".github" / "dependabot.yml"


@pytest.fixture(scope="module")
def config() -> dict:
    return yaml.safe_load(_DEPENDABOT_YML.read_text())


class TestDependabotConfig:
    def test_file_exists(self) -> None:
        # `.github/dependabot.yml` is the only path Dependabot reads.
        # If a contributor moves it (e.g. to docs/), Dependabot silently
        # stops running with no UI signal.
        assert _DEPENDABOT_YML.exists()

    def test_uses_schema_version_2(self, config: dict) -> None:
        # Schema v1 has been deprecated since 2019. v2 is the only
        # version GitHub honours today.
        assert config["version"] == 2

    def test_tracks_pip_ecosystem(self, config: dict) -> None:
        # Pip ecosystem covers `pyproject.toml`'s dependencies + optional
        # extras. Without this entry, runtime deps drift silently.
        ecosystems = [u["package-ecosystem"] for u in config["updates"]]
        assert "pip" in ecosystems

    def test_tracks_github_actions_ecosystem(self, config: dict) -> None:
        # Pinned action versions in `.github/workflows/*` only update
        # via Dependabot. Drop this and a vulnerable action could ship
        # in CI for months.
        ecosystems = [u["package-ecosystem"] for u in config["updates"]]
        assert "github-actions" in ecosystems

    @pytest.mark.parametrize("ecosystem", ["pip", "github-actions"])
    def test_every_ecosystem_has_weekly_schedule(self, config: dict, ecosystem: str) -> None:
        # Daily is too noisy for a solo project; monthly is too slow
        # for security patches. Weekly is the well-tested middle ground.
        entry = next(u for u in config["updates"] if u["package-ecosystem"] == ecosystem)
        assert entry["schedule"]["interval"] == "weekly"

    @pytest.mark.parametrize("ecosystem", ["pip", "github-actions"])
    def test_every_ecosystem_has_open_pr_limit(self, config: dict, ecosystem: str) -> None:
        # An unbounded queue can balloon to 30+ PRs during a review
        # gap. Pin a sane upper bound; the precise value isn't important
        # but the presence of A bound is.
        entry = next(u for u in config["updates"] if u["package-ecosystem"] == ecosystem)
        limit = entry["open-pull-requests-limit"]
        assert isinstance(limit, int)
        assert 1 <= limit <= 10

    def test_pip_ecosystem_groups_security_updates(self, config: dict) -> None:
        # The whole reason Dependabot exists for this project is to ship
        # CVE patches fast. If the `python-security` group is dropped,
        # security updates fall back to ungrouped behaviour. Still works,
        # but our explicit "security gets its own group" intent is gone.
        pip = next(u for u in config["updates"] if u["package-ecosystem"] == "pip")
        groups = pip.get("groups", {})
        assert "python-security" in groups
        sec_group = groups["python-security"]
        assert sec_group.get("applies-to") == "security-updates"

    def test_pip_ecosystem_groups_minor_and_patch(self, config: dict) -> None:
        # Per the comment in dependabot.yml: minor+patch are grouped to
        # avoid PR storms. Major updates remain individual PRs by
        # default (no group covers them).
        pip = next(u for u in config["updates"] if u["package-ecosystem"] == "pip")
        groups = pip.get("groups", {})
        assert "python-minor-and-patch" in groups
        types = groups["python-minor-and-patch"]["update-types"]
        assert set(types) == {"minor", "patch"}
