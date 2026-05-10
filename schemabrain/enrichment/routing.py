"""Routing heuristic: should this column go to Haiku or Sonnet?

Two-tier model routing per the v0 plan: clear-name columns go to cheap
Haiku 4.5; cryptic compound names (e.g. `acct_dim_v3`, `pmt_fct_h`)
route to Sonnet 4.6 because the extra reasoning is worth the ~5x cost
on the small subset of columns that need it.

The heuristic is **deliberately dictionary-free** — it works purely on
name shape (segment count, segment lengths). A dictionary would catch
edge cases (recognizing `acct` as an abbreviation of `account`) but
would add a dependency or a bundled wordlist for a marginal win on a
handful of columns. The shape-based rules below match the plan's
canonical examples and are easy to tune later if production runs show
systematic mis-routing.
"""

from __future__ import annotations

# A segment of this many characters is long enough to plausibly be a
# real English word ("user", "name", "first", "email", "orders").
# Threshold is exclusive of 4-char abbreviations like `acct`, `cust`,
# `dept` which can look word-shaped but are not.
_MIN_RECOGNIZABLE_SEGMENT_LEN = 5

# Average segment length below which a multi-segment name is considered
# heavily abbreviated. `pmt_fct_h` (avg 2.33) and `acct_dim_v3`
# (avg 3.0) both fall under; `ab_customer_xyz` (avg 4.33) doesn't.
_MAX_AVG_SEGMENT_LEN_FOR_CRYPTIC = 3.5

# Below this count of underscore-separated segments, the name is
# considered "simple": single words and 1-underscore compounds (like
# `user_id`, `email_address`, `created_at`) are well within Haiku's
# range, so the routing never escalates them regardless of length.
_MIN_SEGMENTS_FOR_CRYPTIC_CHECK = 3


def is_cryptic(column_name: str) -> bool:
    """True if the column name needs Sonnet-tier reasoning to interpret.

    Rules:
      1. Names with 0 or 1 underscores are never cryptic — they're
         either a single word (which Haiku can handle directly) or a
         simple compound where at least one half usually carries
         enough signal.
      2. Names with ≥2 underscores escape "cryptic" if any segment is
         ≥5 chars (recognizable-word proxy).
      3. Otherwise, cryptic iff the average segment length is < 3.5
         (i.e., the name is heavily abbreviated end-to-end).
    """
    segments = column_name.split("_")
    if len(segments) < _MIN_SEGMENTS_FOR_CRYPTIC_CHECK:
        return False
    if any(len(s) >= _MIN_RECOGNIZABLE_SEGMENT_LEN for s in segments):
        return False
    avg_len = sum(len(s) for s in segments) / len(segments)
    return avg_len < _MAX_AVG_SEGMENT_LEN_FOR_CRYPTIC


__all__ = ["is_cryptic"]
