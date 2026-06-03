"""Render a `DictionaryModel` to a self-contained HTML document.

A convenience export (the Markdown form is the byte-golden + dogfood
surface). Pure, no new dependency: a single HTML5 document with an inline
stylesheet, sharing the canonical PII labels with the Markdown renderer
via `render_common`. Every dynamic value is run through `html.escape`, so
a redacted ON clause like ``"a"."<redacted_column>" = "b"."id"`` renders
as escaped text rather than stray markup.
"""

from __future__ import annotations

import html

from schemabrain.datadict.model import (
    DictColumn,
    DictEntity,
    DictionaryModel,
    DictJoin,
    DictMetric,
)
from schemabrain.datadict.render_common import (
    category_label,
    is_catastrophic_category,
    sensitivity_label,
)
from schemabrain.positioning import TAGLINE

_EM_DASH = "—"

# The SchemaBrain brain mark (canonical `docs/assets/schemabrain-mark-64.svg`,
# inlined verbatim so the export stays self-contained). Its own ivory
# background makes it read as a branded tile on both light and dark pages.
_BRAND_MARK = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">'
    '<rect width="64" height="64" fill="#FAFAF7"></rect>'
    '<defs><clipPath id="sb-mark-clip"><path d="M22 12 C 22 8, 30 6, 32 10 C 36 6, 44 8, 44 14 '
    "C 50 12, 56 18, 52 24 C 58 28, 56 36, 50 38 C 54 44, 48 52, 40 50 C 38 56, 28 56, 26 52 "
    'C 18 54, 12 48, 14 42 C 8 40, 6 32, 12 30 C 8 24, 12 16, 18 18 C 18 14, 20 12, 22 12 Z">'
    "</path></clipPath></defs>"
    '<g clip-path="url(#sb-mark-clip)"><rect x="32" y="0" width="32" height="64" fill="#3DCD8B">'
    "</rect></g>"
    '<path d="M22 12 C 22 8, 30 6, 32 10 C 36 6, 44 8, 44 14 C 50 12, 56 18, 52 24 C 58 28, 56 36, '
    "50 38 C 54 44, 48 52, 40 50 C 38 56, 28 56, 26 52 C 18 54, 12 48, 14 42 C 8 40, 6 32, 12 30 "
    'C 8 24, 12 16, 18 18 C 18 14, 20 12, 22 12 Z" stroke="#0C0C0C" stroke-width="2.2" '
    'stroke-linejoin="round" fill="none"></path>'
    '<line x1="32" y1="10" x2="32" y2="52" stroke="#0C0C0C" stroke-width="1.6" '
    'stroke-linecap="round"></line>'
    '<path d="M14 22 C 20 19, 26 22, 30 21" stroke="#0C0C0C" stroke-width="1.4" '
    'stroke-linecap="round" fill="none"></path>'
    '<path d="M12 32 C 18 28, 26 33, 30 31" stroke="#0C0C0C" stroke-width="1.4" '
    'stroke-linecap="round" fill="none"></path>'
    '<path d="M14 42 C 20 39, 26 43, 30 41" stroke="#0C0C0C" stroke-width="1.4" '
    'stroke-linecap="round" fill="none"></path>'
    '<g clip-path="url(#sb-mark-clip)">'
    '<circle cx="36" cy="22" r="1.4" fill="#FAFAF7"></circle>'
    '<line x1="38.5" y1="22" x2="50" y2="22" stroke="#FAFAF7" stroke-width="1.6" '
    'stroke-linecap="round"></line>'
    '<circle cx="36" cy="32" r="1.4" fill="#FAFAF7"></circle>'
    '<line x1="38.5" y1="32" x2="52" y2="32" stroke="#FAFAF7" stroke-width="1.6" '
    'stroke-linecap="round"></line>'
    '<circle cx="36" cy="42" r="1.4" fill="#FAFAF7"></circle>'
    '<line x1="38.5" y1="42" x2="50" y2="42" stroke="#FAFAF7" stroke-width="1.6" '
    'stroke-linecap="round"></line>'
    "</g></svg>"
)

# The SchemaBrain browser icon (canonical `docs/assets/browser-icon.svg`),
# base64-encoded so the favicon is embedded with no external request. Pinned
# to the asset by `test_html_favicon_matches_browser_icon_asset`.
_FAVICON_SVG_B64 = (
    "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA1MTIgNTEyIiB3aWR0"
    "aD0iNTEyIiBoZWlnaHQ9IjUxMiI+CiAgPGRlZnM+CiAgICA8Y2xpcFBhdGggaWQ9ImJpLWJyYWluIj4KICAgICAg"
    "PHBhdGggZD0iTTE3NiA5NiBDIDE3NiA2NCwgMjQwIDQ4LCAyNTYgODAgQyAyODggNDgsIDM1MiA2NCwgMzUyIDEx"
    "MiBDIDQwMCA5NiwgNDQ4IDE0NCwgNDE2IDE5MiBDIDQ2NCAyMjQsIDQ0OCAyODgsIDQwMCAzMDQgQyA0MzIgMzUy"
    "LCAzODQgNDE2LCAzMjAgNDAwIEMgMzA0IDQ0OCwgMjI0IDQ0OCwgMjA4IDQxNiBDIDE0NCA0MzIsIDk2IDM4NCwg"
    "MTEyIDMzNiBDIDY0IDMyMCwgNDggMjU2LCA5NiAyNDAgQyA2NCAxOTIsIDk2IDEyOCwgMTQ0IDE0NCBDIDE0NCAx"
    "MTIsIDE2MCA5NiwgMTc2IDk2IFoiPjwvcGF0aD4KICAgIDwvY2xpcFBhdGg+CiAgICA8Y2xpcFBhdGggaWQ9ImJp"
    "LWNhcmQiPgogICAgICA8cmVjdCB4PSIwIiB5PSIwIiB3aWR0aD0iNTEyIiBoZWlnaHQ9IjUxMiIgcng9Ijk2IiBy"
    "eT0iOTYiPjwvcmVjdD4KICAgIDwvY2xpcFBhdGg+CiAgPC9kZWZzPgoKICA8ZyBjbGlwLXBhdGg9InVybCgjYmkt"
    "Y2FyZCkiPgogICAgPHJlY3Qgd2lkdGg9IjUxMiIgaGVpZ2h0PSI1MTIiIGZpbGw9IiNGQUZBRjciPjwvcmVjdD4K"
    "CiAgICAKICAgIDxnIGNsaXAtcGF0aD0idXJsKCNiaS1icmFpbikiPgogICAgICA8cmVjdCB4PSIyNTYiIHk9IjAi"
    "IHdpZHRoPSIyNTYiIGhlaWdodD0iNTEyIiBmaWxsPSIjM0VDRjhFIj48L3JlY3Q+CiAgICA8L2c+CgogICAgCiAg"
    "ICA8cGF0aCBkPSJNMTc2IDk2IEMgMTc2IDY0LCAyNDAgNDgsIDI1NiA4MCBDIDI4OCA0OCwgMzUyIDY0LCAzNTIg"
    "MTEyIEMgNDAwIDk2LCA0NDggMTQ0LCA0MTYgMTkyIEMgNDY0IDIyNCwgNDQ4IDI4OCwgNDAwIDMwNCBDIDQzMiAz"
    "NTIsIDM4NCA0MTYsIDMyMCA0MDAgQyAzMDQgNDQ4LCAyMjQgNDQ4LCAyMDggNDE2IEMgMTQ0IDQzMiwgOTYgMzg0"
    "LCAxMTIgMzM2IEMgNjQgMzIwLCA0OCAyNTYsIDk2IDI0MCBDIDY0IDE5MiwgOTYgMTI4LCAxNDQgMTQ0IEMgMTQ0"
    "IDExMiwgMTYwIDk2LCAxNzYgOTYgWiIgc3Ryb2tlPSIjMEEwQTBBIiBzdHJva2Utd2lkdGg9IjIwIiBzdHJva2Ut"
    "bGluZWpvaW49InJvdW5kIiBmaWxsPSJub25lIj48L3BhdGg+CgogICAgCiAgICA8bGluZSB4MT0iMjU2IiB5MT0i"
    "ODAiIHgyPSIyNTYiIHkyPSI0MTYiIHN0cm9rZT0iIzBBMEEwQSIgc3Ryb2tlLXdpZHRoPSIxNiIgc3Ryb2tlLWxp"
    "bmVjYXA9InJvdW5kIj48L2xpbmU+CgogICAgCiAgICA8cGF0aCBkPSJNMTEyIDE3NiBDIDE2MCAxNTIsIDIwOCAx"
    "NzYsIDI0MCAxNjgiIHN0cm9rZT0iIzBBMEEwQSIgc3Ryb2tlLXdpZHRoPSIxNCIgc3Ryb2tlLWxpbmVjYXA9InJv"
    "dW5kIiBmaWxsPSJub25lIj48L3BhdGg+CiAgICA8cGF0aCBkPSJNOTYgMjU2IEMgMTQ0IDIyNCwgMjA4IDI2NCwg"
    "MjQwIDI0OCIgc3Ryb2tlPSIjMEEwQTBBIiBzdHJva2Utd2lkdGg9IjE0IiBzdHJva2UtbGluZWNhcD0icm91bmQi"
    "IGZpbGw9Im5vbmUiPjwvcGF0aD4KICAgIDxwYXRoIGQ9Ik0xMTIgMzM2IEMgMTYwIDMxMiwgMjA4IDM0NCwgMjQw"
    "IDMyOCIgc3Ryb2tlPSIjMEEwQTBBIiBzdHJva2Utd2lkdGg9IjE0IiBzdHJva2UtbGluZWNhcD0icm91bmQiIGZp"
    "bGw9Im5vbmUiPjwvcGF0aD4KCiAgICAKICAgIDxnIGNsaXAtcGF0aD0idXJsKCNiaS1icmFpbikiPgogICAgICA8"
    "Y2lyY2xlIGN4PSIyODgiIGN5PSIxNzYiIHI9IjE0IiBmaWxsPSIjRkFGQUY3Ij48L2NpcmNsZT4KICAgICAgPGxp"
    "bmUgeDE9IjMxMiIgeTE9IjE3NiIgeDI9IjQwMCIgeTI9IjE3NiIgc3Ryb2tlPSIjRkFGQUY3IiBzdHJva2Utd2lk"
    "dGg9IjE2IiBzdHJva2UtbGluZWNhcD0icm91bmQiPjwvbGluZT4KICAgICAgPGNpcmNsZSBjeD0iMjg4IiBjeT0i"
    "MjU2IiByPSIxNCIgZmlsbD0iI0ZBRkFGNyI+PC9jaXJjbGU+CiAgICAgIDxsaW5lIHgxPSIzMTIiIHkxPSIyNTYi"
    "IHgyPSI0MTYiIHkyPSIyNTYiIHN0cm9rZT0iI0ZBRkFGNyIgc3Ryb2tlLXdpZHRoPSIxNiIgc3Ryb2tlLWxpbmVj"
    "YXA9InJvdW5kIj48L2xpbmU+CiAgICAgIDxjaXJjbGUgY3g9IjI4OCIgY3k9IjMzNiIgcj0iMTQiIGZpbGw9IiNG"
    "QUZBRjciPjwvY2lyY2xlPgogICAgICA8bGluZSB4MT0iMzEyIiB5MT0iMzM2IiB4Mj0iNDAwIiB5Mj0iMzM2IiBz"
    "dHJva2U9IiNGQUZBRjciIHN0cm9rZS13aWR0aD0iMTYiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCI+PC9saW5lPgog"
    "ICAgPC9nPgogIDwvZz4KPC9zdmc+"
)
_FAVICON = f"data:image/svg+xml;base64,{_FAVICON_SVG_B64}"

# Headings are styled by CLASS, not tag, so the visual hierarchy stays
# correct regardless of heading DEPTH — a multi-source document nests
# entities one level deeper (## source -> ### entity -> #### section) for
# a semantically correct outline without changing how they look.
_STYLE = """\
:root {
  color-scheme: light dark;
  --brand: #3ecf8e;
  --bg: #ffffff; --text: #14181d; --muted: #5b6470;
  --border: #e4e7eb; --border-soft: #eceef1; --code-bg: #f2f4f6; --alarm: #b00020;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0b0f14; --text: #e6edf3; --muted: #9aa4b2;
    --border: #222a33; --border-soft: #1a212a; --code-bg: #1b232c; --alarm: #ff6b6b;
  }
}
body { font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       max-width: 64rem; margin: 2rem auto; padding: 0 1.25rem;
       background: var(--bg); color: var(--text); }
.masthead { display: flex; align-items: center; gap: .6rem; margin: 0 0 .4rem; }
.masthead .mark { width: 34px; height: 34px; border-radius: 8px; overflow: hidden; flex: none; }
.masthead .mark svg { width: 100%; height: 100%; display: block; }
.wordmark { font: 600 1.15rem ui-monospace, "JetBrains Mono", SFMono-Regular, Menlo, monospace;
            letter-spacing: -.02em; color: var(--text); }
.wordmark .accent { color: var(--brand); }
.tagline { color: var(--muted); margin: 0 0 2.25rem; font-size: .95rem; max-width: 46rem; }
h1 { font-size: 1.9rem; margin: 0 0 .25rem;
     border-left: 4px solid var(--brand); padding-left: .6rem; }
.lede { color: var(--muted); margin: 0 0 2rem; }
.footer { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid var(--border);
          color: var(--muted); font: .82em ui-monospace, "JetBrains Mono", SFMono-Regular, Menlo,
          monospace; }
.footer .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
               background: var(--brand); margin-right: .45rem; vertical-align: middle; }
.source-heading { font-size: 1.6rem; margin: 2.5rem 0 .5rem; }
section.entity { border-top: 1px solid var(--border); padding-top: 1.5rem; margin-top: 1.5rem; }
.entity-name { font-size: 1.35rem; margin: 0 0 .35rem; }
.section-heading { font-size: .8rem; text-transform: uppercase; letter-spacing: .06em;
     color: var(--muted); margin: 1.5rem 0 .5rem; }
ul.meta { list-style: none; padding: 0; margin: .5rem 0 0; color: var(--muted); }
code { font: .85em ui-monospace, "SF Mono", Menlo, monospace;
       background: var(--code-bg); color: var(--text); padding: .1em .35em; border-radius: 4px; }
table { border-collapse: collapse; width: 100%; margin: .25rem 0 .5rem; font-size: .9rem; }
th, td { text-align: left; padding: .4rem .6rem; border-bottom: 1px solid var(--border-soft);
         vertical-align: top; }
th { font-weight: 600; color: var(--muted); }
.catastrophic { color: var(--alarm); font-weight: 600; }
"""


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _desc(text: str | None) -> str:
    return _esc(text) if text else _EM_DASH


def _pii_cell(categories: tuple[str, ...]) -> str:
    if not categories:
        return _EM_DASH
    pieces: list[str] = []
    for category in categories:
        label = _esc(category_label(category))  # type: ignore[arg-type]
        if is_catastrophic_category(category):  # type: ignore[arg-type]
            pieces.append(f'<span class="catastrophic">{label} (catastrophic)</span>')
        else:
            pieces.append(label)
    return " / ".join(pieces)


def _columns_table(columns: tuple[DictColumn, ...], *, tag: str) -> list[str]:
    rows = [
        f'<{tag} class="section-heading">Columns</{tag}>',
        "<table>",
        "<thead><tr><th>Column</th><th>Type</th><th>Null</th><th>PK</th>"
        "<th>Identity</th><th>Sensitivity</th><th>PII categories</th><th>Description</th></tr></thead>",
        "<tbody>",
    ]
    for col in columns:
        rows.append(
            f"<tr><td><code>{_esc(col.name)}</code></td>"
            f"<td><code>{_esc(col.data_type)}</code></td>"
            f"<td>{_yes_no(col.nullable)}</td>"
            f"<td>{_yes_no(col.is_primary_key)}</td>"
            f"<td>{_yes_no(col.is_identity)}</td>"
            f"<td>{_esc(sensitivity_label(col.pii_sensitivity))}</td>"
            f"<td>{_pii_cell(col.pii_categories)}</td>"
            f"<td>{_desc(col.description)}</td></tr>"
        )
    rows += ["</tbody>", "</table>"]
    return rows


def _joins_table(joins: tuple[DictJoin, ...], *, tag: str) -> list[str]:
    rows = [
        f'<{tag} class="section-heading">Joins</{tag}>',
        "<table>",
        "<thead><tr><th>Join</th><th>On</th><th>Cardinality</th>"
        "<th>Provenance</th><th>Description</th></tr></thead>",
        "<tbody>",
    ]
    for join in joins:
        cardinality = _esc(join.cardinality) if join.cardinality else _EM_DASH
        rows.append(
            f"<tr><td><code>{_esc(join.name)}</code></td>"
            f"<td><code>{_esc(join.on_clause)}</code></td>"
            f"<td>{cardinality}</td>"
            f"<td>{_esc(join.provenance)}</td>"
            f"<td>{_desc(join.description)}</td></tr>"
        )
    rows += ["</tbody>", "</table>"]
    return rows


def _metrics_table(metrics: tuple[DictMetric, ...], *, tag: str) -> list[str]:
    rows = [
        f'<{tag} class="section-heading">Metrics</{tag}>',
        "<table>",
        "<thead><tr><th>Metric</th><th>Aggregation</th><th>Measure</th>"
        "<th>Time dimension</th><th>Grains</th><th>Description</th></tr></thead>",
        "<tbody>",
    ]
    for metric in metrics:
        time_dimension = (
            f"<code>{_esc(metric.time_dimension)}</code>" if metric.time_dimension else _EM_DASH
        )
        grains = _esc(", ".join(metric.time_grains)) if metric.time_grains else _EM_DASH
        rows.append(
            f"<tr><td><code>{_esc(metric.name)}</code></td>"
            f"<td>{_esc(metric.agg)}</td>"
            f"<td><code>{_esc(metric.measure)}</code></td>"
            f"<td>{time_dimension}</td>"
            f"<td>{grains}</td>"
            f"<td>{_desc(metric.description)}</td></tr>"
        )
    rows += ["</tbody>", "</table>"]
    return rows


def _entity_section(entity: DictEntity, *, level: int) -> list[str]:
    """Render one entity. `level` is the entity heading depth (h2 or h3).

    Section tables nest one level deeper so a multi-source document keeps
    a correct heading tree (h2 source -> h3 entity -> h4 section).
    Appearance is class-driven, so the depth shift does not change styling.
    """
    entity_tag = f"h{level}"
    section_tag = f"h{level + 1}"
    lines = [
        '<section class="entity">',
        f'<{entity_tag} class="entity-name">{_esc(entity.name)}</{entity_tag}>',
        f"<p>{_esc(entity.description) if entity.description else _EM_DASH}</p>",
        '<ul class="meta">',
        f"<li><strong>Table:</strong> <code>{_esc(entity.qualified_table)}</code></li>",
        f"<li><strong>Identity:</strong> <code>{_esc(entity.identity)}</code></li>",
        f"<li><strong>Group:</strong> {_esc(entity.group)}</li>",
        "</ul>",
    ]
    lines += _columns_table(entity.columns, tag=section_tag)
    if entity.joins:
        lines += _joins_table(entity.joins, tag=section_tag)
    if entity.metrics:
        lines += _metrics_table(entity.metrics, tag=section_tag)
    lines.append("</section>")
    return lines


def render_html(model: DictionaryModel) -> str:
    """Render the full dictionary as a single self-contained HTML document."""
    lede = (
        f"Generated from the local SchemaBrain store (schema version "
        f"{_esc(model.schema_version)}). Every indexed table, column, type, PII "
        f"classification, semantic join, and metric."
    )
    lines: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f'<link rel="icon" type="image/svg+xml" href="{_FAVICON}">',
        "<title>schemabrain · data dictionary</title>",
        f"<style>{_STYLE}</style>",
        "</head>",
        "<body>",
        f'<header class="masthead"><span class="mark">{_BRAND_MARK}</span>'
        '<span class="wordmark">schema<span class="accent">brain</span></span></header>',
        f'<p class="tagline">{_esc(TAGLINE)}</p>',
        "<h1>Data dictionary</h1>",
        f'<p class="lede">{lede}</p>',
    ]
    multi_source = len(model.sources) > 1
    entity_level = 3 if multi_source else 2
    for source in model.sources:
        if multi_source:
            lines.append(
                f'<h2 class="source-heading">Source: '
                f"<code>{_esc(source.source_connection_id)}</code></h2>"
            )
        for entity in source.entities:
            lines += _entity_section(entity, level=entity_level)
    lines += [
        '<footer class="footer"><span class="dot"></span>'
        "schemabrain · data dictionary · generated locally from your store</footer>",
        "</body>",
        "</html>",
        "",
    ]
    return "\n".join(lines)
