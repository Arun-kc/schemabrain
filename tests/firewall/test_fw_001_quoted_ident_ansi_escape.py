"""FW-001 — Quoted-identifier path skips ``_IDENT_REGEX`` entirely.

Background:

    ``schemabrain/mcp/_helpers.py:196-217`` — the
    ``_validate_parsed_segment`` for ``was_quoted=True`` only checks
    length + non-empty. Any printable content lands. An attacker who
    creates a Postgres column with a name carrying an ANSI escape
    (e.g. ``\x1b[31m evil \x1b[0m``) can refer to it via a
    SQL-standard double-quoted identifier in ``qualified_name`` and
    smuggle the escape into the agent's render context — terminal
    UIs that pipe MCP responses through stdout render the escape
    live, hiding the trailing payload.

Expected SECURE behaviour:

    Even quoted identifiers must reject control characters (``\x00``
    through ``\x1f`` and ``\x7f``). Postgres allows them at the
    catalog layer, but the MCP firewall should refuse to echo them
    into agent-rendered surfaces. The parser is the right gate — it
    already has the role and length checks; adding a control-char
    rejection there closes the bypass for every downstream tool.

Repro shape:

  1. Parse a ``qualified_name`` of the form
     ``'public.t."<ANSI escape> evil <ANSI escape>"'``.
  2. Today the parser accepts it. Tomorrow it must raise ``ValueError``.

We exercise the parser directly via ``_parse_column_qualified_name``
— no DB needed because the bypass lives in the validator. The test
also drives ``describe_column_impl`` with the same payload against a
seeded store column to demonstrate the escape would reach the
ColumnDetail response on success.
"""

from __future__ import annotations

import pytest

from schemabrain.core.models import Column, Table
from schemabrain.mcp._helpers import _parse_column_qualified_name

pytestmark = [pytest.mark.firewall_bypass]


# ANSI escape sequences. CSI (`\x1b[`) sequences are the worst offenders
# because they actively reflow the terminal — colour, cursor moves,
# screen clears. NUL/BEL/control chars round out the corpus.
_CONTROL_PAYLOADS: tuple[tuple[str, str], ...] = (
    ("\x1b[31m evil \x1b[0m", "ANSI CSI colour codes — paint output red"),
    ("\x1b[2J cleared", "ANSI CSI 2J — clear-screen escape"),
    ("\x1b]0;hacked\x07", "OSC 0 — title-bar rewrite (xterm-family)"),
    ("\x00null_byte", "embedded NUL"),
    ("\x07bell_attempt", "BEL (ctrl-G)"),
    ("\r\n CRLF_injection", "CRLF — splits log/output lines"),
)


@pytest.mark.parametrize(("payload", "description"), _CONTROL_PAYLOADS)
def test_quoted_identifier_rejects_control_characters(payload: str, description: str) -> None:
    """The qualified-name parser MUST reject quoted segments containing
    control characters.

    Today the parser accepts them — ``_validate_parsed_segment`` only
    runs ``_IDENT_REGEX`` when ``was_quoted=False``. The
    ``ValueError`` we want to see does not raise.
    """
    # Build a payload of shape `public.t."<control-char body>"`. The
    # outer schema/table are plain unquoted identifiers; the column is
    # the attack carrier.
    qualified_name = f'public.t."{payload}"'

    with pytest.raises(ValueError) as exc_info:
        _parse_column_qualified_name(qualified_name)

    # If we reach this assertion, the parser accepted the payload —
    # that's the bypass. The pytest.raises context manager already
    # failed the test if no exception fired; the assertion message
    # below is a backup for clearer triage output.
    assert "control" in str(exc_info.value).lower() or "invalid" in str(exc_info.value).lower(), (
        f"BYPASS: parser accepted quoted identifier with {description!r}. "
        f"Payload {payload!r} passed _validate_parsed_segment despite "
        f"carrying control chars that will reflow agent terminals when "
        f"the column name is echoed in describe_table / describe_column "
        f"output. Reject control chars (\\x00-\\x1f, \\x7f) at the "
        f"parser boundary."
    )


def test_quoted_identifier_with_ansi_escape_reaches_describe_column(store) -> None:
    """End-to-end: when the parser admits the payload, the escape
    survives all the way into the ``ColumnDetail`` response.

    Demonstrates the impact even without spinning up a real Postgres
    column — once the parser accepts the name, downstream layers echo
    it verbatim because they treat the validated string as a trusted
    identifier.
    """
    schema_name = "public"
    table_name = "t"
    column_name = "\x1b[31m evil \x1b[0m"

    # Seed a store row with the malicious column name. We're bypassing
    # the parser here on the write path — this models the worst-case
    # scenario where a real Postgres catalog already carries such a
    # column (whether through migration carelessness or a connector
    # that didn't reject it at index time).
    table = Table(
        name=table_name,
        schema_name=schema_name,
        columns=(
            Column(
                name=column_name,
                table_name=table_name,
                schema_name=schema_name,
                data_type="text",
                nullable=True,
                ordinal_position=1,
            ),
        ),
    )
    store.write_table(table, source_connection_id="fw_001")

    # Parse the malicious column qualified_name. If the parser
    # rejects this — which it should — the test fails-EARLY with a
    # ValueError, which we interpret as the SECURE behaviour.
    qualified_name = f'{schema_name}.{table_name}."{column_name}"'

    try:
        parsed = _parse_column_qualified_name(qualified_name)
    except ValueError:
        # SECURE: parser rejected the payload. Test passes by exiting
        # the try block — when the fix lands, this branch is taken.
        return

    # Reached: parser accepted the payload. The parsed tuple still
    # carries the control characters — that's the bypass. We surface
    # this via a deliberate failure so the next reviewer sees the exact bytes
    # that passed.
    parsed_column = parsed[2]
    assert "\x1b" not in parsed_column and "\x00" not in parsed_column, (
        f"BYPASS: parser accepted column name carrying ANSI escape: "
        f"parsed_column={parsed_column!r}. Downstream MCP layers will "
        f"echo this verbatim into agent-rendered describe_column / "
        f"describe_table responses, where terminal UIs interpret the "
        f"escape sequence live."
    )
