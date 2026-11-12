"""CSS engine: stylesheet collection, media queries, selector matching and the cascade.

Produces a *computed style* dict for every element (and ::before/::after
pseudo elements), the same way a browser does before layout.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Callable, Dict, List, Optional, Tuple

import cssselect
import tinycss2
from lxml import etree

from .cssvalues import (LengthContext, PX, expand, parse_color, split_values, to_pt)

# --------------------------------------------------------------------------- #
# User-agent stylesheet (close to Chromium's html.css)
# --------------------------------------------------------------------------- #
UA_CSS = r"""
html, address, blockquote, body, center, dialog, div, figure, figcaption, footer, form,
header, hr, legend, listing, main, p, plaintext, pre, xmp, search, article, aside, h1, h2, h3,
h4, h5, h6, hgroup, nav, section, dir, dd, dl, dt, menu, ol, ul, fieldset, details, summary,
frameset, frame, noframes, optgroup, picture > source { display: block }
li { display: list-item }
head, script, style, template, noscript, title, meta, link, base, datalist, param, iframe,
object, embed, video, audio, canvas, map, area, input, select, textarea, track, source,
[hidden], dialog:not([open]), slot { display: none }
table { display: table; border-collapse: separate; text-indent: 0 }
caption { display: table-caption; text-align: center }
thead { display: table-header-group; vertical-align: middle }
tbody { display: table-row-group; vertical-align: middle }
tfoot { display: table-footer-group; vertical-align: middle }
tr { display: table-row; vertical-align: inherit }
td, th { display: table-cell; vertical-align: inherit; padding: 1px }
th { font-weight: bold; text-align: center }
col { display: table-column } colgroup { display: table-column-group }
table[border] { border: 1px outset gray }
table[border] td, table[border] th { border: 1px inset gray }
table[border="0"] td, table[border="0"] th { border: none }
html { font-size: 16px; line-height: normal; color: black; font-family: serif }
body { margin: 8px }
h1 { font-size: 2em; margin-top: .67em; margin-bottom: .67em; font-weight: bold }
h2 { font-size: 1.5em; margin-top: .83em; margin-bottom: .83em; font-weight: bold }
h3 { font-size: 1.17em; margin-top: 1em; margin-bottom: 1em; font-weight: bold }
h4 { font-size: 1em; margin-top: 1.33em; margin-bottom: 1.33em; font-weight: bold }
h5 { font-size: .83em; margin-top: 1.67em; margin-bottom: 1.67em; font-weight: bold }
h6 { font-size: .67em; margin-top: 2.33em; margin-bottom: 2.33em; font-weight: bold }
article h1, aside h1, nav h1, section h1 { font-size: 1.5em; margin-top: .83em; margin-bottom: .83em }
p, dl, menu, dir { margin-top: 1em; margin-bottom: 1em }
blockquote, figure { margin: 1em 40px }
ul, ol { margin-top: 1em; margin-bottom: 1em; padding-left: 40px }
ul, menu, dir { list-style-type: disc }
ol { list-style-type: decimal }
ul ul, ol ul, menu ul { list-style-type: circle }
ul ul ul, ul ol ul, ol ul ul, ol ol ul { list-style-type: square }
ul ul, ul ol, ol ul, ol ol { margin-top: 0; margin-bottom: 0 }
dd { margin-left: 40px }
b, strong { font-weight: bolder }
i, em, cite, var, dfn, address { font-style: italic }
pre, code, kbd, samp, tt, xmp, plaintext, listing { font-family: monospace }
pre, xmp, plaintext, listing { white-space: pre; margin-top: 1em; margin-bottom: 1em }
u, ins { text-decoration: underline }
s, strike, del { text-decoration: line-through }
sub { vertical-align: sub; font-size: smaller }
sup { vertical-align: super; font-size: smaller }
small { font-size: smaller } big { font-size: larger }
a[href] { color: #0000EE; text-decoration: underline }
mark { background-color: yellow; color: black }
hr { border-top: 1px inset gray; margin-top: .5em; margin-bottom: .5em }
center { text-align: center }
nobr { white-space: nowrap }
abbr[title] { text-decoration: underline }
q::before { content: open-quote } q::after { content: close-quote }
button { display: inline-block }
"""

INHERITED = {
    "color", "font-family", "font-size", "font-style", "font-weight", "font-variant",
    "line-height", "text-align", "text-indent", "text-transform", "letter-spacing",
    "word-spacing", "white-space", "list-style-type", "list-style-position", "visibility",
    "hyphens", "border-collapse", "direction", "orphans", "widows",
}

INITIAL = {
    "display": "inline", "color": "black", "font-family": "serif", "font-size": "medium",
    "font-style": "normal", "font-weight": "normal", "font-variant": "normal",
    "line-height": "normal", "text-align": "start", "text-indent": "0",
    "text-transform": "none", "letter-spacing": "normal", "word-spacing": "normal",
    "white-space": "normal", "list-style-type": "disc", "list-style-position": "outside",
    "visibility": "visible", "hyphens": "manual", "border-collapse": "separate",
    "direction": "ltr", "orphans": "2", "widows": "2",
    "text-decoration-line": "none", "text-decoration-color": "currentcolor",
    "vertical-align": "baseline", "background-color": "transparent", "background-image": "none",
    "width": "auto", "height": "auto", "max-width": "none", "min-height": "0",
    "break-before": "auto", "break-after": "auto", "break-inside": "auto",
    "float": "none", "position": "static", "column-count": "auto", "content": "normal",
    "font-variant-caps": "normal", "text-align-last": "auto", "list-style-image": "none",
    "overflow-wrap": "normal",
}
for _side in ("top", "right", "bottom", "left"):
    INITIAL[f"margin-{_side}"] = "0"
    INITIAL[f"padding-{_side}"] = "0"
    INITIAL[f"border-{_side}-width"] = "medium"
    INITIAL[f"border-{_side}-style"] = "none"
    INITIAL[f"border-{_side}-color"] = "currentcolor"

KNOWN_PROPS = set(INITIAL)

FONT_SIZE_KEYWORDS = {
    "xx-small": 9, "x-small": 10, "small": 13, "medium": 16, "large": 18,
    "x-large": 24, "xx-large": 32, "xxx-large": 48,
}


# --------------------------------------------------------------------------- #
# Media queries
# --------------------------------------------------------------------------- #
def media_matches(query: str, media_type: str, viewport_px: float) -> bool:
    q_all = (query or "").strip().lower()
    if not q_all:
        return True
    for q in split_values(q_all, ","):
        q = q.strip()
        neg = False
        if q.startswith("not "):
            neg, q = True, q[4:]
        if q.startswith("only "):
            q = q[5:]
        ok = True
        for part in re.split(r"\s+and\s+", q):
            part = part.strip()
            if not part:
                continue
            if part.startswith("("):
                ok = ok and _media_feature(part.strip("() "), viewport_px)
            else:
                ok = ok and part in ("all", media_type)
        if neg:
            ok = not ok
        if ok:
            return True
    return False


def _px(val: str) -> Optional[float]:
    val = val.strip()
    m = re.fullmatch(r"(-?\d*\.?\d+)(px|em|rem|pt|in|cm|mm)?", val)
    if not m:
        return None
    n, u = float(m.group(1)), m.group(2) or "px"
    return n * {"px": 1, "em": 16, "rem": 16, "pt": 4 / 3, "in": 96, "cm": 96 / 2.54,
                "mm": 96 / 25.4}[u]


def _media_feature(feat: str, vw: float) -> bool:
    m = re.fullmatch(r"(width|min-width|max-width)\s*:\s*(.+)", feat)
    if m:
        px = _px(m.group(2))
        if px is None:
            return False
        return {"width": vw == px, "min-width": vw >= px, "max-width": vw <= px}[m.group(1)]
    m = re.fullmatch(r"width\s*(<=|>=|<|>)\s*(.+)", feat)
    if m:
        px = _px(m.group(2))
        if px is None:
            return False
        return {"<=": vw <= px, ">=": vw >= px, "<": vw < px, ">": vw > px}[m.group(1)]
    m = re.fullmatch(r"(.+?)\s*(<=|<)\s*width\s*(<=|<)\s*(.+)", feat)
    if m:
        lo, hi = _px(m.group(1)), _px(m.group(4))
        if lo is None or hi is None:
            return False
        return (vw >= lo if m.group(2) == "<=" else vw > lo) and (vw <= hi if m.group(3) == "<=" else vw < hi)
    simple = {
        "orientation: landscape": True, "orientation: portrait": False,
        "prefers-color-scheme: light": True, "prefers-color-scheme: dark": False,
        "hover: hover": True, "hover": True, "pointer: fine": True, "color": True,
        "any-hover: hover": True, "prefers-reduced-motion: no-preference": True,
        "min-resolution: 1dppx": True,
    }
    return simple.get(re.sub(r"\s+", " ", feat.replace(" :", ":")), False)


# --------------------------------------------------------------------------- #
# Stylesheet collection
# --------------------------------------------------------------------------- #
class Rule:
    __slots__ = ("selector", "xpath", "specificity", "order", "decls", "pseudo", "origin")

    def __init__(self, selector, xpath, specificity, order, decls, pseudo, origin):
        self.selector = selector
        self.xpath = xpath
        self.specificity = specificity
        self.order = order
        self.decls = decls
        self.pseudo = pseudo
        self.origin = origin


def parse_declarations(text_or_tokens) -> List[Tuple[str, str, bool]]:
    if isinstance(text_or_tokens, str):
        items = tinycss2.parse_declaration_list(text_or_tokens, skip_comments=True,
                                                skip_whitespace=True)
    else:
        items = tinycss2.parse_declaration_list(text_or_tokens, skip_comments=True,
                                                skip_whitespace=True)
    out = []
    for d in items:
        if d.type != "declaration":
            continue
        name = d.lower_name if not d.name.startswith("--") else d.name
        out.append((name, tinycss2.serialize(d.value).strip(), bool(d.important)))
    return out


class StyleCollector:
    """Collects rules from <style>, <link rel=stylesheet>, @import and inline sources."""

    def __init__(self, media_type: str, viewport_px: float,
                 fetch_text: Callable[[str, str], Tuple[Optional[str], str]],
                 warn: Callable[[str], None]):
        self.media_type = media_type
        self.viewport_px = viewport_px
        self.fetch_text = fetch_text      # (href, base) -> (css_text|None, new_base)
        self.warn = warn
        self.rules: List[Rule] = []
        self.page_decls: List[Tuple[str, str, bool]] = []
        self.font_faces: List[Tuple[Dict[str, str], str]] = []   # (descriptors, base)
        self.order = 0
        self._translator = cssselect.HTMLTranslator()
        self._import_depth = 0

    def add_css(self, css: str, base: str, origin: int = 1):
        rules = tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True)
        self._add_rules(rules, base, origin)

    def _add_rules(self, rules, base, origin):
        for rule in rules:
            if rule.type == "qualified-rule":
                self._qualified(rule, origin)
            elif rule.type == "at-rule":
                kw = rule.lower_at_keyword
                prelude = tinycss2.serialize(rule.prelude).strip()
                if kw == "media":
                    if rule.content is not None and media_matches(prelude, self.media_type,
                                                                  self.viewport_px):
                        inner = tinycss2.parse_rule_list(rule.content, skip_comments=True,
                                                         skip_whitespace=True)
                        self._add_rules(inner, base, origin)
                elif kw in ("supports", "layer", "container", "document", "scope"):
                    if rule.content is not None:
                        inner = tinycss2.parse_rule_list(rule.content, skip_comments=True,
                                                         skip_whitespace=True)
                        self._add_rules(inner, base, origin)
                elif kw == "import":
                    self._import(prelude, base, origin)
                elif kw == "page":
                    if rule.content is not None:
                        self.page_decls.extend(parse_declarations(rule.content))
                elif kw == "font-face":
                    if rule.content is not None:
                        desc = {n: v for n, v, _ in parse_declarations(rule.content)}
                        self.font_faces.append((desc, base))

    def _import(self, prelude: str, base: str, origin: int):
        if self._import_depth > 8:
            return
        toks = split_values(prelude)
        if not toks:
            return
        from .cssvalues import css_url
        href = css_url(toks[0])
        media = " ".join(toks[1:])
        if not href or (media and not media_matches(media, self.media_type, self.viewport_px)):
            return
        text, new_base = self.fetch_text(href, base)
        if text is None:
            self.warn(f"@import not loaded: {href}")
            return
        self._import_depth += 1
        try:
            self.add_css(text, new_base, origin)
        finally:
            self._import_depth -= 1

    def _qualified(self, rule, origin):
        sel_text = tinycss2.serialize(rule.prelude).strip()
        decls = parse_declarations(rule.content)
        if not decls:
            return
        self.order += 1
        try:
            selectors = cssselect.parse(sel_text)
        except Exception:
            # Try one selector at a time so one bad selector does not kill the list
            selectors = []
            for part in split_values(sel_text, ","):
                try:
                    selectors.extend(cssselect.parse(part))
                except Exception:
                    pass
        for sel in selectors:
            pseudo = sel.pseudo_element
            if pseudo is not None and not isinstance(pseudo, str):
                continue
            if pseudo not in (None, "before", "after"):
                continue
            try:
                xp = self._translator.selector_to_xpath(sel, prefix="descendant-or-self::")
            except Exception:
                continue
            a, b, c = sel.specificity()
            try:
                canon = sel.canonical()
            except Exception:
                canon = sel_text
            self.rules.append(Rule(canon, xp, (0, a, b, c), self.order, decls, pseudo, origin))


# --------------------------------------------------------------------------- #
# Cascade
# --------------------------------------------------------------------------- #
_VAR_RE = re.compile(r"var\(\s*(--[\w-]+)\s*(?:,\s*([^()]*(?:\([^()]*\)[^()]*)*))?\)")


def _substitute_vars(value: str, custom: Dict[str, str], depth: int = 0) -> Optional[str]:
    if "var(" not in value:
        return value
    if depth > 10:
        return None
    failed = False

    def repl(m):
        nonlocal failed
        name, fallback = m.group(1), m.group(2)
        if name in custom:
            return custom[name]
        if fallback is not None:
            return fallback.strip()
        failed = True
        return ""

    new = _VAR_RE.sub(repl, value)
    if failed:
        return None
    return _substitute_vars(new, custom, depth + 1) if "var(" in new else new


class Cascade:
    def __init__(self, root, collector: StyleCollector, viewport_pt: Tuple[float, float],
                 warn: Callable[[str], None]):
        self.root = root
        self.warn = warn
        self.viewport_pt = viewport_pt
        self.styles: Dict[object, dict] = {}
        self.pseudo_styles: Dict[Tuple[object, str], Optional[dict]] = {}
        self._matches: Dict[object, list] = defaultdict(list)
        self._pseudo_matches: Dict[Tuple[object, str], list] = defaultdict(list)
        self._match_rules(collector.rules)
        self._match_inline()
        self.root_font_size = 12.0
        self._compute_tree()

    # ---------------- matching ----------------
    def _match_rules(self, rules: List[Rule]):
        classes, ids = set(), set()
        for el in self.root.iter():
            if not isinstance(el.tag, str):
                continue
            c = el.get("class")
            if c:
                classes.update(c.split())
            i = el.get("id")
            if i:
                ids.add(i)
        cache: Dict[str, list] = {}
        tok_re = re.compile(r"([.#])(-?[_a-zA-Z -￿][_a-zA-Z0-9 -￿-]*)")
        for rule in rules:
            sel = rule.selector
            if ":not(" not in sel and ":is(" not in sel and ":where(" not in sel:
                skip = False
                # Only look at the selector part belonging to this rule's own list item
                for kind, name in tok_re.findall(re.sub(r"\[[^\]]*\]", "", sel)):
                    if (kind == "." and name not in classes) or (kind == "#" and name not in ids):
                        skip = True
                        break
                if skip:
                    continue
            found = cache.get(rule.xpath)
            if found is None:
                try:
                    found = etree.XPath(rule.xpath)(self.root)
                except Exception:
                    found = []
                cache[rule.xpath] = found
            key = (rule.origin, rule.specificity, rule.order)
            for el in found:
                if rule.pseudo:
                    self._pseudo_matches[(el, rule.pseudo)].append((key, rule.decls))
                else:
                    self._matches[el].append((key, rule.decls))

    def _match_inline(self):
        for el in self.root.iter():
            if not isinstance(el.tag, str):
                continue
            st = el.get("style")
            if st:
                try:
                    decls = parse_declarations(st)
                except Exception:
                    continue
                self._matches[el].append(((1, (1, 0, 0, 0), 10 ** 9), decls))

    # ---------------- computing ----------------
    def _compute_tree(self):
        html = self.root
        self.styles[html] = self._compute(html, None, self._matches.get(html, []))
        self.root_font_size = self.styles[html]["font-size"]
        # recompute html once root font size is known (rem on html itself)
        self.styles[html] = self._compute(html, None, self._matches.get(html, []))
        stack = [html]
        while stack:
            el = stack.pop()
            cs = self.styles[el]
            for child in el:
                if not isinstance(child.tag, str):
                    continue
                self.styles[child] = self._compute(child, cs, self._matches.get(child, []))
                stack.append(child)

    def style(self, el) -> dict:
        cs = self.styles.get(el)
        if cs is None:
            parent = el.getparent()
            pcs = self.style(parent) if parent is not None else None
            cs = self._compute(el, pcs, self._matches.get(el, []))
            self.styles[el] = cs
        return cs

    def pseudo(self, el, which: str) -> Optional[dict]:
        key = (el, which)
        if key in self.pseudo_styles:
            return self.pseudo_styles[key]
        matched = self._pseudo_matches.get(key)
        res = None
        if matched:
            cs = self._compute(el, self.style(el), matched, pseudo=True)
            if cs.get("content") not in (None, "normal", "none", "") and cs["display"] != "none":
                res = cs
        self.pseudo_styles[key] = res
        return res

    def _compute(self, el, parent: Optional[dict], matched, pseudo: bool = False) -> dict:
        ordered = []
        for key, decls in matched:
            for name, value, important in decls:
                ordered.append(((1 if important else 0,) + key, name, value))
        ordered.sort(key=lambda t: t[0])

        custom = dict(parent["--vars"]) if parent else {}
        for _, name, value in ordered:
            if name.startswith("--"):
                custom[name] = value
        # resolve custom properties referencing other custom properties
        for k, v in list(custom.items()):
            if "var(" in v:
                sv = _substitute_vars(v, custom)
                custom[k] = sv if sv is not None else ""

        specified: Dict[str, str] = {}
        for _, name, value in ordered:
            if name.startswith("--"):
                continue
            if "var(" in value:
                value = _substitute_vars(value, custom)
                if value is None:
                    continue
            for lname, lval in expand(name, value):
                specified[lname] = lval

        cs: dict = {"--vars": custom}
        pfs = parent["font-size"] if parent else 12.0
        vw, vh = self.viewport_pt

        # ---- font-size first
        fs_val = specified.get("font-size", "inherit" if parent else "medium")
        cs["font-size"] = self._font_size(fs_val, pfs, vw, vh)
        fs = cs["font-size"]
        lctx = LengthContext(fs, self.root_font_size, None, vw, vh)

        for prop in KNOWN_PROPS:
            if prop == "font-size":
                continue
            val = specified.get(prop)
            inherited = prop in INHERITED
            if val is not None:
                vl = val.strip().lower()
                if vl == "inherit" or (vl in ("unset", "revert") and inherited):
                    val = None
                    inherited = True
                elif vl in ("initial", "unset", "revert"):
                    val = INITIAL[prop]
                    inherited = False
            if val is None:
                if inherited and parent is not None:
                    pv = parent.get(prop)
                    if prop == "line-height" and isinstance(pv, tuple) and pv[0] == "factor":
                        cs[prop] = pv
                        continue
                    cs[prop] = pv
                    continue
                val = INITIAL[prop]
            cs[prop] = self._value(prop, val, cs, parent, lctx)

        # border colours default to currentcolor
        for side in ("top", "right", "bottom", "left"):
            key = f"border-{side}-color"
            if cs[key] == "currentcolor":
                cs[key] = cs["color"]
        if cs["text-decoration-color"] == "currentcolor":
            cs["text-decoration-color"] = cs["color"]
        if pseudo:
            cs["display"] = cs["display"] if cs["display"] != "inline" else "inline"
        return cs

    def _font_size(self, val, pfs, vw, vh) -> float:
        v = str(val).strip().lower()
        if v == "inherit" or v == "unset":
            return pfs
        if v == "initial":
            return 12.0
        if v in FONT_SIZE_KEYWORDS:
            return FONT_SIZE_KEYWORDS[v] * PX
        if v == "smaller":
            return pfs / 1.2
        if v == "larger":
            return pfs * 1.2
        ctx = LengthContext(pfs, self.root_font_size, pfs, vw, vh)
        if v.endswith("em") and not v.endswith("rem"):
            ctx.font_size = pfs
        r = to_pt(v, ctx)
        return r if r is not None and r > 0 else pfs

    def _value(self, prop, val, cs, parent, lctx):
        v = val.strip()
        vl = v.lower()
        if prop == "color":
            if vl == "currentcolor":
                return parent["color"] if parent else (0, 0, 0, 1.0)
            c = parse_color(vl)
            if c is None:
                return parent["color"] if parent else (0, 0, 0, 1.0)
            return c
        if prop in ("background-color",):
            c = parse_color(vl)
            return None if (c is None or c[3] == 0) else c
        if prop.endswith("-color"):
            if vl == "currentcolor":
                return "currentcolor"
            c = parse_color(vl)
            return c if c is not None else "currentcolor"
        if prop == "font-weight":
            pw = parent["font-weight"] if parent else 400
            if vl == "normal":
                return 400
            if vl == "bold":
                return 700
            if vl == "bolder":
                return 400 if pw < 350 else (700 if pw < 550 else 900)
            if vl == "lighter":
                return 100 if pw < 550 else (400 if pw < 750 else 700)
            try:
                return int(float(vl))
            except ValueError:
                return pw
        if prop == "line-height":
            if vl == "normal":
                return ("normal", 0)
            try:
                return ("factor", float(vl))
            except ValueError:
                pass
            ctx = LengthContext(lctx.font_size, lctx.root_font_size, lctx.font_size,
                                lctx.viewport_w, lctx.viewport_h)
            r = to_pt(vl, ctx)
            return ("pt", r) if r is not None else ("normal", 0)
        if prop in ("text-indent", "letter-spacing", "word-spacing"):
            if vl == "normal":
                return 0.0
            if vl.endswith("%"):
                return vl          # resolved against width later
            r = to_pt(vl, lctx)
            return r if r is not None else 0.0
        if prop == "font-family":
            return v
        if prop == "content":
            return v
        if prop == "text-decoration-line":
            return frozenset(t for t in vl.split() if t != "none")
        if prop in ("display", "text-align", "white-space", "font-style", "font-variant",
                    "text-transform", "vertical-align", "list-style-type", "float", "position",
                    "break-before", "break-after", "break-inside", "visibility", "hyphens",
                    "font-variant-caps", "border-collapse", "list-style-position", "direction"):
            if prop == "display" and vl.startswith("inline-"):
                return vl
            return vl.split()[0] if vl else vl
        return v
