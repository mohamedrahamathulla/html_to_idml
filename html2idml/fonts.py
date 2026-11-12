"""Mapping CSS font stacks to InDesign font family + style names."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

from .cssvalues import parse_font_family

GENERIC_FAMILIES = {
    "serif": "Times New Roman",
    "sans-serif": "Arial",
    "monospace": "Courier New",
    "cursive": "Comic Sans MS",
    "fantasy": "Impact",
    "system-ui": "Segoe UI",
    "-apple-system": "Segoe UI",
    "blinkmacsystemfont": "Segoe UI",
    "ui-sans-serif": "Segoe UI",
    "ui-serif": "Times New Roman",
    "ui-monospace": "Consolas",
    "ui-rounded": "Segoe UI",
    "math": "Cambria Math",
    "emoji": "Segoe UI Emoji",
    "fangsong": "SimSun",
}

WEIGHT_NAMES = {100: "Thin", 200: "ExtraLight", 300: "Light", 400: "Regular", 500: "Medium",
                600: "SemiBold", 700: "Bold", 800: "ExtraBold", 900: "Black"}

_STYLE_WORDS = r"(Thin|Hairline|Extra ?Light|Ultra ?Light|Light|Semi ?Light|Book|Regular|Normal|Medium|" \
               r"Semi ?Bold|Demi ?Bold|Bold|Extra ?Bold|Ultra ?Bold|Heavy|Black|Italic|Oblique|" \
               r"Condensed|Narrow|Cond|Light Italic|Bold Italic)"


_FILE_CACHE: Optional[Dict[str, List[str]]] = None


def installed_font_files() -> Dict[str, List[str]]:
    """{family name: [font file paths]} for fonts installed on this Windows machine."""
    global _FILE_CACHE
    if _FILE_CACHE is not None:
        return _FILE_CACHE
    out: Dict[str, List[str]] = {}
    _FILE_CACHE = out
    if sys.platform != "win32":
        return out
    try:
        import winreg
    except ImportError:
        return out
    win_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
    ]
    for hive, path in keys:
        try:
            with winreg.OpenKey(hive, path) as k:
                i = 0
                while True:
                    try:
                        name, data, _ = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    i += 1
                    fpath = Path(str(data))
                    if not fpath.is_absolute():
                        fpath = win_fonts / fpath
                    name = re.sub(r"\s*\((TrueType|OpenType|All res|VGA res)[^)]*\)\s*$", "", name)
                    for part in name.split("&"):
                        part = part.strip()
                        if not part:
                            continue
                        names = {part}
                        base = part
                        for _ in range(4):
                            new = re.sub(rf"\s+{_STYLE_WORDS}$", "", base, flags=re.I)
                            if new == base:
                                break
                            base = new
                        if base:
                            names.add(base)
                        for n in names:
                            lst = out.setdefault(n, [])
                            if str(fpath) not in lst:
                                lst.append(str(fpath))
        except OSError:
            continue
    return out


def installed_font_families() -> Set[str]:
    """Font families installed on this Windows machine (empty set elsewhere)."""
    return set(installed_font_files())


class FontResolver:
    def __init__(self, font_map: Optional[Dict[str, str]] = None,
                 generic: Optional[Dict[str, str]] = None,
                 default_family: str = "Times New Roman",
                 style_names: Optional[Dict[str, Dict[str, str]]] = None,
                 weight_mode: str = "simple",
                 check_installed: bool = True,
                 webfont_families: Iterable[str] = ()):
        self.font_map = {k.lower(): v for k, v in (font_map or {}).items()}
        self.generic = dict(GENERIC_FAMILIES)
        self.generic.update({k.lower(): v for k, v in (generic or {}).items()})
        self.default_family = default_family
        self.style_names = {k.lower(): v for k, v in (style_names or {}).items()}
        self.weight_mode = weight_mode
        self.installed = installed_font_families() if check_installed else set()
        self._installed_lower = {f.lower(): f for f in self.installed}
        self.webfonts = {f.lower(): f for f in webfont_families}
        self.used: Dict[str, Set[str]] = {}
        self._cache: Dict[str, str] = {}

    def family(self, css_value: str) -> str:
        if css_value in self._cache:
            return self._cache[css_value]
        fams = parse_font_family(css_value) or ["serif"]
        chosen = None
        for f in fams:
            fl = f.lower()
            if fl in self.font_map:
                chosen = self.font_map[fl]
                break
            if fl in self.generic:
                chosen = self.font_map.get(self.generic[fl].lower(), self.generic[fl])
                break
            if fl in self.webfonts:
                chosen = self.webfonts[fl]
                break
            if not self.installed:          # cannot check -> trust the stack (browser-like)
                chosen = f
                break
            if fl in self._installed_lower:
                chosen = self._installed_lower[fl]
                break
        if chosen is None:
            chosen = self.font_map.get(fams[0].lower(), self.default_family)
        self._cache[css_value] = chosen
        return chosen

    def files_for(self, family: str) -> List[str]:
        """Installed font files belonging to an InDesign family (Windows)."""
        files = installed_font_files()
        for k, v in files.items():
            if k.lower() == family.lower():
                return [f for f in v if Path(f).is_file()]
        return []

    def style(self, family: str, weight: int, italic: bool) -> str:
        weight = int(round(max(100, min(900, weight)) / 100.0) * 100)
        table = self.style_names.get(family.lower())
        if table:
            for key in (f"{weight}{'i' if italic else ''}", f"{weight} italic" if italic else str(weight),
                        "bold" if weight >= 600 else "regular"):
                if key in table:
                    name = table[key]
                    break
            else:
                name = None
            if name and italic and not key.endswith(("i", "italic")):
                name = f"{name} Italic" if name not in ("Regular", "Roman") else "Italic"
            if name:
                self.used.setdefault(family, set()).add(name)
                return name
        if self.weight_mode == "full":
            base = WEIGHT_NAMES[weight]
        else:
            base = "Bold" if weight >= 600 else "Regular"
        if italic:
            name = "Italic" if base == "Regular" else f"{base} Italic"
        else:
            name = base
        self.used.setdefault(family, set()).add(name)
        return name
