"""
Intermediate Representation (IR)
================================

The IR is the *control layer* between HTML and IDML.  Everything the
converter understands about the source page is stored here as plain,
InDesign-neutral data, and everything the IDML writer produces comes only
from here.  That gives you three ways to take full control:

1.  Python:   ``doc = html_to_ir(...)``  ->  edit objects  ->  ``ir_to_idml(doc, ...)``
2.  JSON:     ``html2idml extract``      ->  edit the JSON ->  ``html2idml build``
3.  Plugins:  a ``transform_ir(doc, config)`` function that runs between both stages.

Property vocabulary (all lengths are in points):

Character props  (CHAR_KEYS)
    font_family  str        InDesign family name, e.g. "Arial"
    font_style   str        InDesign style name, e.g. "Bold Italic"
    font_size    float
    leading      float|"auto"
    color        str        name of an entry in Document.colors ("Black", "Paper" are built in)
    tracking     float      1/1000 em
    caps         "normal" | "all_caps" | "small_caps"
    position     "normal" | "superscript" | "subscript"
    underline    bool
    strike       bool
    highlight    str|None   colour name drawn behind the text (inline background-color)
    no_break     bool

Paragraph props  (PARA_KEYS)
    align              "left" | "center" | "right" | "justify"
    space_before / space_after / left_indent / right_indent / first_line_indent   float
    keep_with_next     int (lines)
    keep_together      bool
    start_paragraph    "anywhere" | "next_page" | "next_column" | "next_frame"
    list_type          "none" | "bullet" | "numbered"
    bullet_char        int (unicode code point)
    numbering_format   str   InDesign format, e.g. "1, 2, 3, 4..."
    numbering_start    int|None
    numbering_continue bool
    list_level         int (1-based)
    shading            str|None   colour name (block background-color)
    border             None | {"left": float, "right": float, "color": str}  (block border-left/right)
    rule_above / rule_below   None | {"weight": float, "color": str, "offset": float}
    hyphenation        bool
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

CHAR_KEYS = (
    "font_family", "font_style", "font_size", "leading", "color", "tracking",
    "caps", "position", "underline", "strike", "highlight", "no_break",
)
PARA_KEYS = (
    "align", "space_before", "space_after", "left_indent", "right_indent",
    "first_line_indent", "keep_with_next", "keep_together", "start_paragraph",
    "list_type", "bullet_char", "numbering_format", "numbering_start",
    "numbering_continue", "list_level", "shading", "border", "rule_above", "rule_below",
    "hyphenation",
)
ALL_KEYS = CHAR_KEYS + PARA_KEYS

IR_VERSION = 1


# --------------------------------------------------------------------------- #
# Resources
# --------------------------------------------------------------------------- #
@dataclass
class Color:
    name: str
    space: str = "RGB"                      # "RGB" | "CMYK"
    values: List[float] = field(default_factory=list)


@dataclass
class Style:
    name: str
    kind: str = "paragraph"                 # "paragraph" | "character"
    based_on: Optional[str] = None          # paragraph styles only
    props: Dict[str, Any] = field(default_factory=dict)
    source: str = ""                        # CSS signature it was derived from (info only)


@dataclass
class Asset:
    id: str
    kind: str                               # "image" | "font"
    source: str                             # original src / url as written in the HTML/CSS
    path: str                               # absolute local path of the packaged copy
    format: str = ""                        # JPEG, PNG, GIF, TIFF, SVG, PSD, PDF, EPS, ...
    width_px: float = 0.0
    height_px: float = 0.0
    ppi: float = 72.0
    family: str = ""                        # fonts only


@dataclass
class PageSetup:
    width: float = 595.276                  # A4
    height: float = 841.89
    margin_top: float = 56.693
    margin_bottom: float = 56.693
    margin_left: float = 56.693             # inside margin when facing_pages
    margin_right: float = 56.693            # outside margin when facing_pages
    columns: int = 1
    gutter: float = 12.0
    facing_pages: bool = False
    page_count: Optional[int] = None        # None -> estimated automatically

    @property
    def content_width(self) -> float:
        return self.width - self.margin_left - self.margin_right

    @property
    def content_height(self) -> float:
        return self.height - self.margin_top - self.margin_bottom

    @property
    def column_width(self) -> float:
        cols = max(1, self.columns)
        return (self.content_width - self.gutter * (cols - 1)) / cols


# --------------------------------------------------------------------------- #
# Story content
# --------------------------------------------------------------------------- #
@dataclass
class TextRun:
    text: str
    char_style: Optional[str] = None
    overrides: Dict[str, Any] = field(default_factory=dict)
    link: Optional[str] = None
    type: str = "text"


@dataclass
class ImageRun:
    asset: str
    width: float
    height: float
    char_style: Optional[str] = None
    overrides: Dict[str, Any] = field(default_factory=dict)
    alt: str = ""
    type: str = "image"


Run = Union[TextRun, ImageRun]


@dataclass
class Paragraph:
    style: str
    runs: List[Run] = field(default_factory=list)
    overrides: Dict[str, Any] = field(default_factory=dict)
    source: str = ""                        # e.g. "p.intro#main" (info only)
    type: str = "paragraph"

    def plain_text(self) -> str:
        return "".join(r.text for r in self.runs if isinstance(r, TextRun))


@dataclass
class Cell:
    blocks: List["Block"] = field(default_factory=list)
    row_span: int = 1
    col_span: int = 1
    # fill (colour|None), inset_top/right/bottom/left, vertical_align ("top"|"middle"|"bottom"),
    # edge_top/right/bottom/left: None | {"weight": float, "color": str}
    props: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Row:
    cells: List[Cell] = field(default_factory=list)
    kind: str = "body"                      # "header" | "body" | "footer"
    min_height: float = 3.0


@dataclass
class Table:
    style: str                              # paragraph style of the anchoring paragraph
    rows: List[Row] = field(default_factory=list)
    column_widths: List[float] = field(default_factory=list)
    overrides: Dict[str, Any] = field(default_factory=dict)   # anchor paragraph overrides
    props: Dict[str, Any] = field(default_factory=dict)       # border_top/right/bottom/left
    source: str = ""
    type: str = "table"


Block = Union[Paragraph, Table]


@dataclass
class Story:
    id: str
    blocks: List[Block] = field(default_factory=list)


@dataclass
class Document:
    page: PageSetup = field(default_factory=PageSetup)
    colors: Dict[str, Color] = field(default_factory=dict)
    paragraph_styles: Dict[str, Style] = field(default_factory=dict)
    character_styles: Dict[str, Style] = field(default_factory=dict)
    assets: Dict[str, Asset] = field(default_factory=dict)
    stories: List[Story] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    # ---------------- helpers ----------------
    def resolve_paragraph_style(self, name: str) -> Dict[str, Any]:
        """Fully resolved props of a paragraph style (walks the based_on chain)."""
        chain, seen = [], set()
        cur = name
        while cur and cur in self.paragraph_styles and cur not in seen:
            seen.add(cur)
            chain.append(self.paragraph_styles[cur])
            cur = self.paragraph_styles[cur].based_on
        out: Dict[str, Any] = {}
        for st in reversed(chain):
            out.update(st.props)
        return out

    def iter_paragraphs(self):
        """Yield every Paragraph, including those inside table cells."""
        def walk(blocks):
            for b in blocks:
                if isinstance(b, Paragraph):
                    yield b
                elif isinstance(b, Table):
                    for row in b.rows:
                        for cell in row.cells:
                            yield from walk(cell.blocks)
        for s in self.stories:
            yield from walk(s.blocks)

    def iter_blocks(self):
        def walk(blocks):
            for b in blocks:
                yield b
                if isinstance(b, Table):
                    for row in b.rows:
                        for cell in row.cells:
                            yield from walk(cell.blocks)
        for s in self.stories:
            yield from walk(s.blocks)

    def add_color(self, rgb, space: str = "RGB") -> str:
        """Register an (r, g, b) tuple and return the colour name to use in props."""
        r, g, b = (int(round(max(0, min(255, v)))) for v in rgb[:3])
        if (r, g, b) == (0, 0, 0):
            return "Black"
        if space.upper() == "CMYK":
            c, m, y, k = rgb_to_cmyk(r, g, b)
            name = f"C={c} M={m} Y={y} K={k}"
            if name not in self.colors:
                self.colors[name] = Color(name, "CMYK", [c, m, y, k])
        else:
            name = f"R={r} G={g} B={b}"
            if name not in self.colors:
                self.colors[name] = Color(name, "RGB", [r, g, b])
        return name

    # ---------------- (de)serialisation ----------------
    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["ir_version"] = IR_VERSION
        return d

    def to_json(self, path: Union[str, Path, None] = None, indent: int = 1) -> str:
        text = json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)
        if path:
            Path(path).write_text(text, encoding="utf-8")
        return text

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Document":
        doc = cls()
        doc.page = PageSetup(**d.get("page", {}))
        doc.colors = {k: Color(**v) for k, v in d.get("colors", {}).items()}
        doc.paragraph_styles = {k: Style(**v) for k, v in d.get("paragraph_styles", {}).items()}
        doc.character_styles = {k: Style(**v) for k, v in d.get("character_styles", {}).items()}
        doc.assets = {k: Asset(**v) for k, v in d.get("assets", {}).items()}
        doc.stories = [Story(id=s["id"], blocks=[_block_from_dict(b) for b in s.get("blocks", [])])
                       for s in d.get("stories", [])]
        doc.metadata = d.get("metadata", {})
        doc.warnings = d.get("warnings", [])
        return doc

    @classmethod
    def from_json(cls, path: Union[str, Path]) -> "Document":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def _run_from_dict(d):
    d = dict(d)
    t = d.pop("type", "text")
    return ImageRun(**d) if t == "image" else TextRun(**d)


def _block_from_dict(d):
    d = dict(d)
    t = d.pop("type", "paragraph")
    if t == "table":
        rows = []
        for r in d.pop("rows", []):
            cells = [Cell(blocks=[_block_from_dict(b) for b in c.get("blocks", [])],
                          row_span=c.get("row_span", 1), col_span=c.get("col_span", 1),
                          props=c.get("props", {})) for c in r.get("cells", [])]
            rows.append(Row(cells=cells, kind=r.get("kind", "body"), min_height=r.get("min_height", 3.0)))
        return Table(rows=rows, **d)
    runs = [_run_from_dict(r) for r in d.pop("runs", [])]
    return Paragraph(runs=runs, **d)


# --------------------------------------------------------------------------- #
# Table grid helper (shared by builder, layout estimate and writer)
# --------------------------------------------------------------------------- #
def table_grid(rows: List[Row]):
    """Place cells honouring row/col spans.

    Returns ``(positions, n_cols)`` where positions is a list of
    ``(row_index, col_index, cell)``.
    """
    occupied = set()
    positions = []
    n_cols = 0
    for r, row in enumerate(rows):
        c = 0
        for cell in row.cells:
            while (r, c) in occupied:
                c += 1
            rs = max(1, min(cell.row_span, len(rows) - r))
            cs = max(1, cell.col_span)
            cell.row_span = rs
            for dr in range(rs):
                for dc in range(cs):
                    occupied.add((r + dr, c + dc))
            positions.append((r, c, cell))
            c += cs
            n_cols = max(n_cols, c)
        n_cols = max(n_cols, max((cc + 1 for (rr, cc) in occupied if rr == r), default=0))
    return positions, n_cols


def rgb_to_cmyk(r, g, b):
    if (r, g, b) == (0, 0, 0):
        return 0, 0, 0, 100
    rp, gp, bp = r / 255, g / 255, b / 255
    k = 1 - max(rp, gp, bp)
    c = (1 - rp - k) / (1 - k)
    m = (1 - gp - k) / (1 - k)
    y = (1 - bp - k) / (1 - k)
    return tuple(int(round(v * 100)) for v in (c, m, y, k))
