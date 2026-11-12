"""Page-count estimation (InDesign does the real line breaking; we only size the frame chain)."""
from __future__ import annotations

import math

from .ir import Document, ImageRun, Paragraph, Table, TextRun, table_grid


def _eff(doc: Document, para) -> dict:
    p = doc.resolve_paragraph_style(para.style)
    p.update(para.overrides or {})
    return p


def paragraph_height(doc: Document, para: Paragraph, width: float) -> float:
    p = _eff(doc, para)
    size = float(p.get("font_size") or 12.0)
    lead = p.get("leading")
    lead = size * 1.2 if lead in (None, "auto") else float(lead)
    avail = max(20.0, width - float(p.get("left_indent") or 0) - float(p.get("right_indent") or 0))
    text_parts, img_h = [], 0.0
    for r in para.runs:
        if isinstance(r, TextRun):
            text_parts.append(r.text)
        elif isinstance(r, ImageRun):
            img_h += float(r.height)
            text_parts.append(" ")
    text = "".join(text_parts)
    caps = 1.1 if p.get("caps") == "all_caps" else 1.0
    cpl = max(1.0, avail / (size * 0.5 * caps))
    lines = sum(max(1, math.ceil(len(seg) / cpl)) for seg in text.split(" "))
    return lines * lead + img_h + float(p.get("space_before") or 0) + float(p.get("space_after") or 0)


def blocks_height(doc: Document, blocks, width: float) -> float:
    total = 0.0
    for b in blocks:
        if isinstance(b, Paragraph):
            total += paragraph_height(doc, b, width)
        elif isinstance(b, Table):
            total += table_height(doc, b)
    return total


def table_height(doc: Document, t: Table) -> float:
    positions, ncols = table_grid(t.rows)
    widths = list(t.column_widths) + [60.0] * max(0, ncols - len(t.column_widths))
    row_h = [max(3.0, float(r.min_height or 3.0)) for r in t.rows]
    for r, c, cell in positions:
        w = sum(widths[c:c + cell.col_span])
        pr = cell.props or {}
        h = blocks_height(doc, cell.blocks, w - pr.get("inset_left", 4) - pr.get("inset_right", 4))
        h += pr.get("inset_top", 4) + pr.get("inset_bottom", 4)
        row_h[r] = max(row_h[r], h / max(1, cell.row_span))
    return sum(row_h) + float((t.overrides or {}).get("space_before") or 0)


def estimate_pages(doc: Document, safety: float = 1.08) -> int:
    if not doc.stories:
        return 1
    pg = doc.page
    capacity = pg.content_height * max(1, pg.columns)
    pages, current = 1, 0.0
    for b in doc.stories[0].blocks:
        eff = _eff(doc, b) if isinstance(b, (Paragraph, Table)) else {}
        if eff.get("start_paragraph") == "next_page" and current > 0:
            pages += 1
            current = 0.0
        h = paragraph_height(doc, b, pg.column_width) if isinstance(b, Paragraph) else table_height(doc, b)
        current += h * safety
        while current > capacity:
            pages += 1
            current -= capacity
    return max(1, pages)
