"""TypeScript ↔ Python positioning parity gate.

Asserts that the canonical positioning strings the dashboard/landing consumes
(`web/lib/positioning.ts`) are byte-identical to the Python single source of
truth (`schemabrain.positioning`). A contributor who edits the tagline / install
command / license label on one side without mirroring the other fails CI here.

Mirrors `test_ts_charter_sync.py` — a cheap regex check, no TS parser, for the
closed set of string constants. The landing rebuild imports these constants
directly, so this gate keeps the marketed hero from drifting away from the
package's own description.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from schemabrain import positioning

REPO_ROOT = Path(__file__).resolve().parents[2]
TS_FILE = REPO_ROOT / "web" / "lib" / "positioning.ts"


def _ts_string_const(ts_file: Path, name: str) -> str:
    """Pull the value off an `export const NAME = "value";` declaration.

    `\\s*` between `=` and the opening quote tolerates prettier wrapping a long
    string onto the next line.
    """
    text = ts_file.read_text()
    match = re.search(
        rf'export\s+const\s+{re.escape(name)}\s*=\s*"([^"]*)"',
        text,
    )
    if not match:
        raise AssertionError(f"const {name!r} not declared in {ts_file.relative_to(REPO_ROOT)}")
    return match.group(1)


@pytest.mark.parametrize(
    ("const_name", "python_value"),
    [
        ("TAGLINE", positioning.TAGLINE),
        ("INSTALL_PRIMARY", positioning.INSTALL_PRIMARY),
        ("LICENSE_LABEL", positioning.LICENSE_LABEL),
    ],
)
def test_ts_positioning_matches_python(const_name: str, python_value: str) -> None:
    ts_value = _ts_string_const(TS_FILE, const_name)
    assert ts_value == python_value, (
        f"positioning drift for {const_name}:\n"
        f"  Python (schemabrain/positioning.py): {python_value!r}\n"
        f"  TS     (web/lib/positioning.ts):     {ts_value!r}"
    )
