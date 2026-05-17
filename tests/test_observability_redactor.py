"""Tests for the EventRedactor — 4 rules + anti-tautology."""

from __future__ import annotations

from schemabrain.observability.redactor import EventRedactor


class TestConnectionUrlRedaction:
    def test_postgresql_url_redacted(self) -> None:
        r = EventRedactor()
        assert r.redact("postgresql://user:pass@host:5432/db") == "<redacted-connection-url>"

    def test_postgres_psycopg_url_redacted(self) -> None:
        r = EventRedactor()
        assert r.redact("postgresql+psycopg://u:p@h:5432/db") == "<redacted-connection-url>"

    def test_mysql_url_redacted(self) -> None:
        r = EventRedactor()
        assert r.redact("mysql://u:p@h/d") == "<redacted-connection-url>"

    def test_sqlite_file_url_not_redacted_no_credentials(self) -> None:
        # sqlite:// URLs reference a local file with no credentials —
        # they are not a credential-leak vector. The new regex requires
        # user:pass@ to qualify.
        r = EventRedactor()
        assert r.redact("sqlite:///./x.db") == "sqlite:///./x.db"

    def test_mid_string_postgres_url_redacted(self) -> None:
        # The previous regex was ^-anchored and missed embedded URLs.
        # A natural-language agent query referencing a connection
        # string mid-sentence must redact too.
        r = EventRedactor()
        assert (
            r.redact("connecting to postgresql://u:p@h/d for stats") == "<redacted-connection-url>"
        )

    def test_mongodb_url_with_creds_redacted(self) -> None:
        r = EventRedactor()
        assert r.redact("mongodb://user:pw@host/db") == "<redacted-connection-url>"

    def test_redis_url_with_creds_redacted(self) -> None:
        r = EventRedactor()
        assert r.redact("redis://default:secret@h:6379") == "<redacted-connection-url>"

    def test_https_basic_auth_url_redacted(self) -> None:
        r = EventRedactor()
        assert r.redact("https://token:x@github.com/org/repo") == "<redacted-connection-url>"

    def test_plain_https_url_not_redacted_no_credentials(self) -> None:
        # Public URLs without user:pass@ qualifier shouldn't be redacted
        # — they're not credentials.
        r = EventRedactor()
        assert r.redact("https://docs.example.com") == "https://docs.example.com"

    def test_postgres_url_inside_dict_redacted(self) -> None:
        r = EventRedactor()
        out = r.redact({"url": "postgresql://u:p@h/d"})
        assert out == {"url": "<redacted-connection-url>"}

    def test_url_case_insensitive(self) -> None:
        r = EventRedactor()
        assert r.redact("PostgreSQL://u:p@h/d") == "<redacted-connection-url>"


class TestLongValueTruncation:
    def test_long_string_truncated_with_byte_count(self) -> None:
        r = EventRedactor()
        big = "x" * 4000
        out = r.redact(big)
        assert out == "<truncated:4000 bytes>"

    def test_short_string_kept(self) -> None:
        r = EventRedactor()
        assert r.redact("hello") == "hello"

    def test_2049_bytes_truncated(self) -> None:
        r = EventRedactor()
        big = "y" * 2049
        out = r.redact(big)
        assert out == "<truncated:2049 bytes>"

    def test_2048_bytes_kept(self) -> None:
        r = EventRedactor()
        big = "z" * 2048
        out = r.redact(big)
        assert out == big

    def test_multibyte_chars_counted_in_bytes(self) -> None:
        r = EventRedactor()
        # Each ✓ is 3 bytes UTF-8; 700 of them = 2100 bytes > 2048
        big = "✓" * 700
        out = r.redact(big)
        assert out == "<truncated:2100 bytes>"


class TestFilterValueRedaction:
    def test_get_metric_filter_values_become_placeholder(self) -> None:
        r = EventRedactor()
        out = r.redact({"metric_name": "total_revenue", "filters": {"user_id": 12345}})
        assert out == {
            "metric_name": "total_revenue",
            "filters": {"user_id": "<value>"},
        }

    def test_filter_string_values_become_placeholder(self) -> None:
        r = EventRedactor()
        out = r.redact({"filters": {"email": "alice@example.com"}})
        # Filter value rule fires before the email rule because the
        # key sits inside `filters`.
        assert out == {"filters": {"email": "<value>"}}

    def test_filter_nested_dict_propagates(self) -> None:
        r = EventRedactor()
        out = r.redact({"filters": {"customer": {"id": "C42"}}})
        assert out == {"filters": {"customer": {"id": "<value>"}}}

    def test_non_filter_dict_not_affected(self) -> None:
        r = EventRedactor()
        out = r.redact({"args": {"user_id": "U1"}})
        # `args` key is NOT in the filter-arg set; the inner string
        # stays unmodified unless it matches another rule.
        assert out == {"args": {"user_id": "U1"}}

    def test_filter_key_kept(self) -> None:
        # Keys are never redacted — only values.
        r = EventRedactor()
        out = r.redact({"filters": {"secret_token": "S"}})
        assert "secret_token" in out["filters"]


class TestEmailRedaction:
    def test_plain_email_redacted(self) -> None:
        r = EventRedactor()
        assert r.redact("alice@example.com") == "<email>"

    def test_email_inside_dict(self) -> None:
        r = EventRedactor()
        out = r.redact({"contact": "bob@ex.org"})
        assert out == {"contact": "<email>"}

    def test_non_email_string_kept(self) -> None:
        r = EventRedactor()
        assert r.redact("alice at example dot com") == "alice at example dot com"

    def test_string_with_at_but_not_email_kept(self) -> None:
        r = EventRedactor()
        # "@something" without dot is not email-shaped.
        assert r.redact("user@host") == "user@host"


class TestNoOpCases:
    """Anti-tautology: plain values must not be modified."""

    def test_plain_string_kept(self) -> None:
        r = EventRedactor()
        assert r.redact("customer_name") == "customer_name"

    def test_int_kept(self) -> None:
        r = EventRedactor()
        assert r.redact(42) == 42

    def test_float_kept(self) -> None:
        r = EventRedactor()
        assert r.redact(3.14) == 3.14

    def test_bool_kept(self) -> None:
        r = EventRedactor()
        assert r.redact(True) is True

    def test_none_kept(self) -> None:
        r = EventRedactor()
        assert r.redact(None) is None

    def test_empty_dict_kept(self) -> None:
        r = EventRedactor()
        assert r.redact({}) == {}

    def test_empty_list_kept(self) -> None:
        r = EventRedactor()
        assert r.redact([]) == []

    def test_list_of_strings(self) -> None:
        r = EventRedactor()
        assert r.redact(["a", "b"]) == ["a", "b"]

    def test_tuple_preserved_as_tuple(self) -> None:
        r = EventRedactor()
        out = r.redact(("a", "b"))
        assert out == ("a", "b")
        assert isinstance(out, tuple)


class TestNesting:
    def test_url_inside_filter_still_url_redacted(self) -> None:
        # The order matters: long-value > URL > filter > email.
        # Inside filters, a connection URL still fires the URL rule
        # first because it's the most-credentialed value type.
        r = EventRedactor()
        out = r.redact({"filters": {"x": "postgresql://u:p@h/d"}})
        # URL rule fires before the filter-value rule because it's
        # the first match in the string-redaction guard order.
        assert out == {"filters": {"x": "<redacted-connection-url>"}}

    def test_deeply_nested_dict(self) -> None:
        r = EventRedactor()
        out = r.redact({"a": {"b": {"c": "alice@x.com"}}})
        assert out == {"a": {"b": {"c": "<email>"}}}

    def test_mid_string_email_redacted(self) -> None:
        # Previously the email regex was full-string anchored and
        # missed "contact alice@example.com". The new search-based
        # rule redacts the whole containing string when an email
        # substring is present.
        r = EventRedactor()
        assert r.redact("contact alice@example.com please") == "<email>"
