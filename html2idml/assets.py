"""Resolving, fetching and packaging every external resource (CSS, images, fonts)."""
from __future__ import annotations

import base64
import hashlib
import io
import mimetypes
import re
import shutil
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from .ir import Asset

try:
    from PIL import Image as PILImage
except Exception:  # pragma: no cover
    PILImage = None

# Formats InDesign can place directly
PLACEABLE = {"JPEG", "PNG", "GIF", "TIFF", "BMP", "PSD", "SVG", "PDF", "EPS", "AI"}
EXT_FORMAT = {
    ".jpg": "JPEG", ".jpeg": "JPEG", ".jpe": "JPEG", ".png": "PNG", ".gif": "GIF",
    ".tif": "TIFF", ".tiff": "TIFF", ".bmp": "BMP", ".psd": "PSD", ".svg": "SVG",
    ".pdf": "PDF", ".eps": "EPS", ".ai": "AI", ".webp": "WEBP", ".avif": "AVIF",
    ".ico": "ICO", ".jfif": "JPEG",
}
FORMAT_EXT = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "TIFF": ".tif", "BMP": ".bmp",
              "PSD": ".psd", "SVG": ".svg", "PDF": ".pdf", "EPS": ".eps", "AI": ".ai"}


def is_url(s: str) -> bool:
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", s or "")) and not s.lower().startswith("file:")


class Resolver:
    """Turns href/src values into bytes, relative to a base (local dir or URL)."""

    def __init__(self, allow_remote: bool = False, timeout: float = 20.0,
                 warn: Callable[[str], None] = print):
        self.allow_remote = allow_remote
        self.timeout = timeout
        self.warn = warn
        self._cache: Dict[str, Optional[bytes]] = {}

    # base is either "C:/dir" (local directory) or "https://host/path/"
    def join(self, href: str, base: str) -> Tuple[str, str]:
        """Return (kind, location) where kind is 'file', 'url' or 'data'."""
        href = (href or "").strip()
        if href.startswith("data:"):
            return "data", href
        if is_url(href):
            return "url", href
        if href.lower().startswith("file:"):
            p = urllib.request.url2pathname(urllib.parse.urlparse(href).path)
            return "file", str(Path(p))
        if is_url(base):
            return "url", urllib.parse.urljoin(base, href)
        clean = urllib.parse.unquote(href.split("#", 1)[0].split("?", 1)[0])
        p = Path(clean)
        if not p.is_absolute():
            p = Path(base) / p
        return "file", str(p.resolve())

    def read(self, href: str, base: str) -> Tuple[Optional[bytes], str, str]:
        """Return (data, location, new_base_for_relative_refs)."""
        kind, loc = self.join(href, base)
        if loc in self._cache:
            data = self._cache[loc]
        else:
            data = None
            try:
                if kind == "data":
                    data = decode_data_uri(loc)[0]
                elif kind == "url":
                    if self.allow_remote:
                        req = urllib.request.Request(loc, headers={"User-Agent": "html2idml/1.0"})
                        with urllib.request.urlopen(req, timeout=self.timeout) as r:
                            data = r.read()
                    else:
                        self.warn(f"remote resource skipped (enable images.download_remote): {loc}")
                else:
                    pp = Path(loc)
                    if pp.is_file():
                        data = pp.read_bytes()
                    else:
                        self.warn(f"file not found: {loc}")
            except Exception as e:  # network/IO errors are reported, never fatal
                self.warn(f"could not read {loc}: {e}")
            self._cache[loc] = data
        if kind == "url":
            new_base = loc.rsplit("/", 1)[0] + "/"
        elif kind == "file":
            new_base = str(Path(loc).parent)
        else:
            new_base = base
        return data, loc, new_base

    def fetch_text(self, href: str, base: str) -> Tuple[Optional[str], str]:
        data, _, new_base = self.read(href, base)
        if data is None:
            return None, base
        return decode_text(data), new_base


def decode_text(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    m = re.match(rb'@charset\s+"([^"]+)"', data)
    if m:
        try:
            return data.decode(m.group(1).decode("ascii"), "replace")
        except LookupError:
            pass
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace")


def decode_data_uri(uri: str) -> Tuple[bytes, str]:
    header, _, payload = uri.partition(",")
    mime = header[5:].split(";")[0] or "text/plain"
    if ";base64" in header:
        return base64.b64decode(payload + "=" * (-len(payload) % 4)), mime
    return urllib.parse.unquote_to_bytes(payload), mime


def _safe_name(name: str) -> str:
    name = urllib.parse.unquote(name)
    name = re.sub(r"[^\w.\- ()]+", "_", name).strip(" ._") or "asset"
    return name[:120]


class AssetManager:
    """Copies every image/font into the output package and records it in the IR."""

    def __init__(self, links_dir: Path, fonts_dir: Path, resolver: Resolver,
                 assets: Dict[str, Asset], warn: Callable[[str], None],
                 convert_unsupported: bool = True, default_ppi: float = 72.0):
        self.links_dir = Path(links_dir)
        self.fonts_dir = Path(fonts_dir)
        self.resolver = resolver
        self.assets = assets
        self.warn = warn
        self.convert_unsupported = convert_unsupported
        self.default_ppi = default_ppi
        self._by_source: Dict[str, str] = {}
        self._used_names = set()
        self._counter = 0

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"

    def _target(self, folder: Path, name: str) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        stem, ext = Path(name).stem, Path(name).suffix
        candidate = name
        i = 1
        while candidate.lower() in self._used_names:
            i += 1
            candidate = f"{stem}-{i}{ext}"
        self._used_names.add(candidate.lower())
        return folder / candidate

    # ------------------------------------------------------------------ images
    def image(self, src: str, base: str) -> Optional[Asset]:
        if not src:
            return None
        kind, loc = self.resolver.join(src, base)
        key = loc if kind != "data" else hashlib.sha1(loc.encode()).hexdigest()
        if key in self._by_source:
            return self.assets[self._by_source[key]]
        data, loc, _ = self.resolver.read(src, base)
        if data is None:
            return None
        if kind == "data":
            mime = decode_data_uri(loc)[1]
            ext = mimetypes.guess_extension(mime) or ".bin"
            if mime == "image/svg+xml":
                ext = ".svg"
            name = f"embedded-image{ext}"
        else:
            path_part = urllib.parse.urlparse(loc).path if kind == "url" else loc
            name = Path(path_part).name or "image"
            if not Path(name).suffix:
                name += ".png"
        asset = self._store_image(data, _safe_name(name), src)
        if asset:
            self._by_source[key] = asset.id
        return asset

    def svg_markup(self, markup: bytes, hint: str = "inline-svg") -> Optional[Asset]:
        key = hashlib.sha1(markup).hexdigest()
        if key in self._by_source:
            return self.assets[self._by_source[key]]
        asset = self._store_image(markup, f"{hint}.svg", "<svg>")
        if asset:
            self._by_source[key] = asset.id
        return asset

    def _store_image(self, data: bytes, name: str, source: str) -> Optional[Asset]:
        ext = Path(name).suffix.lower()
        fmt = EXT_FORMAT.get(ext, "")
        width = height = 0.0
        ppi = self.default_ppi
        looks_svg = data[:512].lstrip().startswith((b"<svg", b"<?xml")) and b"<svg" in data[:4096]
        if fmt == "SVG" or looks_svg:
            fmt = "SVG"
            width, height = svg_size(data)
            if not name.lower().endswith(".svg"):
                name = Path(name).stem + ".svg"
        elif PILImage is not None:
            try:
                im = PILImage.open(io.BytesIO(data))
                fmt = (im.format or fmt or "").upper()
                if fmt == "MPO":
                    fmt = "JPEG"
                width, height = im.size
                dpi = im.info.get("dpi")
                if dpi and dpi[0] and float(dpi[0]) > 1:
                    ppi = float(round(float(dpi[0])))
                if fmt not in PLACEABLE and self.convert_unsupported:
                    out = io.BytesIO()
                    conv = im.convert("RGBA") if im.mode not in ("RGB", "RGBA", "L", "LA") else im
                    conv.save(out, "PNG")
                    data = out.getvalue()
                    self.warn(f"converted {fmt or ext} image to PNG for InDesign: {source}")
                    fmt = "PNG"
                    name = Path(name).stem + ".png"
                    ppi = self.default_ppi
            except Exception as e:
                if fmt not in ("PDF", "EPS", "AI", "PSD"):
                    self.warn(f"unreadable image {source}: {e}")
                    return None
        if fmt in FORMAT_EXT and Path(name).suffix.lower() not in (FORMAT_EXT[fmt], ".jpeg", ".tiff"):
            name = Path(name).stem + FORMAT_EXT[fmt]
        target = self._target(self.links_dir, name)
        target.write_bytes(data)
        asset = Asset(id=self._next_id("img"), kind="image", source=source,
                      path=str(target.resolve()), format=fmt, width_px=float(width),
                      height_px=float(height), ppi=ppi)
        self.assets[asset.id] = asset
        return asset

    # ------------------------------------------------------------------ fonts
    def local_font(self, path: str, family: str) -> Optional[Asset]:
        """Copy an installed font file (e.g. C:/Windows/Fonts/segoeui.ttf) into the package."""
        key = "localfont:" + str(Path(path).resolve()).lower()
        if key in self._by_source:
            return self.assets[self._by_source[key]]
        try:
            data = Path(path).read_bytes()
        except OSError as e:
            self.warn(f"could not copy font {path}: {e}")
            return None
        target = self._target(self.fonts_dir, _safe_name(Path(path).name))
        target.write_bytes(data)
        asset = Asset(id=self._next_id("font"), kind="font", source=str(path),
                      path=str(target.resolve()), format=Path(path).suffix.lstrip(".").upper(),
                      family=family)
        self.assets[asset.id] = asset
        self._by_source[key] = asset.id
        return asset

    def font(self, src: str, base: str, family: str) -> Optional[Asset]:
        kind, loc = self.resolver.join(src, base)
        if loc in self._by_source:
            return self.assets[self._by_source[loc]]
        data, loc, _ = self.resolver.read(src, base)
        if data is None:
            return None
        if kind == "data":
            name = _safe_name(family) + ".otf"
        else:
            path_part = urllib.parse.urlparse(loc).path if kind == "url" else loc
            name = _safe_name(Path(path_part).name or family)
        if name.lower().endswith((".woff", ".woff2")):
            self.warn(f"web font {name} copied, but InDesign cannot use WOFF/WOFF2 - "
                      f"install the TTF/OTF version of '{family}'")
        target = self._target(self.fonts_dir, name)
        target.write_bytes(data)
        asset = Asset(id=self._next_id("font"), kind="font", source=src,
                      path=str(target.resolve()), format=Path(name).suffix.lstrip(".").upper(),
                      family=family)
        self.assets[asset.id] = asset
        self._by_source[loc] = asset.id
        return asset


def svg_size(data: bytes) -> Tuple[float, float]:
    """Best-effort intrinsic size of an SVG in CSS px."""
    head = data[:4096].decode("utf-8", "ignore")
    m = re.search(r"<svg\b[^>]*>", head, re.S)
    if not m:
        return 300.0, 150.0
    tag = m.group(0)

    def attr(n):
        mm = re.search(rf'\b{n}\s*=\s*["\']([^"\']+)["\']', tag)
        return mm.group(1) if mm else None

    def px(v):
        if not v:
            return None
        mm = re.match(r"\s*(\d*\.?\d+)\s*(px|pt|mm|cm|in)?\s*$", v)
        if not mm:
            return None
        n = float(mm.group(1))
        return n * {"px": 1, None: 1, "pt": 4 / 3, "mm": 96 / 25.4, "cm": 96 / 2.54, "in": 96}[mm.group(2)]

    w, h = px(attr("width")), px(attr("height"))
    vb = attr("viewBox")
    if vb:
        parts = re.split(r"[\s,]+", vb.strip())
        if len(parts) == 4:
            try:
                vw, vh = float(parts[2]), float(parts[3])
                if w and not h and vw:
                    h = w * vh / vw
                elif h and not w and vh:
                    w = h * vw / vh
                elif not w and not h:
                    w, h = vw, vh
            except ValueError:
                pass
    return (w or 300.0), (h or 150.0)


def copy_tree_file(src: Path, dst_dir: Path) -> Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    shutil.copy2(src, dst)
    return dst
