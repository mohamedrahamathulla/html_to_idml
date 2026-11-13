"""Converter configuration.  Every option can be set from YAML/JSON, Python or the CLI."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Dict, Optional, Union

PAGE_SIZES = {  # width, height in pt
    "a3": (841.89, 1190.551), "a4": (595.276, 841.89), "a5": (419.528, 595.276),
    "a6": (297.638, 419.528), "b4": (708.661, 1000.63), "b5": (498.898, 708.661),
    "letter": (612.0, 792.0), "legal": (612.0, 1008.0), "tabloid": (792.0, 1224.0),
    "ledger": (1224.0, 792.0), "executive": (522.0, 756.0),
}

DEFAULTS: Dict[str, Any] = {
    "page": {
        "size": None,             # "A4" | "Letter" | "210mm 297mm" | None (CSS @page, then A4)
        "orientation": None,      # "portrait" | "landscape"
        "width": None, "height": None,
        "margins": None,          # "20mm" | [top, right, bottom, left] | {"top": ...}
        "columns": 1,
        "gutter": "12pt",
        "facing_pages": False,
        "page_count": None,       # None -> estimate, fit-pages.jsx finishes the job
        "use_css_page_rule": True,
    },
    "css": {
        "media": "screen",        # which @media rules apply ("screen" = what the browser shows)
        "viewport_width": 1280,   # px, for media queries and vw units
        "viewport_height": 800,
        "extra_stylesheets": [],  # more CSS files applied after the page's own CSS
        "user_css": "",           # CSS text applied last (highest author priority)
        "ignore_body_margin": True,
    },
    "fonts": {
        "map": {},                # {"Helvetica Neue": "Helvetica", "Open Sans": "Open Sans"}
        "generic": {},            # override generic families {"serif": "Minion Pro"}
        "default_family": "Times New Roman",
        "style_names": {},        # {"Segoe UI": {"600": "Semibold", "300": "Light"}}
        "weight_mode": "simple",  # "simple" (Regular/Bold) | "full" (Thin..Black)
        "check_installed": True,  # pick the first family of a stack that is installed (Windows)
        "copy_webfonts": True,
        "package_fonts": True,    # copy the installed font files used into "Document fonts"
                                  # (InDesign activates them when the document opens; check licences)
    },
    "styles": {
        "base_style": "Body",     # all paragraph styles are based on this one
        "names": {},              # {"h1": "Heading 1", "p.lead": "Lead", ".note": "Note"} (CSS selectors)
        "char_names": {},         # {"strong": "Bold", "a": "Hyperlink"}
        "overrides": {},          # {"Heading 1": {"font_size": 28, "color": "R=200 G=0 B=0"}}
        "char_overrides": {},
        "name_with_class": True,  # p.intro -> style "p.intro"
        "heading_keep_with_next": 2,
        "hyphenation": False,     # browsers do not hyphenate
    },
    "images": {
        "embed": True,            # embed image data inside the IDML (travels with the file)
        "download_remote": True,  # fetch http(s) images/CSS (in parallel, browser-like)
        "url_map": {},            # {"https://site/img.jpg": "C:/saved/img.jpg"} local replacements
        "convert_unsupported": True,  # WEBP/AVIF/ICO -> PNG
        "inline_svg": True,       # export inline <svg> as placed .svg graphics
        "default_ppi": 72,
        "fit_to_column": True,
    },
    "links": {"hyperlinks": True},
    "idml": {
        "dom_version": "16.0",    # 16.0 = InDesign 2021; use "8.0" for CS6 compatibility
        "color_space": "RGB",     # "RGB" | "CMYK"
        "ruler_units": "Points",  # Points | Millimeters | Inches | Picas | Centimeters
        "smart_text_reflow": True,
        "paragraph_shading": True,   # needs dom_version >= 14
    },
    "output": {
        "links_dir": "Links",
        "fonts_dir": "Document fonts",
        "write_ir": False,
        "write_jsx": True,
        "write_report": True,
        "zip_package": True,      # <name>-package.zip: idml + Links + Document fonts + script
    },
    "plugins": [],                # python files defining transform_html / transform_ir hooks
}


def deep_merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config(dict):
    """A nested dict with attribute access: ``cfg.page["columns"]`` or ``cfg["page"]["columns"]``."""

    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError as e:
            raise AttributeError(item) from e

    @classmethod
    def load(cls, source: Union[None, str, Path, Dict[str, Any]] = None,
             **overrides) -> "Config":
        data: Dict[str, Any] = {}
        if isinstance(source, dict):
            data = source
        elif source:
            p = Path(source)
            text = p.read_text(encoding="utf-8")
            if p.suffix.lower() in (".yaml", ".yml"):
                import yaml
                data = yaml.safe_load(text) or {}
            else:
                data = json.loads(text)
        merged = deep_merge(DEFAULTS, data)
        for dotted, value in overrides.items():
            if value is None:
                continue
            node = merged
            parts = dotted.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
        return cls(merged)


def length_pt(value, default: Optional[float] = None) -> Optional[float]:
    """'20mm' / '1in' / 36 / '12pt' / '16px' -> points."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    m = re.fullmatch(r"\s*(-?\d*\.?\d+)\s*(pt|px|mm|cm|in|pc|p)?\s*", str(value).lower())
    if not m:
        return default
    n = float(m.group(1))
    unit = m.group(2) or "pt"
    return n * {"pt": 1, "px": 0.75, "mm": 72 / 25.4, "cm": 72 / 2.54, "in": 72, "pc": 12,
                "p": 12}[unit]


def parse_margins(value):
    """Return (top, right, bottom, left) in pt or None."""
    if value is None:
        return None
    if isinstance(value, dict):
        return tuple(length_pt(value.get(k), 0.0) for k in ("top", "right", "bottom", "left"))
    if isinstance(value, (list, tuple)):
        vals = [length_pt(v, 0.0) for v in value]
    else:
        vals = [length_pt(v, 0.0) for v in str(value).split()]
    if not vals:
        return None
    if len(vals) == 1:
        vals *= 4
    elif len(vals) == 2:
        vals = [vals[0], vals[1], vals[0], vals[1]]
    elif len(vals) == 3:
        vals = [vals[0], vals[1], vals[2], vals[1]]
    return tuple(vals[:4])


def parse_page_size(value):
    """'A4' | 'letter landscape' | '210mm 297mm' -> (w, h) or None."""
    if not value:
        return None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return length_pt(value[0]), length_pt(value[1])
    tokens = str(value).lower().replace(",", " ").split()
    landscape = "landscape" in tokens
    tokens = [t for t in tokens if t not in ("landscape", "portrait")]
    size = None
    if tokens and tokens[0] in PAGE_SIZES:
        size = PAGE_SIZES[tokens[0]]
    elif len(tokens) >= 2 and "x" not in tokens[0]:
        w, h = length_pt(tokens[0]), length_pt(tokens[1])
        if w and h:
            size = (w, h)
    elif len(tokens) == 1 and "x" in tokens[0]:
        a, b = tokens[0].split("x", 1)
        w, h = length_pt(a), length_pt(b)
        if w and h:
            size = (w, h)
    elif len(tokens) == 1:
        w = length_pt(tokens[0])
        if w:
            size = (w, w)
    if size and landscape and size[0] < size[1]:
        size = (size[1], size[0])
    return size
