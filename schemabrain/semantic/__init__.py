"""Semantic layer plumbing.

`semantic/` holds the compiler that turns a `Metric` definition + caller
arguments (group_by, filters, time_grain, limit) into a parameterised
SQL statement plus provenance metadata. The compiler IR is the v2
substrate per `project_architectural_decisions_v1_v2.md` seam 2:
`validate_query` at v2 is this module with more rules.
"""
