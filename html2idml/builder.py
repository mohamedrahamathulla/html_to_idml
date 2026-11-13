"""HTML (+ computed CSS)  ->  IR Document.

Walks the DOM exactly like a browser's block/inline formatting would, and
turns it into paragraphs, character runs, inline images and tables, together
with paragraph/character styles derived from the CSS.

Control hooks available directly in the HTML:
    data-idml-style="Heading 1"          force the paragraph style name
    data-idml-char-style="Emphasis"      force the character style name
    data-idml-props='{"keep_with_next": 3}'       paragraph property overrides (JSON)
    data-idml-char-props='{"tracking": 50}'       character property overrides (JSON)
    data-idml-skip                        do not convert this element
    data-idml-page-break                  start this element on a new page
"""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import cssselect
from lxml import etree

from .cssvalues import LengthContext, blend_on_white, to_pt
from .ir import (CHAR_KEYS, PARA_KEYS, Cell, Document, ImageRun, Paragraph, Row, Story,
                 Style, Table, TextRun, table_grid)

BLOCK_DISPLAYS = {"block", "list-item", "flex", "grid", "flow-root", "table", "table-caption",
                  "table-row-group", "table-header-group", "table-footer-group", "table-row",
                  "table-cell", "run-in", "inline-table", "contents", "table-column",
                  "table-column-group", "ruby"}
BULLETS = {"disc": 8226, "circle": 9702, "square": 9642, "disclosure-closed": 9656,
           "disclosure-open": 9662}
NUMBER_FORMATS = {
    "decimal": "1, 2, 3, 4...", "decimal-leading-zero": "01, 02, 03...",
    "lower-alpha": "a, b, c, d...", "lower-latin": "a, b, c, d...",
    "upper-alpha": "A, B, C, D...", "upper-latin": "A, B, C, D...",
    "lower-roman": "i, ii, iii, iv...", "upper-roman": "I, II, III, IV...",
}
OL_TYPE_ATTR = {"1": "decimal", "a": "lower-alpha", "A": "upper-alpha", "i": "lower-roman",
                "I": "upper-roman"}
SVG_ATTR_CASE = ["viewBox", "preserveAspectRatio", "gradientUnits", "gradientTransform",
                 "patternUnits", "patternContentUnits", "patternTransform", "clipPathUnits",
                 "markerWidth", "markerHeight", "markerUnits", "refX", "refY", "stdDeviation",
                 "textLength", "lengthAdjust", "startOffset", "spreadMethod", "maskUnits",
                 "maskContentUnits", "filterUnits", "primitiveUnits", "baseFrequency",
                 "numOctaves", "kernelMatrix", "tableValues", "pathLength", "attributeName",
                 "repeatCount", "keySplines", "keyTimes", "calcMode", "xChannelSelector",
                 "yChannelSelector", "diffuseConstant", "specularExponent", "surfaceScale",
                 "pointsAtX", "pointsAtY", "pointsAtZ", "limitingConeAngle", "edgeMode",
                 "targetX", "targetY", "kernelUnitLength", "zoomAndPan"]
SVG_TAG_CASE = ["linearGradient", "radialGradient", "clipPath", "foreignObject", "textPath",
                "feGaussianBlur", "feOffset", "feBlend", "feColorMatrix", "feComposite",
                "feFlood", "feMerge", "feMergeNode", "feMorphology", "feTurbulence",
                "feDropShadow", "feImage", "feTile", "feComponentTransfer", "feFuncA",
                "feFuncR", "feFuncG", "feFuncB", "feDisplacementMap", "feDiffuseLighting",
                "feSpecularLighting", "fePointLight", "feSpotLight", "feDistantLight",
                "feConvolveMatrix", "animateTransform", "animateMotion", "altGlyph"]


class InlineCtx(NamedTuple):
    chain: Tuple[str, ...] = ()
    deco: frozenset = frozenset()
    position: str = "normal"
    base_size: Optional[float] = None
    size: float = 12.0
    highlight: Any = None
    link: Optional[str] = None
    explicit: Optional[str] = None
    char_props: Optional[dict] = None


class BlockCtx(NamedTuple):
    left: float = 0.0
    right: float = 0.0
    width: float = 480.0
    shading: Any = None
    deco: frozenset = frozenset()
    list_depth: int = 0
    in_cell: bool = False
    side_border: Any = None      # {"left": w, "right": w, "color": name} from CSS border-left/right


class _Para:
    __slots__ = ("owner", "bctx", "items")

    def __init__(self, owner, bctx):
        self.owner = owner
        self.bctx = bctx
        self.items: List[list] = []     # [kind, payload, cs, ictx]

    def ends_with_space(self) -> bool:
        for kind, payload, _, _ in reversed(self.items):
            if kind == "text":
                if payload:
                    return payload[-1] in (" ", " ")
                continue
            return False
        return True


def _r(v, nd=3):
    return round(v, nd) if isinstance(v, float) else v


class Builder:
    def __init__(self, root, cascade, config, doc: Document, assets, fonts, base: str,
                 warn):
        self.root = root
        self.cascade = cascade
        self.cfg = config
        self.doc = doc
        self.assets = assets
        self.fonts = fonts
        self.base = base
        self.warn = warn
        self.scfg = config["styles"]
        self.color_space = config["idml"]["color_space"]
        self.pending: List[tuple] = []
        self._para_names = self._selector_names(self.scfg.get("names") or {})
        self._char_names = self._selector_names(self.scfg.get("char_names") or {})
        self._reset_state([])

    # ------------------------------------------------------------------ setup
    def _selector_names(self, mapping: Dict[str, str]) -> Dict[Any, str]:
        out: Dict[Any, str] = {}
        tr = cssselect.HTMLTranslator()
        for sel, name in mapping.items():
            try:
                xp = tr.css_to_xpath(sel, prefix="descendant-or-self::")
                for el in etree.XPath(xp)(self.root):
                    out[el] = name
            except Exception:
                self.warn(f"invalid selector in styles config: {sel!r}")
        return out

    def _reset_state(self, container):
        self.container = container
        self.para: Optional[_Para] = None
        self.gap_fixed = 0.0
        self.gap_pos = 0.0
        self.gap_neg = 0.0
        self.pending_break = False
        self.pending_list: Optional[dict] = None
        self.pending_rule_above: Optional[dict] = None
        self.last_full: Optional[dict] = None
        self.emitted = 0

    @contextmanager
    def sub_container(self, container):
        saved = (self.container, self.para, self.gap_fixed, self.gap_pos, self.gap_neg,
                 self.pending_break, self.pending_list, self.pending_rule_above,
                 self.last_full, self.emitted)
        self._reset_state(container)
        try:
            yield
        finally:
            (self.container, self.para, self.gap_fixed, self.gap_pos, self.gap_neg,
             self.pending_break, self.pending_list, self.pending_rule_above,
             self.last_full, self.emitted) = saved

    # ------------------------------------------------------------------ helpers
    def style(self, el) -> dict:
        return self.cascade.style(el)

    def color(self, rgba) -> Optional[str]:
        if rgba is None or isinstance(rgba, str):
            return None
        if rgba[3] <= 0:
            return None
        rgb = blend_on_white(rgba) if rgba[3] < 1 else rgba[:3]
        return self.doc.add_color(rgb, self.color_space)

    def length(self, cs, prop, base_width=None) -> float:
        v = cs.get(prop)
        ctx = LengthContext(cs["font-size"], self.cascade.root_font_size, base_width,
                            *self.cascade.viewport_pt)
        r = to_pt(v, ctx)
        return r if r is not None else 0.0

    def border(self, cs, side) -> float:
        if cs.get(f"border-{side}-style") in (None, "none", "hidden"):
            return 0.0
        return max(0.0, self.length(cs, f"border-{side}-width"))

    @staticmethod
    def tag(el) -> str:
        return el.tag.lower() if isinstance(el.tag, str) else ""

    def signature(self, el) -> str:
        tag = self.tag(el)
        cls = (el.get("class") or "").split()
        if cls and self.scfg.get("name_with_class", True):
            return f"{tag}.{cls[0]}"
        return tag

    def para_name(self, el) -> str:
        explicit = el.get("data-idml-style")
        if explicit:
            return explicit
        if el in self._para_names:
            return self._para_names[el]
        return self.signature(el)

    # ------------------------------------------------------------------ props
    def leading(self, cs, size) -> Any:
        lh = cs["line-height"]
        kind, val = lh
        if kind == "normal":
            return "auto"
        if kind == "factor":
            return _r(val * size)
        return _r(val)

    def char_props(self, cs, ictx: Optional[InlineCtx], bctx: BlockCtx) -> Dict[str, Any]:
        family = self.fonts.family(cs["font-family"])
        italic = cs["font-style"] in ("italic", "oblique")
        size = cs["font-size"]
        position = "normal"
        if ictx is not None and ictx.position != "normal":
            position = ictx.position
            if ictx.base_size:
                size = ictx.base_size
        deco = bctx.deco | (ictx.deco if ictx is not None else cs["text-decoration-line"])
        caps = "normal"
        if cs["text-transform"] == "uppercase":
            caps = "all_caps"
        elif cs["font-variant"] == "small-caps" or cs["font-variant-caps"] in (
                "small-caps", "all-small-caps"):
            caps = "small_caps"
        ls = cs["letter-spacing"]
        tracking = (ls / size * 1000.0) if isinstance(ls, float) and size else 0.0
        props = {
            "font_family": family,
            "font_style": self.fonts.style(family, cs["font-weight"], italic),
            "font_size": _r(size),
            "leading": self.leading(cs, size),
            "color": self.color(cs["color"]) or "Black",
            "tracking": _r(tracking, 1),
            "caps": caps,
            "position": position,
            "underline": "underline" in deco,
            "strike": "line-through" in deco,
            "highlight": self.color(ictx.highlight) if ictx is not None and ictx.highlight else None,
            "no_break": cs["white-space"] == "nowrap",
        }
        if ictx is not None and ictx.char_props:
            props.update(ictx.char_props)
        return props

    def para_props(self, cs, bctx: BlockCtx, owner) -> Dict[str, Any]:
        align = {"left": "left", "start": "left", "right": "right", "end": "right",
                 "center": "center", "-webkit-center": "center", "-moz-center": "center",
                 "justify": "justify"}.get(cs["text-align"], "left")
        if cs["direction"] == "rtl" and cs["text-align"] in ("start",):
            align = "right"
        ti = cs["text-indent"]
        if isinstance(ti, str):
            ti = to_pt(ti, LengthContext(cs["font-size"], percent_base=bctx.width)) or 0.0
        tag = self.tag(owner)
        kwn = 0
        if re.fullmatch(r"h[1-6]", tag):
            kwn = int(self.scfg.get("heading_keep_with_next", 2) or 0)
        if cs["break-after"] == "avoid":
            kwn = max(kwn, 2)
        return {
            "align": align,
            "space_before": 0.0,
            "space_after": 0.0,
            "left_indent": _r(max(0.0, bctx.left)),
            "right_indent": _r(max(0.0, bctx.right)),
            "first_line_indent": _r(ti),
            "keep_with_next": kwn,
            "keep_together": cs["break-inside"] in ("avoid", "avoid-page"),
            "start_paragraph": "anywhere",
            "list_type": "none",
            "bullet_char": 8226,
            "numbering_format": "1, 2, 3, 4...",
            "numbering_start": None,
            "numbering_continue": True,
            "list_level": 1,
            "shading": self.color(bctx.shading) if bctx.shading else None,
            "border": bctx.side_border,
            "rule_above": None,
            "rule_below": None,
            "hyphenation": bool(self.scfg.get("hyphenation", False)) or cs["hyphens"] == "auto",
        }

    # ------------------------------------------------------------------ gaps (margin collapsing)
    def gap_margin(self, m: float):
        if m >= 0:
            self.gap_pos = max(self.gap_pos, m)
        else:
            self.gap_neg = min(self.gap_neg, m)

    def gap_fix(self, extra: float):
        self.gap_fixed += self.gap_pos + self.gap_neg + extra
        self.gap_pos = self.gap_neg = 0.0

    def take_gap(self) -> float:
        v = self.gap_fixed + self.gap_pos + self.gap_neg
        self.gap_fixed = self.gap_pos = self.gap_neg = 0.0
        return max(0.0, v)

    # ------------------------------------------------------------------ entry
    def build(self) -> Document:
        body = self.root.find(".//body")
        if body is None:
            body = self.root
        page = self.doc.page
        story = Story(id="main")
        self.doc.stories.append(story)
        self._reset_state(story.blocks)
        bcs = self.style(body)
        ctx = BlockCtx(width=page.column_width)
        # Base style = body
        base_name = self.scfg.get("base_style") or "Body"
        base_props = self.para_props(bcs, ctx, body)
        base_props.update(self.char_props(bcs, None, ctx))
        base_props["keep_with_next"] = 0
        self.doc.paragraph_styles[base_name] = Style(base_name, "paragraph", None, base_props,
                                                     source="body")
        self.base_name = base_name
        self.walk_block(body, ctx, is_root=True)
        self.flush()
        self.finalize()
        return self.doc

    # ------------------------------------------------------------------ block level
    def walk_block(self, el, bctx: BlockCtx, is_root: bool = False):
        cs = self.style(el)
        disp = cs["display"]
        if disp == "none" or el.get("data-idml-skip") is not None:
            return
        if cs["visibility"] in ("hidden", "collapse") and not is_root:
            return
        tag = self.tag(el)
        if disp in ("table", "inline-table"):
            self.table(el, bctx)
            return
        if disp == "contents":
            self.walk_children(el, cs, bctx)
            return
        self.flush()
        if tag == "hr":
            self.hr(el, cs, bctx)
            return

        width = bctx.width
        ign = is_root and self.cfg["css"].get("ignore_body_margin", True)
        sides = {}
        for s in ("top", "right", "bottom", "left"):
            sides[f"m{s[0]}"] = 0.0 if ign else self.length(cs, f"margin-{s}", width)
            sides[f"p{s[0]}"] = 0.0 if ign else self.length(cs, f"padding-{s}", width)
            sides[f"b{s[0]}"] = 0.0 if ign else self.border(cs, s)
        if tag in ("html", "body") and not ign:
            pass

        if cs["break-before"] in ("page", "left", "right", "always") or \
                el.get("data-idml-page-break") is not None:
            self.pending_break = True

        self.gap_margin(sides["mt"])
        if sides["pt"] + sides["bt"] > 0:
            self.gap_fix(sides["pt"] + sides["bt"])
        if sides["bt"] > 0:
            self.pending_rule_above = {
                "weight": _r(sides["bt"]),
                "color": self.color(cs["border-top-color"]) or "Black",
                "offset": _r(cs["font-size"] * 0.8 + sides["pt"]),
            }

        left = bctx.left + sides["ml"] + sides["pl"] + sides["bl"]
        right = bctx.right + sides["mr"] + sides["pr"] + sides["br"]
        inner = width - (sides["ml"] + sides["pl"] + sides["bl"] + sides["mr"] + sides["pr"] + sides["br"])
        # explicit width / max-width narrower than the available space
        for prop in ("width", "max-width"):
            wv = cs.get(prop)
            if wv and wv not in ("auto", "none") and not is_root:
                w = to_pt(wv, LengthContext(cs["font-size"], self.cascade.root_font_size,
                                            width, *self.cascade.viewport_pt))
                if w is not None and 0 < w < inner:
                    d = inner - w
                    if str(cs.get("margin-left")).strip() == "auto" and \
                            str(cs.get("margin-right")).strip() == "auto":
                        left += d / 2
                        right += d / 2
                    else:
                        right += d
                    inner = w
        nctx = bctx._replace(
            left=left, right=right, width=max(20.0, inner),
            shading=cs["background-color"] if cs["background-color"] else bctx.shading,
            deco=bctx.deco | cs["text-decoration-line"],
            list_depth=bctx.list_depth + (1 if tag in ("ul", "ol", "menu", "dir") else 0),
        )
        if (sides["bl"] > 0 or sides["br"] > 0) and not is_root:
            side = "left" if sides["bl"] > 0 else "right"
            nctx = nctx._replace(side_border={
                "left": _r(sides["bl"]), "right": _r(sides["br"]),
                "color": self.color(cs[f"border-{side}-color"]) or "Black"})
        if is_root:
            nctx = nctx._replace(shading=None, side_border=None)

        if disp == "list-item":
            self.start_list_item(el, cs, nctx)

        start_emitted = self.emitted
        if tag == "pre" and el.text and el.text.startswith(("\n", "\r\n")):
            el.text = el.text[2:] if el.text.startswith("\r\n") else el.text[1:]
        self.walk_children(el, cs, nctx)
        self.flush()

        if sides["bb"] > 0 and self.emitted > start_emitted and self.last_full is not None:
            self.last_full["rule_below"] = {
                "weight": _r(sides["bb"]),
                "color": self.color(cs["border-bottom-color"]) or "Black",
                "offset": _r(cs["font-size"] * 0.25 + sides["pb"]),
            }
        if sides["pb"] + sides["bb"] > 0:
            self.gap_fix(sides["pb"] + sides["bb"])
        self.gap_margin(sides["mb"])
        if cs["break-after"] in ("page", "left", "right", "always"):
            self.pending_break = True
        if disp == "list-item":
            self.pending_list = None
        if self.emitted == start_emitted:
            self.pending_rule_above = None

    def start_list_item(self, el, cs, nctx: BlockCtx):
        lst = cs["list-style-type"]
        parent = el.getparent()
        ptag = self.tag(parent) if parent is not None else ""
        if ptag == "ol" and parent.get("type") in OL_TYPE_ATTR and lst == "decimal":
            lst = OL_TYPE_ATTR[parent.get("type")]
        level = max(1, nctx.list_depth)
        info: Dict[str, Any] = {"list_level": level}
        if lst == "none" or cs.get("list-style-image", "none") != "none" and lst == "none":
            info["list_type"] = "none"
        elif lst in BULLETS:
            info["list_type"] = "bullet"
            info["bullet_char"] = BULLETS[lst]
        elif lst in NUMBER_FORMATS:
            info["list_type"] = "numbered"
            info["numbering_format"] = NUMBER_FORMATS[lst]
            siblings = [c for c in parent if self.tag(c) == "li"] if parent is not None else [el]
            if el.get("value", "").strip().lstrip("-").isdigit():
                info["numbering_start"] = int(el.get("value"))
                info["numbering_continue"] = False
            elif siblings and siblings[0] is el:
                start = (parent.get("start") or "1").strip() if parent is not None else "1"
                info["numbering_start"] = int(start) if start.lstrip("-").isdigit() else 1
                info["numbering_continue"] = False
        else:
            info["list_type"] = "bullet"
            info["bullet_char"] = 8226
        if info["list_type"] != "none":
            hang = min(cs["font-size"] * 1.5, max(0.0, nctx.left))
            info["first_line_indent"] = _r(-hang)
        info["_name"] = f"{self.signature(parent) if parent is not None else 'ul'} li" + \
            (f" {level}" if level > 1 else "")
        self.pending_list = info

    def hr(self, el, cs, bctx):
        self.gap_margin(self.length(cs, "margin-top", bctx.width))
        weight = max(self.border(cs, "top"), self.border(cs, "bottom"), 0.75)
        color = self.color(cs["border-top-color"]) or "Black"
        if cs["background-color"] is not None and weight <= 1:
            color = self.color(cs["background-color"]) or color
            h = self.length(cs, "height", bctx.width)
            weight = max(weight, h)
        full = self.para_props(cs, bctx, el)
        full.update(self.char_props(cs, None, bctx))
        full.update({"font_size": 1.0, "leading": _r(max(1.0, weight)),
                     "rule_below": {"weight": _r(weight), "color": color, "offset": 0.0},
                     "space_before": self.take_gap()})
        if self.pending_break:
            full["start_paragraph"] = "next_page"
            self.pending_break = False
        para = Paragraph(style=self.para_name(el), source="hr")
        self.container.append(para)
        self.pending.append((para, para.style, full, [], "hr",
                             self.natural(self.length(cs, "margin-top", bctx.width))))
        self.last_full = full
        self.emitted += 1
        self.gap_margin(self.length(cs, "margin-bottom", bctx.width))

    def walk_children(self, el, cs, bctx: BlockCtx):
        ictx = InlineCtx(size=cs["font-size"])
        self.pseudo(el, "before", ictx, bctx, el)
        self.add_text(el.text, cs, ictx, bctx, el)
        for child in el:
            if isinstance(child.tag, str):
                self.walk_node(child, bctx, ictx, el)
            self.add_text(child.tail, cs, ictx, bctx, el)
        self.pseudo(el, "after", ictx, bctx, el)

    def walk_node(self, child, bctx, ictx: InlineCtx, owner):
        ccs = self.style(child)
        if ccs["display"] == "none" or child.get("data-idml-skip") is not None:
            return
        tag = self.tag(child)
        if tag == "br":
            self.add_raw(" ", ccs, ictx, bctx, owner)
            return
        if tag == "wbr":
            self.add_raw("​", ccs, ictx, bctx, owner)
            return
        if tag == "img" or (tag == "input" and (child.get("type") or "").lower() == "image"):
            self.add_image(child, ccs, ictx, bctx, owner)
            return
        if tag == "svg":
            self.add_svg(child, ccs, ictx, bctx, owner)
            return
        if ccs["visibility"] in ("hidden", "collapse"):
            return
        if ccs["display"] in BLOCK_DISPLAYS and ccs["display"] != "contents":
            self.walk_block(child, bctx)
        elif ccs["display"] == "contents":
            for gc in child:
                if isinstance(gc.tag, str):
                    self.walk_node(gc, bctx, ictx, owner)
                self.add_text(gc.tail, ccs, ictx, bctx, owner)
        else:
            self.walk_inline(child, ccs, bctx, ictx, owner)

    # ------------------------------------------------------------------ inline level
    def extend_inline(self, el, cs, ictx: InlineCtx) -> InlineCtx:
        tag = self.tag(el)
        explicit = el.get("data-idml-char-style") or self._char_names.get(el) or ictx.explicit
        chain = ictx.chain + (self.signature(el),)
        va = cs["vertical-align"]
        position, base_size = ictx.position, ictx.base_size
        if va in ("super", "sub") or tag in ("sup", "sub") and va not in ("baseline",):
            new_pos = "superscript" if (va == "super" or (tag == "sup" and va != "sub")) else "subscript"
            if position == "normal":
                base_size = ictx.size
            position = new_pos
        link = ictx.link
        if tag == "a" and self.cfg["links"].get("hyperlinks", True):
            href = (el.get("href") or "").strip()
            if href and not href.startswith(("#", "javascript:")):
                link = href
        cp = ictx.char_props
        raw = el.get("data-idml-char-props")
        if raw:
            try:
                cp = dict(cp or {}, **json.loads(raw))
            except ValueError:
                self.warn(f"invalid JSON in data-idml-char-props: {raw!r}")
        return InlineCtx(chain=chain, deco=ictx.deco | cs["text-decoration-line"],
                         position=position, base_size=base_size, size=cs["font-size"],
                         highlight=cs["background-color"] or ictx.highlight, link=link,
                         explicit=explicit, char_props=cp)

    def walk_inline(self, el, cs, bctx, ictx, owner):
        nictx = self.extend_inline(el, cs, ictx)
        self.pseudo(el, "before", nictx, bctx, owner)
        self.add_text(el.text, cs, nictx, bctx, owner)
        for child in el:
            if isinstance(child.tag, str):
                self.walk_node(child, bctx, nictx, owner)
            self.add_text(child.tail, cs, nictx, bctx, owner)
        self.pseudo(el, "after", nictx, bctx, owner)

    def pseudo(self, el, which, ictx, bctx, owner):
        pcs = self.cascade.pseudo(el, which)
        if not pcs:
            return
        from .cssvalues import split_values
        out = []
        for tok in split_values(pcs["content"]):
            t = tok.strip()
            if t[:1] in "\"'" and t[-1:] == t[:1]:
                out.append(t[1:-1])
            elif t.startswith("attr("):
                out.append(el.get(t[5:-1].strip(), ""))
            elif t == "open-quote":
                out.append("“")
            elif t == "close-quote":
                out.append("”")
        text = "".join(out)
        if text:
            pictx = ictx._replace(chain=ictx.chain + (f"{self.signature(el)}-{which}",),
                                  deco=ictx.deco | pcs["text-decoration-line"],
                                  highlight=pcs["background-color"] or ictx.highlight)
            self.add_text(text, pcs, pictx, bctx, owner)

    def ensure_para(self, owner, bctx):
        if self.para is None:
            self.para = _Para(owner, bctx)
        return self.para

    def add_text(self, text, cs, ictx, bctx, owner):
        if not text:
            return
        ws = cs["white-space"]
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if ws in ("normal", "nowrap"):
            text = re.sub(r"[ \t\n\f]+", " ", text)
        elif ws == "pre-line":
            text = re.sub(r"[ \t\f]*\n[ \t\f]*", "\n", text)
            text = re.sub(r"[ \t\f]+", " ", text).replace("\n", " ")
        else:  # pre, pre-wrap, break-spaces
            text = text.replace("\n", " ")
        if ws in ("normal", "nowrap", "pre-line"):
            if self.para is None or self.para.ends_with_space():
                text = text.lstrip(" ")
        if not text:
            return
        tt = cs["text-transform"]
        if tt == "lowercase":
            text = text.lower()
        elif tt == "capitalize":
            text = re.sub(r"(^|[\s\- (\"'“])(\w)", lambda m: m.group(1) + m.group(2).upper(), text)
        self.ensure_para(owner, bctx).items.append(["text", text, cs, ictx])

    def add_raw(self, text, cs, ictx, bctx, owner):
        self.ensure_para(owner, bctx).items.append(["text", text, cs, ictx])

    def image_size(self, el, cs, asset, bctx) -> Tuple[float, float]:
        nat_w = asset.width_px or 300.0
        nat_h = asset.height_px or 150.0
        lctx = LengthContext(cs["font-size"], self.cascade.root_font_size, bctx.width,
                             *self.cascade.viewport_pt)
        w = to_pt(cs.get("width"), lctx)
        h = to_pt(cs.get("height"), LengthContext(cs["font-size"], self.cascade.root_font_size,
                                                   None, *self.cascade.viewport_pt))
        if w is None and el.get("width"):
            w = to_pt(el.get("width").strip(), lctx, allow_unitless=True)
        if h is None and el.get("height"):
            h = to_pt(el.get("height").strip(), lctx, allow_unitless=True)
        ratio = nat_h / nat_w if nat_w else 1.0
        if w and not h:
            h = w * ratio
        elif h and not w:
            w = h / ratio if ratio else h
        elif not w and not h:
            w, h = nat_w * 0.75, nat_h * 0.75
        mw = to_pt(cs.get("max-width"), lctx)
        if mw and w > mw:
            h, w = h * mw / w, mw
        if self.cfg["images"].get("fit_to_column", True) and w > bctx.width > 0:
            h, w = h * bctx.width / w, bctx.width
        max_h = self.doc.page.content_height * 0.97
        if h > max_h > 0:   # a graphic taller than the frame would be overset forever
            w, h = w * max_h / h, max_h
        return _r(max(1.0, w), 2), _r(max(1.0, h), 2)

    def add_image(self, el, cs, ictx, bctx, owner):
        candidates = image_candidates(el)
        asset = None
        for src in candidates:
            asset = self.assets.image(src, self.base)
            if asset is not None:
                break
        if asset is None:
            if not candidates:
                self.warn("an <img> has no src - left out")
            alt = el.get("alt")
            if alt:
                self.add_text(alt, cs, ictx, bctx, owner)
            return
        w, h = self.image_size(el, cs, asset, bctx)
        run = ImageRun(asset=asset.id, width=w, height=h, alt=el.get("alt") or "")
        self.ensure_para(owner, bctx).items.append(["image", run, cs, ictx])

    def add_svg(self, el, cs, ictx, bctx, owner):
        if not self.cfg["images"].get("inline_svg", True):
            return
        markup = etree.tostring(el, method="xml", encoding="unicode", with_tail=False)
        for name in SVG_ATTR_CASE:
            markup = re.sub(rf"(\s){name.lower()}=", rf"\g<1>{name}=", markup)
        for name in SVG_TAG_CASE:
            markup = re.sub(rf"(</?){name.lower()}\b", rf"\g<1>{name}", markup)
        if "xmlns=" not in markup.split(">", 1)[0]:
            markup = markup.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
        if "xlink:" in markup and "xmlns:xlink" not in markup.split(">", 1)[0]:
            markup = markup.replace("<svg", '<svg xmlns:xlink="http://www.w3.org/1999/xlink"', 1)
        asset = self.assets.svg_markup(('<?xml version="1.0" encoding="UTF-8"?>\n' + markup).encode("utf-8"))
        if asset is None:
            return
        w, h = self.image_size(el, cs, asset, bctx)
        run = ImageRun(asset=asset.id, width=w, height=h, alt="svg")
        self.ensure_para(owner, bctx).items.append(["image", run, cs, ictx])

    # ------------------------------------------------------------------ paragraph emission
    def flush(self):
        para, self.para = self.para, None
        if para is None:
            return
        items = para.items
        # trim trailing collapsible whitespace and one trailing forced line break
        while items:
            last = items[-1]
            if last[0] != "text":
                break
            ws = last[2]["white-space"]
            if ws in ("normal", "nowrap", "pre-line"):
                last[1] = last[1].rstrip(" ")
            if last[1].endswith(" "):
                last[1] = last[1][:-1]
                if ws in ("normal", "nowrap", "pre-line"):
                    last[1] = last[1].rstrip(" ")
                if not last[1]:
                    items.pop()
                break
            if not last[1]:
                items.pop()
                continue
            break
        has_content = any((k == "image") or (k == "text" and p) for k, p, _, _ in items)
        if not has_content:
            return
        owner = para.owner
        ocs = self.style(owner)
        bctx = para.bctx
        full = self.para_props(ocs, bctx, owner)
        full.update(self.char_props(ocs, None, bctx))
        name = self.para_name(owner)
        if self.pending_list is not None:
            info = dict(self.pending_list)
            self.pending_list = None
            list_name = info.pop("_name", name)
            full.update(info)
            if self.tag(owner) == "li" and not owner.get("data-idml-style") and owner not in self._para_names:
                name = list_name
        full["space_before"] = _r(self.take_gap())
        if self.pending_break:
            full["start_paragraph"] = "next_page"
            self.pending_break = False
        if self.pending_rule_above is not None:
            full["rule_above"] = self.pending_rule_above
            self.pending_rule_above = None
        raw = owner.get("data-idml-props")
        if raw:
            try:
                full.update(json.loads(raw))
            except ValueError:
                self.warn(f"invalid JSON in data-idml-props: {raw!r}")
        # build runs
        runs, run_meta = [], []
        auto_leading = False
        for kind, payload, cs, ictx in items:
            target = self.char_props(cs, ictx, bctx)
            cname = None
            if ictx.chain:
                cname = ictx.explicit or " ".join(ictx.chain)
            if kind == "image":
                if full.get("leading") != "auto" and payload.height > (
                        full["leading"] if isinstance(full["leading"], (int, float)) else 0):
                    auto_leading = True
                runs.append(payload)
                run_meta.append((payload, cname, target, bool(ictx.explicit)))
                continue
            text = payload
            prev = runs[-1] if runs else None
            if (isinstance(prev, TextRun) and run_meta[-1][1] == cname and
                    run_meta[-1][2] == target and prev.link == ictx.link):
                prev.text += text
                continue
            run = TextRun(text=text, link=ictx.link)
            runs.append(run)
            run_meta.append((run, cname, target, bool(ictx.explicit)))
        if auto_leading:
            full["leading"] = "auto"
            for meta in run_meta:
                meta[2]["leading"] = "auto"
        p = Paragraph(style=name, runs=runs, source=self._source(owner))
        self.container.append(p)
        own_mt = 0.0 if self.tag(owner) == "li" else self.length(ocs, "margin-top", bctx.width)
        self.pending.append((p, name, full, run_meta, self.signature(owner), self.natural(own_mt)))
        self.last_full = full
        self.emitted += 1

    def _source(self, el) -> str:
        s = self.signature(el)
        if el.get("id"):
            s += "#" + el.get("id")
        return s

    # ------------------------------------------------------------------ tables
    def table(self, el, bctx: BlockCtx):
        cs = self.style(el)
        self.flush()
        width = bctx.width
        mt = self.length(cs, "margin-top", width)
        mb = self.length(cs, "margin-bottom", width)
        if cs["break-before"] in ("page", "always") or el.get("data-idml-page-break") is not None:
            self.pending_break = True
        # caption(s) first
        for child in el:
            if isinstance(child.tag, str) and self.tag(child) == "caption":
                self.walk_block(child, bctx)
        self.gap_margin(mt)

        header, body, footer = [], [], []

        def collect(parent, kind):
            for ch in parent:
                if not isinstance(ch.tag, str):
                    continue
                t = self.tag(ch)
                d = self.style(ch)["display"]
                if d == "none":
                    continue
                if t == "tr" or d == "table-row":
                    (header if kind == "header" else footer if kind == "footer" else body).append(ch)
                elif t == "thead" or d == "table-header-group":
                    collect(ch, "header")
                elif t == "tfoot" or d == "table-footer-group":
                    collect(ch, "footer")
                elif t == "tbody" or d == "table-row-group":
                    collect(ch, kind)

        collect(el, "body")
        if not header and body:
            # a first row made only of <th> is treated as a header row (repeats on each page)
            first = body[0]
            cells = [c for c in first if isinstance(c.tag, str) and self.tag(c) in ("td", "th")]
            if cells and all(self.tag(c) == "th" for c in cells) and len(body) > 1:
                header.append(body.pop(0))
        tr_list = [(tr, "header") for tr in header] + [(tr, "body") for tr in body] + \
                  [(tr, "footer") for tr in footer]
        if not tr_list:
            return
        rows: List[Row] = []
        cell_els = []
        for tr, kind in tr_list:
            row = Row(kind=kind)
            trcs = self.style(tr)
            for td in tr:
                if not isinstance(td.tag, str) or self.tag(td) not in ("td", "th"):
                    continue
                if self.style(td)["display"] == "none":
                    continue
                cell = Cell(row_span=_int(td.get("rowspan"), 1), col_span=_int(td.get("colspan"), 1))
                row.cells.append(cell)
                cell_els.append((cell, td, tr))
            h = self.length(trcs, "height")
            row.min_height = _r(max(3.0, h))
            rows.append(row)
        positions, ncols = table_grid(rows)
        if ncols == 0:
            return

        # ---- table width and column widths
        avail = width - self.length(cs, "margin-left", width) - self.length(cs, "margin-right", width)
        tw = to_pt(cs.get("width"), LengthContext(cs["font-size"], self.cascade.root_font_size,
                                                  avail, *self.cascade.viewport_pt))
        if tw is None and el.get("width"):
            tw = to_pt(el.get("width"), LengthContext(cs["font-size"], percent_base=avail),
                       allow_unitless=True)
        cell_lookup = {id(c): (td, tr) for c, td, tr in cell_els}
        fixed = [None] * ncols
        pref = [0.0] * ncols
        minw = [0.0] * ncols
        for r, c, cell in positions:
            td, _ = cell_lookup[id(cell)]
            tcs = self.style(td)
            ins = self.length(tcs, "padding-left", avail) + self.length(tcs, "padding-right", avail)
            text = " ".join("".join(td.itertext()).split())
            fs = tcs["font-size"]
            words = text.split() or [""]
            pw = len(text) * fs * 0.5 + ins + 2
            mw = max(len(w) for w in words) * fs * 0.55 + ins + 2
            for img in td.iter("img"):
                try:
                    iw = float(re.sub(r"[^\d.]", "", img.get("width") or "") or 0) * 0.75
                except ValueError:
                    iw = 0
                mw = max(mw, iw + ins)
                pw = max(pw, iw + ins)
            wcss = to_pt(tcs.get("width"), LengthContext(fs, self.cascade.root_font_size,
                                                         tw or avail, *self.cascade.viewport_pt))
            if wcss is None and td.get("width"):
                wcss = to_pt(td.get("width"), LengthContext(fs, percent_base=tw or avail),
                             allow_unitless=True)
            if cell.col_span == 1:
                pref[c] = max(pref[c], pw)
                minw[c] = max(minw[c], mw)
                if wcss:
                    fixed[c] = max(fixed[c] or 0.0, wcss + (ins if tcs.get("box-sizing") != "border-box" else 0))
        col_idx = 0
        for ch in el:
            if not isinstance(ch.tag, str):
                continue
            cols = [ch] if self.tag(ch) == "col" else [x for x in ch if isinstance(x.tag, str) and self.tag(x) == "col"] \
                if self.tag(ch) == "colgroup" else []
            for col in cols:
                span = _int(col.get("span"), 1)
                wv = col.get("width") or self.style(col).get("width")
                w = to_pt(wv, LengthContext(12.0, percent_base=tw or avail), allow_unitless=True) if wv else None
                for _ in range(span):
                    if col_idx < ncols and w:
                        fixed[col_idx] = w
                    col_idx += 1
        widths = distribute(fixed, pref, minw, tw, avail)

        # ---- build cell contents
        for r, c, cell in positions:
            td, tr = cell_lookup[id(cell)]
            tcs = self.style(td)
            trcs = self.style(tr)
            span_w = sum(widths[c:c + cell.col_span])
            ins = {s: _r(self.length(tcs, f"padding-{s}", span_w)) for s in ("top", "right", "bottom", "left")}
            fill = tcs["background-color"] or trcs["background-color"]
            if fill is None:
                p = tr.getparent()
                if p is not None and self.tag(p) in ("thead", "tbody", "tfoot"):
                    fill = self.style(p)["background-color"]
            if fill is None:
                fill = cs["background-color"]
            props: Dict[str, Any] = {
                "fill": self.color(fill) if fill else None,
                "inset_top": ins["top"], "inset_right": ins["right"],
                "inset_bottom": ins["bottom"], "inset_left": ins["left"],
                "vertical_align": {"top": "top", "bottom": "bottom", "middle": "middle",
                                   "baseline": "top", "text-top": "top",
                                   "text-bottom": "bottom"}.get(tcs["vertical-align"], "middle"),
            }
            for s in ("top", "right", "bottom", "left"):
                bw = self.border(tcs, s)
                if bw == 0 and cs["border-collapse"] == "collapse":
                    # collapsed borders may be declared on the row
                    bw = self.border(trcs, s) if s in ("top", "bottom") else 0
                    col = trcs[f"border-{s}-color"]
                else:
                    col = tcs[f"border-{s}-color"]
                props[f"edge_{s}"] = {"weight": _r(bw), "color": self.color(col) or "Black"} if bw > 0 else None
            cell.props = props
            ch = self.length(tcs, "height")
            if ch:
                rows[r].min_height = max(rows[r].min_height, _r(ch))
            content_w = max(10.0, span_w - ins["left"] - ins["right"])
            cctx = BlockCtx(width=content_w, deco=bctx.deco, in_cell=True)
            with self.sub_container(cell.blocks):
                self.walk_children(td, tcs, cctx)
                self.flush()

        tprops = {}
        for s in ("top", "right", "bottom", "left"):
            bw = self.border(cs, s)
            tprops[f"border_{s}"] = {"weight": _r(bw), "color": self.color(cs[f"border-{s}-color"]) or "Black"} if bw > 0 else None
        name = self.para_name(el)
        table = Table(style=name, rows=rows, column_widths=[_r(w, 2) for w in widths],
                      props=tprops, source=self._source(el))
        full = self.para_props(cs, bctx, el)
        full.update(self.char_props(cs, None, bctx))
        full["leading"] = "auto"
        full["first_line_indent"] = 0.0
        if (str(cs.get("margin-left")).strip() == "auto" and str(cs.get("margin-right")).strip() == "auto") \
                or (el.get("align") or "").lower() == "center":
            full["align"] = "center"
        elif (el.get("align") or "").lower() == "right":
            full["align"] = "right"
        else:
            full["align"] = "left"
        full["space_before"] = _r(self.take_gap())
        if self.pending_break:
            full["start_paragraph"] = "next_page"
            self.pending_break = False
        self.container.append(table)
        self.pending.append((table, name, full, [], self.signature(el), self.natural(mt)))
        self.last_full = full
        self.emitted += 1
        self.gap_margin(mb)

    # ------------------------------------------------------------------ styles
    @staticmethod
    def natural(margin_top: float) -> Dict[str, Any]:
        """Context-free values used when a paragraph style is first defined; the
        context-dependent value of each occurrence becomes a local override."""
        return {"space_before": _r(max(0.0, margin_top)), "space_after": 0.0,
                "numbering_start": None, "numbering_continue": True,
                "start_paragraph": "anywhere"}

    def finalize(self):
        doc = self.doc
        base = self.base_name
        overrides_cfg = self.scfg.get("overrides") or {}
        for block, name, full, run_meta, sig, natural in self.pending:
            if name not in doc.paragraph_styles:
                parent = doc.resolve_paragraph_style(base)
                style_full = dict(full)
                style_full.update(natural)
                props = {k: v for k, v in style_full.items() if parent.get(k) != v}
                # spacing/list properties that vary per occurrence stay as overrides
                doc.paragraph_styles[name] = Style(name, "paragraph", base, props, source=sig)
            resolved = doc.resolve_paragraph_style(name)
            block.overrides = {k: v for k, v in full.items() if resolved.get(k) != v}
            eff = dict(resolved)
            eff.update(block.overrides)
            for run, cname, target, explicit in run_meta:
                diff = {k: target[k] for k in CHAR_KEYS if eff.get(k) != target[k]}
                if not diff and not explicit:
                    run.char_style = None
                    run.overrides = {}
                    continue
                if cname is None:
                    run.char_style = None
                    run.overrides = diff
                    continue
                if cname not in doc.character_styles:
                    doc.character_styles[cname] = Style(cname, "character", None, diff, source=cname)
                cst = doc.character_styles[cname].props
                run.char_style = cname
                run.overrides = {k: target[k] for k in CHAR_KEYS if cst.get(k, eff.get(k)) != target[k]}
        # user overrides from config (applied to styles, so every paragraph follows)
        for sname, props in overrides_cfg.items():
            if sname in doc.paragraph_styles:
                doc.paragraph_styles[sname].props.update(props)
            else:
                self.warn(f"styles.overrides: no paragraph style named {sname!r}")
        for sname, props in (self.scfg.get("char_overrides") or {}).items():
            if sname in doc.character_styles:
                doc.character_styles[sname].props.update(props)
            else:
                self.warn(f"styles.char_overrides: no character style named {sname!r}")


def _srcset_best(srcset: str) -> Optional[str]:
    """Largest candidate of a srcset ("a.jpg 480w, b.jpg 1200w" / "a.jpg 1x, b.jpg 2x")."""
    best, best_score = None, -1.0
    for part in re.split(r",\s+(?=\S)", (srcset or "").strip()):
        bits = part.strip().split()
        if not bits:
            continue
        url, score = bits[0].rstrip(","), 1.0
        if len(bits) > 1:
            m = re.fullmatch(r"(\d*\.?\d+)([wxh])", bits[1].lower())
            if m:
                score = float(m.group(1)) * (1 if m.group(2) != "x" else 1000)
        if score > best_score:
            best, best_score = url, score
    return best


def image_candidates(el) -> List[str]:
    """Image URLs to try, best first: real lazy-load source, largest srcset entry, src."""
    src = (el.get("src") or "").strip()
    placeholder = (not src or (src.startswith("data:") and len(src) < 600)
                   or src.lower().endswith(("blank.gif", "spacer.gif", "pixel.gif", "placeholder.png")))
    lazy = [el.get(k).strip() for k in ("data-src", "data-lazy-src", "data-original", "data-lazy",
                                        "data-url") if el.get(k)]
    sets = [_srcset_best(el.get(k)) for k in ("srcset", "data-srcset") if el.get(k)]
    # <picture><source srcset=…><img></picture>  (the HTML parser may nest <img> inside <source>)
    picture = next((a for a in list(el.iterancestors())[:3]
                    if isinstance(a.tag, str) and a.tag.lower() == "picture"), None)
    if picture is not None:
        for source in picture.iter("source"):
            typ = (source.get("type") or "").lower()
            if typ and not any(t in typ for t in ("jpeg", "jpg", "png", "webp", "gif", "avif", "svg")):
                continue
            sets.append(_srcset_best(source.get("srcset") or source.get("data-srcset") or ""))
    order = (lazy + sets + ([src] if src else [])) if placeholder else (sets + [src] + lazy)
    out = []
    for u in order:
        if u and u not in out:
            out.append(u)
    return out


def _int(v, default):
    try:
        return max(1, int(str(v).strip()))
    except (TypeError, ValueError):
        return default


def distribute(fixed, pref, minw, table_w, avail) -> List[float]:
    n = len(pref)
    base = [fixed[i] if fixed[i] else max(minw[i], 12.0) for i in range(n)]
    prefw = [fixed[i] if fixed[i] else max(pref[i], minw[i], 12.0) for i in range(n)]
    if table_w:
        W = min(table_w, avail) if avail > 0 else table_w
    else:
        W = min(avail, sum(prefw))
    W = max(W, 12.0 * n)
    flex = [i for i in range(n) if not fixed[i]]
    if sum(prefw) <= W:
        extra = W - sum(prefw)
        targets = flex or list(range(n))
        tot = sum(prefw[i] for i in targets) or 1.0
        out = list(prefw)
        if table_w or sum(prefw) > W * 0.999:
            for i in targets:
                out[i] += extra * prefw[i] / tot
        return out
    if sum(base) <= W:
        out = list(base)
        room = W - sum(base)
        want = sum(prefw[i] - base[i] for i in flex) or 1.0
        for i in flex:
            out[i] = base[i] + room * (prefw[i] - base[i]) / want
        if not flex:
            scale = W / sum(base)
            out = [b * scale for b in base]
        return out
    scale = W / sum(base)
    return [b * scale for b in base]
