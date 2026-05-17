"""`EventRedactor` strips credentials and PII-shaped values from
`args_summary` BEFORE the bus writes the event line.

Four rules apply per-value (keys are never touched):

  1. Connection URLs (`postgresql://`, `mysql://`, `sqlite://`, …)
     → `<redacted-connection-url>`. Closes the credential leak path
     where a tool arg carries a full URL.
  2. Long strings (>2 KB) → `<truncated:N bytes>`. Stops a large SQL
     blob or pasted text from inflating the events file.
  3. `get_metric.filters` dict VALUES → `<value>`. Filter values are
     user PII by default (emails, customer IDs); keys describe the
     schema and are safe.
  4. Email-shaped strings → `<email>`. Defence in depth for free-text
     args that slip past rule 3.

The redactor is conservative-but-incomplete by design. A user passing
raw PII as a positional arg still leaks. The Charter envelope already
documents this caveat for the structured response stream; the event
log inherits it.
"""

from __future__ import annotations

import re
from typing import Any, Final

_MAX_STRING_BYTES: Final[int] = 2048

# Match connection-style URLs anywhere in a string, not just at the
# start. An AI agent's natural-language argument like "connecting to
# postgresql://user:pw@host/db" must redact too, not only the bare
# string. The trailing `[^@\s]+:[^@\s]+@` requires `user:pass@` to
# qualify so plain documentation URLs (https://docs.example.com)
# don't false-positive.
_CONNECTION_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"(postgresql|postgres|mysql|sqlite|mongodb|redis|amqp|amqps|https?)"
    r"(\+\w+)?://[^@\s]+:[^@\s]+@",
    re.IGNORECASE,
)
# Match an email anywhere in a string. The previous full-string match
# missed mid-sentence emails like "contact alice@example.com".
_EMAIL_RE: Final[re.Pattern[str]] = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")

# Tool args whose VALUES should be flattened to `<value>` regardless
# of shape because they are user-supplied filter inputs.
_FILTER_ARG_KEYS: Final[frozenset[str]] = frozenset({"filters"})


class EventRedactor:
    """Pure function dressed as a class for future extensibility.

    Stateless today; tomorrow a custom redactor instance may carry a
    site-specific deny-list (e.g. additional schemes or column-name
    allow-lists). The class shape keeps the call site stable.
    """

    def redact(self, value: Any) -> Any:
        return self._walk(value, in_filter_values=False)

    def _walk(self, value: Any, *, in_filter_values: bool) -> Any:
        if isinstance(value, dict):
            return {
                k: self._walk(v, in_filter_values=in_filter_values or (k in _FILTER_ARG_KEYS))
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self._walk(item, in_filter_values=in_filter_values) for item in value]
        if isinstance(value, tuple):
            return tuple(self._walk(item, in_filter_values=in_filter_values) for item in value)
        if isinstance(value, str):
            return self._redact_string(value, in_filter_values=in_filter_values)
        # Non-string scalars (int, float, bool, None) inside a filter
        # context are still user-supplied PII candidates.
        if in_filter_values and value is not None:
            return "<value>"
        return value

    @staticmethod
    def _redact_string(value: str, *, in_filter_values: bool) -> str:
        # Length-budget check first — a giant string never needs further
        # scanning and may not even be valid UTF-8 to regex.
        encoded_len = len(value.encode("utf-8"))
        if encoded_len > _MAX_STRING_BYTES:
            return f"<truncated:{encoded_len} bytes>"
        # `search` (not `match`) so mid-string credentials get caught.
        # When a URL is present, redact the WHOLE string — partially
        # surgical replacement risks leaving the surrounding context
        # that referenced the URL (and may itself be sensitive).
        if _CONNECTION_URL_RE.search(value):
            return "<redacted-connection-url>"
        if in_filter_values:
            return "<value>"
        # Same posture for emails: if any substring is email-shaped,
        # redact the whole string. False positives on legitimate
        # values are acceptable; the alternative is leaking PII.
        if _EMAIL_RE.search(value):
            return "<email>"
        return value
