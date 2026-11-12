"""High level API:  html_to_ir()  ->  (edit / transform)  ->  ir_to_idml().

    from html2idml import convert
    convert("page.html", "out/page.idml")

    from html2idml import html_to_ir, ir_to_idml
    doc = html_to_ir("page.html", "out")
    doc.paragraph_styles["h1"].props["font_size"] = 30
    ir_to_idml(doc, "out/page.idml")
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

import lxml.html
from lxml import etree

from .assets import AssetManager, Resolver, decode_text, is_url
from .builder import Builder
from .config import Config, length_pt, parse_margins, parse_page_size
from .css import UA_CSS, Cascade, StyleCollector, media_matches
from .cssvalues import LengthContext, css_url, expand, parse_font_family, split_values, to_pt
from .fonts import FontResolver
from .idml_writer import IDMLWriter
from .ir import Document, PageSetup
from .jsx import FIT_PAGES_JSX
from .layout import estimate_pages

ConfigLike = Union[None, str, Path, Dict[str, Any], Config]


class Warnings(list):
    """A de-duplicating warning list that is also callable: ``warn("message")``."""

    def __init__(self, items=(), callback: Optional[Callable[[str], None]] = None):
        super().__init__(items)
        self.callback = callback

    def __call__(self, msg: str):
        if msg not in self:
            self.append(msg)
            if self.callback:
                self.callback(msg)


def _config(cfg: ConfigLike) -> Config:
    return cfg if isinstance(cfg, Config) else Config.load(cfg)


def load_plugins(paths: List[str]):
    mods = []
    for i, p in enumerate(paths or []):
        spec = importlib.util.spec_from_file_location(f"html2idml_plugin_{i}", p)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load plugin {p}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mods.append(mod)
    return mods


def read_html(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    head = data[:4096].decode("ascii", "ignore")
    m = re.search(r"""<meta[^>]+charset\s*=\s*["']?([\w-]+)""", head, re.I)
    if m:
        try:
            return data.decode(m.group(1), "replace")
        except LookupError:
            pass
    return decode_text(data)


def _page_setup(cfg: Config, collector: StyleCollector) -> PageSetup:
    pc = cfg["page"]
    page = PageSetup()
    size = parse_page_size(pc.get("size"))
    if size is None and pc.get("width") and pc.get("height"):
        size = (length_pt(pc["width"]), length_pt(pc["height"]))
    css_margins = None
    if pc.get("use_css_page_rule", True) and collector.page_decls:
        css = {}
        for name, value, _ in collector.page_decls:
            for ln, lv in expand(name, value):
                css[ln] = lv
        if size is None and css.get("size") and css["size"].strip().lower() not in ("auto",):
            size = parse_page_size(css["size"])
        ctx = LengthContext(12.0, 12.0)
        if any(f"margin-{s}" in css for s in ("top", "right", "bottom", "left")):
            css_margins = tuple((to_pt(css.get(f"margin-{s}", "0"), ctx) or 0.0)
                                for s in ("top", "right", "bottom", "left"))
    if size is None:
        size = (page.width, page.height)
    w, h = size
    orient = (pc.get("orientation") or "").lower()
    if (orient == "landscape" and w < h) or (orient == "portrait" and w > h):
        w, h = h, w
    page.width, page.height = float(w), float(h)
    margins = parse_margins(pc.get("margins")) or css_margins
    if margins:
        page.margin_top, page.margin_right, page.margin_bottom, page.margin_left = margins
    page.columns = max(1, int(pc.get("columns") or 1))
    page.gutter = length_pt(pc.get("gutter"), 12.0)
    page.facing_pages = bool(pc.get("facing_pages"))
    page.page_count = pc.get("page_count")
    return page


def html_to_ir(source: Union[str, Path], out_dir: Union[str, Path], config: ConfigLike = None,
               html_text: Optional[str] = None, plugins: Optional[List[str]] = None,
               on_warning: Optional[Callable[[str], None]] = None,
               on_progress: Optional[Callable[[str, float], None]] = None) -> Document:
    """Parse HTML+CSS into an IR Document.  Assets are copied into ``out_dir``.

    ``on_progress(stage, fraction)`` is called as the conversion advances (0.0 .. 1.0).
    """
    t0 = time.time()
    progress = on_progress or (lambda stage, frac: None)
    progress("Reading HTML", 0.05)
    cfg = _config(config)
    warn = Warnings(callback=on_warning)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    resolver = Resolver(allow_remote=bool(cfg["images"].get("download_remote")), warn=warn)

    src = str(source)
    if html_text is not None:
        base = str(Path(src).resolve().parent) if src and not is_url(src) else src
        text = html_text
    elif is_url(src):
        data, loc, base = resolver.read(src, "")
        if data is None:
            raise FileNotFoundError(f"cannot download {src} (enable images.download_remote)")
        text = read_html(data)
    else:
        p = Path(src).resolve()
        text = read_html(p.read_bytes())
        base = str(p.parent)

    root = lxml.html.document_fromstring(text or "<html><body></body></html>")
    for bad in root.xpath("//comment() | //processing-instruction()"):
        parent = bad.getparent()
        if parent is not None:
            # keep the tail text when dropping comments
            if bad.tail:
                prev = bad.getprevious()
                if prev is not None:
                    prev.tail = (prev.tail or "") + bad.tail
                else:
                    parent.text = (parent.text or "") + bad.tail
            parent.remove(bad)
    base_el = root.find(".//base[@href]")
    if base_el is not None:
        kind, loc = resolver.join(base_el.get("href"), base)
        base = loc if kind == "url" else (loc if Path(loc).is_dir() else str(Path(loc).parent))

    mods = load_plugins((plugins or []) + list(cfg.get("plugins") or []))
    for m in mods:
        if hasattr(m, "transform_html"):
            r = m.transform_html(root, cfg)
            if r is not None:
                root = r

    progress("Loading stylesheets", 0.12)
    css_cfg = cfg["css"]
    vw = float(css_cfg.get("viewport_width") or 1280)
    vh = float(css_cfg.get("viewport_height") or 800)
    media = css_cfg.get("media") or "screen"
    collector = StyleCollector(media, vw, resolver.fetch_text, warn)
    collector.add_css(UA_CSS, base, origin=0)
    for el in root.iter("style", "link"):
        if el.tag == "style":
            if media_matches(el.get("media") or "", media, vw):
                collector.add_css(el.text or "", base, origin=1)
        else:
            rel = (el.get("rel") or "").lower().split()
            if "stylesheet" in rel and "alternate" not in rel and el.get("href"):
                if not media_matches(el.get("media") or "", media, vw):
                    continue
                css_text, css_base = resolver.fetch_text(el.get("href"), base)
                if css_text is None:
                    warn(f"stylesheet not loaded: {el.get('href')}")
                else:
                    collector.add_css(css_text, css_base, origin=1)
    for extra in css_cfg.get("extra_stylesheets") or []:
        css_text, css_base = resolver.fetch_text(str(extra), str(Path.cwd()))
        if css_text is None:
            warn(f"extra stylesheet not found: {extra}")
        else:
            collector.add_css(css_text, css_base, origin=1)
    if css_cfg.get("user_css"):
        collector.add_css(css_cfg["user_css"], base, origin=1)

    progress("Page setup", 0.25)
    page = _page_setup(cfg, collector)
    doc = Document(page=page)
    title = root.findtext(".//title")
    doc.metadata = {"source": src, "title": (title or "").strip(), "lang": root.get("lang", ""),
                    "generator": "html2idml", "created": time.strftime("%Y-%m-%d %H:%M:%S")}

    ocfg = cfg["output"]
    assets = AssetManager(out_dir / ocfg.get("links_dir", "Links"),
                          out_dir / ocfg.get("fonts_dir", "Document fonts"), resolver, doc.assets,
                          warn, convert_unsupported=bool(cfg["images"].get("convert_unsupported", True)),
                          default_ppi=float(cfg["images"].get("default_ppi") or 72))

    # web fonts
    fcfg = cfg["fonts"]
    webfonts = []
    for desc, fbase in collector.font_faces:
        fam_list = parse_font_family(desc.get("font-family", ""))
        if not fam_list:
            continue
        fam = fam_list[0]
        webfonts.append(fam)
        if not fcfg.get("copy_webfonts", True):
            continue
        srcs = split_values(desc.get("src", ""), ",")
        best = None
        for s in srcs:
            u = css_url(s)
            if not u:
                continue
            low = s.lower()
            if any(k in low for k in ("truetype", "opentype", ".ttf", ".otf")):
                best = u
                break
            best = best or u
        if best:
            assets.font(best, fbase, fam)

    viewport_pt = (vw * 0.75, vh * 0.75)
    progress(f"Applying {len(collector.rules)} CSS rules", 0.3)
    cascade = Cascade(root, collector, viewport_pt, warn)
    fonts = FontResolver(font_map=fcfg.get("map"), generic=fcfg.get("generic"),
                         default_family=fcfg.get("default_family", "Times New Roman"),
                         style_names=fcfg.get("style_names"), weight_mode=fcfg.get("weight_mode", "simple"),
                         check_installed=bool(fcfg.get("check_installed", True)),
                         webfont_families=webfonts)
    progress("Building paragraphs, tables and images", 0.5)
    Builder(root, cascade, cfg, doc, assets, fonts, base, warn).build()
    progress("Packaging fonts", 0.65)
    doc.metadata["fonts_used"] = {k: sorted(v) for k, v in fonts.used.items()}
    if fcfg.get("package_fonts", True):
        webfont_fams = {a.family.lower() for a in doc.assets.values() if a.kind == "font"}
        packaged = []
        for fam in fonts.used:
            if fam.lower() in webfont_fams:
                continue
            files = fonts.files_for(fam)
            for f in files:
                assets.local_font(f, fam)
            if files:
                packaged.append(fam)
        doc.metadata["fonts_packaged"] = packaged
    if fonts.installed:
        missing = [f for f in fonts.used if f.lower() not in {x.lower() for x in fonts.installed}]
        doc.metadata["fonts_not_installed"] = missing
        for f in missing:
            warn(f"font '{f}' is not installed on this machine - InDesign will report it missing")
    for m in mods:
        if hasattr(m, "transform_ir"):
            r = m.transform_ir(doc, cfg)
            if r is not None:
                doc = r
    doc.warnings = list(warn)
    doc.metadata["parse_seconds"] = round(time.time() - t0, 3)
    return doc


def ir_to_idml(doc: Document, out_path: Union[str, Path], config: ConfigLike = None,
               on_warning: Optional[Callable[[str], None]] = None,
               on_progress: Optional[Callable[[str, float], None]] = None) -> Dict[str, Any]:
    """Write the IR to an .idml file (+ fit-pages script, report, optional zip package)."""
    t0 = time.time()
    progress = on_progress or (lambda stage, frac: None)
    progress("Estimating pages", 0.72)
    cfg = _config(config)
    out_path = Path(out_path)
    if out_path.suffix.lower() != ".idml":
        out_path = out_path.with_suffix(".idml")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    warnings = warn = Warnings(doc.warnings, callback=on_warning)

    n_pages = int(doc.page.page_count or estimate_pages(doc))
    progress(f"Writing IDML ({n_pages} pages)", 0.78)
    stats = IDMLWriter(doc, cfg, warn).write(out_path, n_pages)
    ocfg = cfg["output"]
    outputs = {"idml": str(out_path)}
    if ocfg.get("write_jsx", True):
        jsx = out_path.with_name(out_path.stem + ".fit-pages.jsx")
        jsx.write_text(FIT_PAGES_JSX, encoding="utf-8")
        outputs["jsx"] = str(jsx)
    if ocfg.get("write_ir"):
        irp = out_path.with_name(out_path.stem + ".ir.json")
        doc.to_json(irp)
        outputs["ir"] = str(irp)
    report = {
        "outputs": outputs,
        "stats": stats,
        "paragraph_styles": len(doc.paragraph_styles),
        "character_styles": len(doc.character_styles),
        "colors": len(doc.colors),
        "assets": [{"id": a.id, "kind": a.kind, "file": a.path, "source": a.source,
                    "format": a.format} for a in doc.assets.values()],
        "fonts_used": doc.metadata.get("fonts_used", {}),
        "warnings": list(warnings),
        "write_seconds": round(time.time() - t0, 3),
    }
    if ocfg.get("write_report", True):
        rp = out_path.with_name(out_path.stem + ".report.json")
        rp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        outputs["report"] = str(rp)
    if ocfg.get("zip_package"):
        progress("Creating package zip", 0.9)
        zp = out_path.with_name(out_path.stem + "-package.zip")
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(out_path, out_path.name)
            for key in ("jsx", "report"):
                if key in outputs:
                    z.write(outputs[key], Path(outputs[key]).name)
            for a in doc.assets.values():
                if a.kind == "image" and cfg["images"].get("embed") and not ocfg.get("zip_links", True):
                    continue
                ap = Path(a.path)
                if ap.exists():
                    try:
                        rel = ap.relative_to(out_path.parent)
                    except ValueError:
                        folder = (ocfg.get("links_dir", "Links") if a.kind == "image"
                                  else ocfg.get("fonts_dir", "Document fonts"))
                        rel = Path(folder) / ap.name
                    z.write(ap, str(rel))
        outputs["package"] = str(zp)
    progress("Done", 1.0)
    return report


def convert(source: Union[str, Path], out_path: Union[str, Path], config: ConfigLike = None,
            plugins: Optional[List[str]] = None,
            on_warning: Optional[Callable[[str], None]] = None,
            on_progress: Optional[Callable[[str, float], None]] = None) -> Dict[str, Any]:
    """HTML file/URL -> .idml in one call. Returns the report dict."""
    cfg = _config(config)
    out_path = Path(out_path)
    doc = html_to_ir(source, out_path.parent, cfg, plugins=plugins, on_warning=on_warning,
                     on_progress=on_progress)
    report = ir_to_idml(doc, out_path, cfg, on_warning=on_warning, on_progress=on_progress)
    report["parse_seconds"] = doc.metadata.get("parse_seconds")
    return report
