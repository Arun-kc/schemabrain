"""Metric authoring surface: YAML grammar, suggest, and apply pipelines.

`metrics/` is the user-facing producer side for metrics — the layer
between hand-authored or dbt-imported YAML and the storage layer. The
`core/metric.py` module owns the dataclass + invariants; this package
owns the file format + the path from file to store.
"""
