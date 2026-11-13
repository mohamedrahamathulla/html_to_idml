"""Resolving, fetching and packaging every external resource (CSS, images, fonts)."""
from __future__ import annotations

import base64
import hashlib
import io
import mimetypes
import re
import shutil
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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


BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
ACCEPT = {
    # ask for formats InDesign can place, so servers do not swap JPEGs for WEBP/AVIF
    "image": "image/jpeg,image/png,image/gif,image/svg+xml,image/tiff;q=0.9,image/*;q=0.5,*/*;q=0.3",
    "css": "text/css,*/*;q=0.1",
    "html": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "font": "font/woff2,font/ttf,font/otf,*/*;q=0.5",
}


def _guess_kind(url: str) -> str:
    ext = Path(urllib.parse.urlsplit(url).path.lower()).suffix
    if ext == ".css":
        return "css"
    if ext in (".ttf", ".otf", ".woff", ".woff2"):
        return "font"
    if ext in (".html", ".htm", ".php", ".asp", ".aspx", ".jsp"):
        return "html"
    if ext in EXT_FORMAT:
        return "image"
    return "other"


_SSL_CTX = None


def ssl_context() -> ssl.SSLContext:
    """HTTPS context that validates certificates the way the browser on this PC does.

    Uses the operating-system certificate store (``truststore``) when installed, so company
    root certificates trusted by Windows are trusted here too.  Otherwise it uses Python's
    default verification minus the extra ``VERIFY_X509_STRICT`` rule added in Python 3.13,
    which rejects many corporate proxy certificates that browsers accept.  Certificate
    verification itself always stays on.
    """
    global _SSL_CTX
    if _SSL_CTX is None:
        try:
            import truststore
            _SSL_CTX = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception:
            ctx = ssl.create_default_context()
            if hasattr(ssl, "VERIFY_X509_STRICT"):
                ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
            _SSL_CTX = ctx
    return _SSL_CTX


class FetchError(Exception):
    pass


class Resolver:
    """Turns href/src values into bytes, relative to a base (local dir or URL)."""

    def __init__(self, allow_remote: bool = False, timeout: float = 25.0,
                 warn: Callable[[str], None] = print, retries: int = 2,
                 url_map: Optional[Dict[str, str]] = None):
        self.allow_remote = allow_remote
        # {remote URL: local file} - use a local copy instead of downloading
        self.url_map = {k.strip(): v for k, v in (url_map or {}).items()}
        self.timeout = timeout
        self.warn = warn
        self.retries = retries
        self._cache: Dict[str, Optional[bytes]] = {}
        self._errors: Dict[str, str] = {}
        self._lock = threading.Lock()

    # base is either "C:/dir" (local directory) or "https://host/path/"
    def join(self, href: str, base: str) -> Tuple[str, str]:
        """Return (kind, location) where kind is 'file', 'url' or 'data'."""
        href = (href or "").strip()
        if href.startswith("data:"):
            return "data", href
        if href.startswith("//"):
            return "url", "https:" + href
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

    # ------------------------------------------------------------------ network
    def _download(self, url: str, referer: Optional[str] = None) -> bytes:
        kind = _guess_kind(url)
        headers = {"User-Agent": BROWSER_UA, "Accept": ACCEPT.get(kind, "*/*"),
                   "Accept-Language": "en-US,en;q=0.9", "Connection": "close"}
        if kind == "image":
            headers.update({"Sec-Fetch-Dest": "image", "Sec-Fetch-Mode": "no-cors"})
        if referer and is_url(referer):
            parts = urllib.parse.urlsplit(referer)
            headers["Referer"] = f"{parts.scheme}://{parts.netloc}/"
        last = None
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout, context=ssl_context()) as r:
                    data = r.read()
                    ctype = (r.headers.get("Content-Type") or "").lower()
                if kind in ("image", "font") and ctype.startswith("text/html") and \
                        b"<svg" not in data[:600]:
                    raise FetchError("the server sent a web page instead of the file "
                                     "(blocked by a web filter, a login, or hotlink protection)")
                return data
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise FetchError(f"HTTP {e.code} refused - the website or your network's web "
                                     f"filter blocks it; save the file from your browser and "
                                     f"upload it together with the page")
                if e.code == 404:
                    raise FetchError("HTTP 404 - not found on the server")
                last = f"HTTP {e.code}"
                if e.code < 500 and e.code != 429:
                    break
            except FetchError:
                raise
            except Exception as e:  # timeouts, connection resets, DNS
                reason = getattr(e, "reason", e)
                if isinstance(reason, ssl.SSLError) or "CERTIFICATE" in str(e).upper():
                    raise FetchError(f"secure connection failed ({reason}); install the "
                                     f"'truststore' package so the Windows certificate store is used")
                last = f"{type(e).__name__}: {reason}"
            time.sleep(0.6 * (attempt + 1))
        raise FetchError(last or "download failed")

    def prefetch(self, urls, referer: Optional[str] = None, workers: int = 8):
        """Download many URLs in parallel into the cache; read() reports any failures."""
        if not self.allow_remote:
            return
        todo = [u for u in dict.fromkeys(urls)
                if u and is_url(u) and u not in self._cache and u not in self.url_map]
        if not todo:
            return

        def one(u):
            try:
                data, err = self._download(u, referer), None
            except Exception as e:  # noqa: BLE001
                data, err = None, str(e)
            with self._lock:
                self._cache[u] = data
                if err:
                    self._errors[u] = err

        with ThreadPoolExecutor(max_workers=min(workers, len(todo))) as pool:
            list(pool.map(one, todo))

    def read(self, href: str, base: str) -> Tuple[Optional[bytes], str, str]:
        """Return (data, location, new_base_for_relative_refs)."""
        kind, loc = self.join(href, base)
        if loc in self._cache:
            data = self._cache[loc]
            if data is None and loc in self._errors:
                self.warn(f"could not download {loc}: {self._errors.pop(loc)}")
        else:
            data = None
            try:
                if kind == "data":
                    data = decode_data_uri(loc)[0]
                elif loc in self.url_map:
                    data = Path(self.url_map[loc]).read_bytes()
                elif kind == "url":
                    if self.allow_remote:
                        data = self._download(loc, base if is_url(base) else None)
                    else:
                        self.warn(f"remote file skipped - turn on 'Download http(s) images & CSS' "
                                  f"(images.download_remote): {loc}")
                else:
                    pp = Path(loc)
                    if pp.is_file():
                        data = pp.read_bytes()
                    else:
                        self.warn(f"file not found: {loc}")
            except Exception as e:  # network/IO errors are reported, never fatal
                self.warn(f"could not download {loc}: {e}" if kind == "url" else f"could not read {loc}: {e}")
            self._cache[loc] = data
        if kind == "url":
            new_base = urllib.parse.urljoin(loc, ".")
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
                    has_alpha = im.mode in ("RGBA", "LA", "PA") or (
                        im.mode == "P" and "transparency" in im.info)
                    if has_alpha:          # keep transparency
                        im.convert("RGBA").save(out, "PNG")
                        new_fmt, new_ext = "PNG", ".png"
                    else:                  # photos stay photos: high-quality JPEG
                        im.convert("RGB").save(out, "JPEG", quality=95, subsampling=0,
                                               dpi=(ppi, ppi))
                        new_fmt, new_ext = "JPEG", ".jpg"
                    data = out.getvalue()
                    self.warn(f"converted {fmt or ext} image to {new_fmt} for InDesign: {source}")
                    fmt = new_fmt
                    name = Path(name).stem + new_ext
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
