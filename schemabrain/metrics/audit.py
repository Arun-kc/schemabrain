"""Audit applied metrics for the anti-pattern phrases PR-6h.2 now blocks
at suggest-time.

PR-6h.2 (#88) tightened `metrics suggest` to reject candidates whose
descriptions admit the metric doesn't compute what its name implies
(the `total_order_item_revenue = sum(unit_price_cents)` footgun).
That's a forward-only fix — it prevents the LLM from RE-SUGGESTING
the pattern, but stores that received an anti-pattern metric BEFORE
PR-6h.2 shipped still have the bad metric persisted.

This module powers `schemabrain metrics audit [--fix]`:

- Without `--fix`: scan every applied metric, list the ones whose
  description matches `DESCRIPTION_ANTI_PATTERN_PHRASES`, return a
  structured report. Exit 0 if clean, 1 if any matches found (so CI
  can fail builds against a store with bad metrics).
- With `--fix`: also delete the matching metrics (origin != dbt_import).

Why match on description rather than `(agg, column)` shape: the
anti-pattern is about MEANING, not structure. `sum(unit_price_cents)`
is perfectly valid as a price-mix indicator if the description names
it as such; it's only a footgun when the description ADMITS the
shape doesn't match the name. The LLM's own description is the most
honest signal — the same one PR-6h.2 reads at suggest-time.
"""

from __future__ import annotations

from dataclasses import dataclass

from schemabrain.core.metric import Metric
from schemabrain.core.store_protocol import Store
from schemabrain.metrics.suggest import DESCRIPTION_ANTI_PATTERN_PHRASES


@dataclass(frozen=True)
class AuditFinding:
    """One metric flagged by the audit.

    `matched_phrase` is the substring that triggered the match — used
    by the CLI renderer to explain WHY each metric was flagged so the
    user understands what they're being asked to remove.
    """

    metric: Metric
    matched_phrase: str
    source_connection_id: str

    @property
    def is_dbt_owned(self) -> bool:
        """dbt-owned metrics can't be deleted manually (the upstream
        dbt repo is the source of truth). Surfaced to the CLI so it
        can emit a guided message instead of just failing on a
        `DbtOwnedMetricError` mid-loop."""
        return self.metric.origin == "dbt_import"


def find_anti_pattern_metrics(
    store: Store,
    *,
    source_connection_id: str | None = None,
) -> list[AuditFinding]:
    """Return every metric whose description contains an anti-pattern phrase.

    Reads via the protocol's `list_metrics`. The lookup is
    case-insensitive on the description, matching PR-6h.2's
    suggest-time check at `_parse_candidate`. `source_connection_id=
    None` scans across every source (the CLI default; pass an explicit
    source to narrow to one indexed schema).
    """
    findings: list[AuditFinding] = []
    metrics = store.list_metrics(source_connection_id=source_connection_id)
    # Cross-source listings don't return the source id alongside each
    # metric — to attach it to findings we re-query per source.
    # Single-source listings are the common path; the cross-source
    # path is a convenience for ops who want to clean every store at
    # once and is acceptable being O(N^2) in practice (N = source
    # count, typically 1).
    if source_connection_id is not None:
        for metric in metrics:
            phrase = _matched_phrase(metric)
            if phrase is not None:
                findings.append(
                    AuditFinding(
                        metric=metric,
                        matched_phrase=phrase,
                        source_connection_id=source_connection_id,
                    )
                )
        return findings
    # Cross-source path — discover the source id per match.
    for metric in metrics:
        phrase = _matched_phrase(metric)
        if phrase is None:
            continue
        for sid in _candidate_source_ids(store):
            if store.get_metric(metric.name, source_connection_id=sid) is not None:
                findings.append(
                    AuditFinding(
                        metric=metric,
                        matched_phrase=phrase,
                        source_connection_id=sid,
                    )
                )
                break
    return findings


def remove_anti_pattern_metrics(
    store: Store, findings: list[AuditFinding]
) -> tuple[int, list[AuditFinding]]:
    """Delete every non-dbt-owned finding's metric row. Return
    `(removed_count, skipped_findings)`.

    `skipped_findings` carries the dbt-owned entries the audit refused
    to touch — the CLI renders these alongside a guided message
    pointing the user at the upstream dbt repo. Idempotent: re-running
    after a fix is a no-op (the findings list will be empty next
    time).
    """
    removed = 0
    skipped: list[AuditFinding] = []
    for finding in findings:
        if finding.is_dbt_owned:
            skipped.append(finding)
            continue
        deleted = store.delete_metric(
            finding.metric.name,
            source_connection_id=finding.source_connection_id,
        )
        if deleted:
            removed += 1
    return removed, skipped


def _matched_phrase(metric: Metric) -> str | None:
    """Return the first anti-pattern phrase appearing in `metric.description`,
    or `None` if the description is clean.

    Case-insensitive — same matching shape as `_parse_candidate` in
    `metrics/suggest.py`. Returning the matched phrase (rather than
    just True/False) lets the CLI explain its reasoning per finding.
    """
    description = (metric.description or "").lower()
    for phrase in DESCRIPTION_ANTI_PATTERN_PHRASES:
        if phrase in description:
            return phrase
    return None


def _candidate_source_ids(store: Store) -> list[str]:
    """Best-effort source-id discovery for the cross-source audit
    path. Falls back to an empty list if the store doesn't expose the
    protocol method (older versions); the audit then can't attach a
    source_id to cross-source findings, which is acceptable — the
    `--fix` path won't fire on findings without a source_id.
    """
    method = getattr(store, "list_distinct_source_connection_ids", None)
    if method is None:  # pragma: no cover — defensive backwards-compat shim
        return []
    return list(method())
