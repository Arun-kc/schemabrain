"""Schema Brain: the SQL firewall between AI agents and your production database."""

from importlib.metadata import version

# Read directly from the installed package metadata so `pyproject.toml`
# is the single source of truth for the version literal. Bumping the
# version in one place (pyproject) automatically propagates to
# `schemabrain.__version__` and `schemabrain --version`.
__version__ = version("schemabrain")
