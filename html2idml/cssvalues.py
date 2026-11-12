"""CSS value helpers: colours, lengths, tokenising and shorthand expansion."""
from __future__ import annotations

import colorsys
import re
from functools import lru_cache
from typing import List, Optional, Tuple

PX = 0.75  # 1 CSS px = 0.75 pt (96 dpi)

ABS_UNITS = {
    "pt": 1.0, "px": PX, "in": 72.0, "cm": 72 / 2.54, "mm": 72 / 25.4,
    "q": 72 / 101.6, "pc": 12.0,
}

NAMED_COLORS = dict(
    (k, v) for k, v in (item.split(":") for item in """
aliceblue:f0f8ff antiquewhite:faebd7 aqua:00ffff aquamarine:7fffd4 azure:f0ffff beige:f5f5dc
bisque:ffe4c4 black:000000 blanchedalmond:ffebcd blue:0000ff blueviolet:8a2be2 brown:a52a2a
burlywood:deb887 cadetblue:5f9ea0 chartreuse:7fff00 chocolate:d2691e coral:ff7f50
cornflowerblue:6495ed cornsilk:fff8dc crimson:dc143c cyan:00ffff darkblue:00008b darkcyan:008b8b
darkgoldenrod:b8860b darkgray:a9a9a9 darkgreen:006400 darkgrey:a9a9a9 darkkhaki:bdb76b
darkmagenta:8b008b darkolivegreen:556b2f darkorange:ff8c00 darkorchid:9932cc darkred:8b0000
darksalmon:e9967a darkseagreen:8fbc8f darkslateblue:483d8b darkslategray:2f4f4f
darkslategrey:2f4f4f darkturquoise:00ced1 darkviolet:9400d3 deeppink:ff1493 deepskyblue:00bfff
dimgray:696969 dimgrey:696969 dodgerblue:1e90ff firebrick:b22222 floralwhite:fffaf0
forestgreen:228b22 fuchsia:ff00ff gainsboro:dcdcdc ghostwhite:f8f8ff gold:ffd700
goldenrod:daa520 gray:808080 green:008000 greenyellow:adff2f grey:808080 honeydew:f0fff0
hotpink:ff69b4 indianred:cd5c5c indigo:4b0082 ivory:fffff0 khaki:f0e68c lavender:e6e6fa
lavenderblush:fff0f5 lawngreen:7cfc00 lemonchiffon:fffacd lightblue:add8e6 lightcoral:f08080
lightcyan:e0ffff lightgoldenrodyellow:fafad2 lightgray:d3d3d3 lightgreen:90ee90 lightgrey:d3d3d3
lightpink:ffb6c1 lightsalmon:ffa07a lightseagreen:20b2aa lightskyblue:87cefa
lightslategray:778899 lightslategrey:778899 lightsteelblue:b0c4de lightyellow:ffffe0 lime:00ff00
limegreen:32cd32 linen:faf0e6 magenta:ff00ff maroon:800000 mediumaquamarine:66cdaa
mediumblue:0000cd mediumorchid:ba55d3 mediumpurple:9370db mediumseagreen:3cb371
mediumslateblue:7b68ee mediumspringgreen:00fa9a mediumturquoise:48d1cc mediumvioletred:c71585
midnightblue:191970 mintcream:f5fffa mistyrose:ffe4e1 moccasin:ffe4b5 navajowhite:ffdead
navy:000080 oldlace:fdf5e6 olive:808000 olivedrab:6b8e23 orange:ffa500 orangered:ff4500
orchid:da70d6 palegoldenrod:eee8aa palegreen:98fb98 paleturquoise:afeeee palevioletred:db7093
papayawhip:ffefd5 peachpuff:ffdab9 peru:cd853f pink:ffc0cb plum:dda0dd powderblue:b0e0e6
purple:800080 rebeccapurple:663399 red:ff0000 rosybrown:bc8f8f royalblue:4169e1
saddlebrown:8b4513 salmon:fa8072 sandybrown:f4a460 seagreen:2e8b57 seashell:fff5ee
sienna:a0522d silver:c0c0c0 skyblue:87ceeb slateblue:6a5acd slategray:708090 slategrey:708090
snow:fffafa springgreen:00ff7f steelblue:4682b4 tan:d2b48c teal:008080 thistle:d8bfd8
tomato:ff6347 turquoise:40e0d0 violet:ee82ee wheat:f5deb3 white:ffffff whitesmoke:f5f5f5
yellow:ffff00 yellowgreen:9acd32
""".split())
)

RGBA = Tuple[float, float, float, float]


def split_values(value: str, sep: Optional[str] = None) -> List[str]:
    """Split a CSS value on whitespace (or `sep`) outside of parentheses/quotes."""
    out, buf, depth, quote = [], [], 0, None
    for ch in value:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        is_sep = (ch.isspace() if sep is None else ch == sep)
        if is_sep and depth == 0:
            if buf:
                out.append("".join(buf).strip())
                buf = []
            continue
        buf.append(ch)
    if buf and "".join(buf).strip():
        out.append("".join(buf).strip())
    return out


def _num(s: str) -> float:
    s = s.strip()
    if s.endswith("%"):
        return float(s[:-1]) / 100.0
    return float(s)


@lru_cache(maxsize=4096)
def parse_color(value: str) -> Optional[RGBA]:
    """Return (r, g, b, a) with r,g,b in 0..255 and a in 0..1, or None."""
    if not value:
        return None
    v = value.strip().lower()
    if v in ("transparent", "none", "initial", "inherit", "unset"):
        return (0, 0, 0, 0.0) if v == "transparent" else None
    if v in NAMED_COLORS:
        h = NAMED_COLORS[v]
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
    if v.startswith("#"):
        h = v[1:]
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        if len(h) in (6, 8) and re.fullmatch(r"[0-9a-f]+", h):
            a = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a)
        return None
    m = re.fullmatch(r"(rgba?|hsla?)\((.*)\)", v)
    if not m:
        return None
    fn, args = m.group(1), m.group(2).replace("/", " ").replace(",", " ")
    parts = args.split()
    if len(parts) < 3:
        return None
    try:
        alpha = _num(parts[3]) if len(parts) > 3 else 1.0
        if fn.startswith("rgb"):
            rgb = []
            for p in parts[:3]:
                rgb.append(_num(p) * 255 if p.endswith("%") else float(p))
            return (rgb[0], rgb[1], rgb[2], alpha)
        hue = float(parts[0].replace("deg", ""))
        sat, light = _num(parts[1]), _num(parts[2])
        r, g, b = colorsys.hls_to_rgb((hue % 360) / 360, light, sat)
        return (r * 255, g * 255, b * 255, alpha)
    except ValueError:
        return None


def blend_on_white(c: RGBA) -> Tuple[float, float, float]:
    """Flatten an RGBA colour onto white paper (how it would print)."""
    r, g, b, a = c
    return (r * a + 255 * (1 - a), g * a + 255 * (1 - a), b * a + 255 * (1 - a))


_LEN_RE = re.compile(r"^(-?\d*\.?\d+(?:e-?\d+)?)([a-z%]*)$")
_CALC_TOKEN = re.compile(r"(-?\d*\.?\d+)(px|pt|em|rem|%|in|cm|mm|pc|q|vw|vh|vmin|vmax|ex|ch)?")


class LengthContext:
    __slots__ = ("font_size", "root_font_size", "percent_base", "viewport_w", "viewport_h")

    def __init__(self, font_size=12.0, root_font_size=12.0, percent_base=None,
                 viewport_w=960.0, viewport_h=540.0):
        self.font_size = font_size
        self.root_font_size = root_font_size
        self.percent_base = percent_base
        self.viewport_w = viewport_w     # in pt
        self.viewport_h = viewport_h


def to_pt(value, ctx: LengthContext, allow_unitless: bool = False) -> Optional[float]:
    """Convert a CSS length to points. Returns None for auto/unknown."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    v = str(value).strip().lower()
    if not v or v in ("auto", "none", "normal", "initial", "inherit", "unset", "fit-content",
                      "max-content", "min-content"):
        return None
    if v.startswith(("calc(", "min(", "max(", "clamp(")):
        return _calc(v, ctx)
    if v in ("thin", "medium", "thick"):
        return {"thin": 1 * PX, "medium": 3 * PX, "thick": 5 * PX}[v]
    m = _LEN_RE.match(v)
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2)
    if unit == "":
        return n * PX if (allow_unitless or n == 0) else (0.0 if n == 0 else None)
    if unit in ABS_UNITS:
        return n * ABS_UNITS[unit]
    if unit == "em":
        return n * ctx.font_size
    if unit == "rem":
        return n * ctx.root_font_size
    if unit in ("ex", "ch"):
        return n * ctx.font_size * 0.5
    if unit == "%":
        return None if ctx.percent_base is None else n / 100.0 * ctx.percent_base
    if unit == "vw":
        return n / 100.0 * ctx.viewport_w
    if unit == "vh":
        return n / 100.0 * ctx.viewport_h
    if unit == "vmin":
        return n / 100.0 * min(ctx.viewport_w, ctx.viewport_h)
    if unit == "vmax":
        return n / 100.0 * max(ctx.viewport_w, ctx.viewport_h)
    return None


def _calc(v: str, ctx: LengthContext) -> Optional[float]:
    fn = v.split("(", 1)[0]
    inner = v[len(fn) + 1:-1] if v.endswith(")") else v[len(fn) + 1:]
    if fn in ("min", "max", "clamp"):
        vals = [to_pt(p.strip(), ctx) for p in split_values(inner, ",")]
        vals = [x for x in vals if x is not None]
        if not vals:
            return None
        if fn == "min":
            return min(vals)
        if fn == "max":
            return max(vals)
        if len(vals) == 3:
            return max(vals[0], min(vals[1], vals[2]))
        return vals[0]
    inner = re.sub(r"\b(calc|min|max|clamp)\(", "(", inner)

    def repl(m):
        n, unit = m.group(1), m.group(2)
        if unit is None:
            return n
        val = to_pt(n + unit, ctx)
        return "0" if val is None else repr(val)

    expr = _CALC_TOKEN.sub(repl, inner)
    if not re.fullmatch(r"[0-9eE.+\-*/() ]+", expr):
        return None
    try:
        return float(eval(expr, {"__builtins__": {}}, {}))  # noqa: S307 - sanitised above
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Shorthand expansion
# --------------------------------------------------------------------------- #
SIDES = ("top", "right", "bottom", "left")
BORDER_STYLES = {"none", "hidden", "dotted", "dashed", "solid", "double", "groove", "ridge",
                 "inset", "outset"}
FONT_STYLES = {"italic", "oblique"}
FONT_WEIGHTS = {"bold", "bolder", "lighter", "100", "200", "300", "400", "500", "600", "700",
                "800", "900"}
SIZE_KEYWORDS = {"xx-small", "x-small", "small", "medium", "large", "x-large", "xx-large",
                 "xxx-large", "smaller", "larger"}


def _four(values):
    if len(values) == 1:
        return values * 4
    if len(values) == 2:
        return [values[0], values[1], values[0], values[1]]
    if len(values) == 3:
        return [values[0], values[1], values[2], values[1]]
    return values[:4]


def _is_length_like(t: str) -> bool:
    t = t.lower()
    return bool(_LEN_RE.match(t)) or t in ("thin", "medium", "thick") or t.startswith("calc(")


def _border_parts(value: str):
    width = style = color = None
    for tok in split_values(value):
        tl = tok.lower()
        if tl in BORDER_STYLES:
            style = tl
        elif _is_length_like(tl):
            width = tl
        elif parse_color(tl) is not None or tl == "currentcolor":
            color = tl
    return width, style, color


@lru_cache(maxsize=8192)
def expand(name: str, value: str) -> Tuple[Tuple[str, str], ...]:
    """Expand a declaration into longhand (name, value) pairs."""
    v = value.strip()
    vl = v.lower()
    if vl in ("inherit", "initial", "unset", "revert"):
        if name in ("margin", "padding"):
            return tuple((f"{name}-{s}", vl) for s in SIDES)
        if name == "border":
            return tuple((f"border-{s}-{p}", vl) for s in SIDES for p in ("width", "style", "color"))
    if name in ("margin", "padding"):
        vals = _four(split_values(v))
        return tuple((f"{name}-{s}", vals[i]) for i, s in enumerate(SIDES))
    if name in ("margin-block", "padding-block"):
        base = name.split("-")[0]
        vals = split_values(v) or ["0"]
        return ((f"{base}-top", vals[0]), (f"{base}-bottom", vals[-1]))
    if name in ("margin-inline", "padding-inline"):
        base = name.split("-")[0]
        vals = split_values(v) or ["0"]
        return ((f"{base}-left", vals[0]), (f"{base}-right", vals[-1]))
    logical = {"margin-block-start": "margin-top", "margin-block-end": "margin-bottom",
               "margin-inline-start": "margin-left", "margin-inline-end": "margin-right",
               "padding-block-start": "padding-top", "padding-block-end": "padding-bottom",
               "padding-inline-start": "padding-left", "padding-inline-end": "padding-right"}
    if name in logical:
        return ((logical[name], v),)
    if name in ("border-width", "border-style", "border-color"):
        prop = name.split("-")[1]
        vals = _four(split_values(v))
        return tuple((f"border-{s}-{prop}", vals[i]) for i, s in enumerate(SIDES))
    if name == "border" or (name.startswith("border-") and name[7:] in SIDES):
        sides = SIDES if name == "border" else (name[7:],)
        w, s, c = _border_parts(v)
        out = []
        for side in sides:
            out.append((f"border-{side}-width", w or "medium"))
            out.append((f"border-{side}-style", s or "none"))
            out.append((f"border-{side}-color", c or "currentcolor"))
        return tuple(out)
    if name == "font":
        return _expand_font(v)
    if name == "background":
        color = "transparent"
        image = "none"
        for tok in split_values(v):
            if tok.lower().startswith("url("):
                image = tok
            elif parse_color(tok) is not None:
                color = tok
        return (("background-color", color), ("background-image", image))
    if name == "list-style":
        out = []
        for tok in split_values(v):
            tl = tok.lower()
            if tl in ("inside", "outside"):
                out.append(("list-style-position", tl))
            elif tl.startswith("url("):
                out.append(("list-style-image", tok))
            else:
                out.append(("list-style-type", tok))
        return tuple(out)
    if name == "text-decoration":
        lines, color = [], None
        for tok in split_values(v):
            tl = tok.lower()
            if tl in ("underline", "line-through", "overline", "none"):
                lines.append(tl)
            elif parse_color(tl) is not None:
                color = tl
        out = [("text-decoration-line", " ".join(lines) or "none")]
        if color:
            out.append(("text-decoration-color", color))
        return tuple(out)
    if name in ("page-break-before", "page-break-after"):
        mapped = {"always": "page", "left": "page", "right": "page", "avoid": "avoid"}.get(vl, "auto")
        return (("break-" + name.split("-")[-1], mapped),)
    if name == "page-break-inside":
        return (("break-inside", vl),)
    if name == "columns":
        out = []
        for tok in split_values(v):
            if tok.isdigit():
                out.append(("column-count", tok))
        return tuple(out)
    if name == "gap" or name == "grid-gap":
        return ()
    if name == "inset":
        return ()
    if name == "flex" or name == "flex-flow" or name == "grid" or name == "transition" \
            or name == "animation":
        return ()
    if name == "word-wrap":
        return (("overflow-wrap", v),)
    return ((name, v),)


def _expand_font(v: str):
    tokens = split_values(v)
    style = "normal"
    variant = "normal"
    weight = "normal"
    size = None
    line_height = None
    family_tokens = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        tl = t.lower()
        if size is None:
            if tl in FONT_STYLES:
                style = tl
            elif tl == "small-caps":
                variant = tl
            elif tl in FONT_WEIGHTS:
                weight = tl
            elif tl == "normal":
                pass
            elif "/" in t or _is_length_like(t) or tl in SIZE_KEYWORDS:
                if "/" in t:
                    a, b = t.split("/", 1)
                    size = a
                    if b:
                        line_height = b
                    elif i + 1 < len(tokens):
                        i += 1
                        line_height = tokens[i]
                else:
                    size = t
                    if i + 1 < len(tokens) and tokens[i + 1].startswith("/"):
                        i += 1
                        lh = tokens[i][1:]
                        if not lh and i + 1 < len(tokens):
                            i += 1
                            lh = tokens[i]
                        line_height = lh
            else:
                # system font keyword (caption, menu, ...) -> ignore
                return ()
        else:
            family_tokens.append(t)
        i += 1
    if size is None:
        return ()
    out = [("font-style", style), ("font-variant", variant), ("font-weight", weight),
           ("font-size", size), ("line-height", line_height or "normal")]
    if family_tokens:
        out.append(("font-family", " ".join(family_tokens)))
    return tuple(out)


def parse_font_family(value: str) -> List[str]:
    fams = []
    for part in split_values(value, ","):
        p = part.strip().strip("'\"").strip()
        if p:
            fams.append(p)
    return fams


def css_url(token: str) -> Optional[str]:
    t = token.strip()
    m = re.match(r"url\(\s*(['\"]?)(.*?)\1\s*\)", t, re.S)
    if m:
        return m.group(2).strip()
    if t[:1] in "\"'" and t[-1:] == t[:1]:
        return t[1:-1]
    return None
