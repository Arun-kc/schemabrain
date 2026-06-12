"""Test isolation for the setup suite.

Several tests here invoke `schemabrain.cli._dispatch()` / `main()` (e.g.
`test_demo.py`, the init tests), which call `configure_logging` — a
function that sets the `schemabrain` package logger's ``propagate =
False`` and swaps in a stderr handler. That is a global, process-wide
side effect. Because this directory sorts before
``tests/test_check_engine.py`` in pytest's collection order, leaking
``propagate=False`` would break later ``caplog``-based assertions on
``schemabrain.*`` loggers (which need propagation enabled to be captured
by the root-attached caplog handler).

Snapshot + restore the package logger around every test so the suite is
order-independent and never pollutes the rest of the run. Mirrors
``tests/datadict/conftest.py`` (same footgun, same fix).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

_PACKAGE_LOGGER = "schemabrain"


@pytest.fixture(autouse=True)
def _restore_schemabrain_logging() -> Iterator[None]:
    logger = logging.getLogger(_PACKAGE_LOGGER)
    saved_propagate = logger.propagate
    saved_level = logger.level
    saved_handlers = logger.handlers[:]
    try:
        yield
    finally:
        logger.propagate = saved_propagate
        logger.level = saved_level
        logger.handlers[:] = saved_handlers
