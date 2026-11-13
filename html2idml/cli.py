"""Command line interface.

    python -m html2idml convert page.html -o out/page.idml [-c config.yaml]
    python -m html2idml extract page.html -o out/page.ir.json     # HTML -> editable IR
    python -m html2idml build   out/page.ir.json -o out/page.idml # edited IR -> IDML
    python -m html2idml validate out/page.idml
    python -m html2idml serve                                     # web UI on http://127.0.0.1:8765
    python -m html2idml page.html                                 # shorthand for convert
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Config
from .ir import Document
from .pipeline import convert, html_to_ir, ir_to_idml
from .validate import validate_idml


def _common(p):
    p.add_argument("-c", "--config", help="YAML/JSON config file")
    p.add_argument("--page", help='page size: A4, Letter, "210mm 297mm", "A5 landscape"')
    p.add_argument("--margins", help='e.g. "20mm" or "20mm 15mm 20mm 15mm"')
    p.add_argument("--columns", type=int)
    p.add_argument("--gutter")
    p.add_argument("--facing", action="store_true", help="facing pages (spreads)")
    p.add_argument("--pages", type=int, help="force the number of pages")
    p.add_argument("--media", help="CSS media type to emulate (screen|print)")
    p.add_argument("--viewport", type=int, help="viewport width in px for media queries")
    p.add_argument("--css", action="append", help="extra stylesheet (repeatable)")
    p.add_argument("--plugin", action="append", default=[], help="python plugin file (repeatable)")
    p.add_argument("--link-images", action="store_true",
                   help="link images from Links/ instead of embedding them in the IDML")
    p.add_argument("--no-font-package", action="store_true",
                   help="do not copy installed font files into Document fonts/")
    p.add_argument("--no-remote", action="store_true", help="do not download http(s) images/CSS")
    p.add_argument("--cmyk", action="store_true", help="create CMYK swatches instead of RGB")
    p.add_argument("--dom-version", help='IDML DOMVersion (default 16.0; "8.0" for CS6)')
    p.add_argument("--zip", action="store_true", help="also write a zip package with all assets")
    p.add_argument("--quiet", "-q", action="store_true")


def _cfg(a) -> Config:
    ov = {
        "page.size": a.page, "page.margins": a.margins, "page.columns": a.columns,
        "page.gutter": a.gutter, "page.facing_pages": True if a.facing else None,
        "page.page_count": a.pages, "css.media": a.media, "css.viewport_width": a.viewport,
        "images.embed": False if a.link_images else None,
        "fonts.package_fonts": False if a.no_font_package else None,
        "images.download_remote": False if a.no_remote else None,
        "idml.color_space": "CMYK" if a.cmyk else None, "idml.dom_version": a.dom_version,
        "output.zip_package": True if a.zip else None,
    }
    cfg = Config.load(a.config, **ov)
    if a.css:
        cfg["css"]["extra_stylesheets"] = list(cfg["css"].get("extra_stylesheets") or []) + a.css
    return cfg


def _print_report(rep, quiet):
    if quiet:
        return
    st = rep["stats"]
    print(f"  pages ~{st['pages']}  paragraphs {st['paragraphs']}  tables {st['tables']}  "
          f"images {st['images']}  links {st['hyperlinks']}")
    print(f"  styles: {rep['paragraph_styles']} paragraph, {rep['character_styles']} character, "
          f"{rep['colors']} colours")
    for k, v in rep["outputs"].items():
        print(f"  {k:8s} {v}")
    if rep["warnings"]:
        print(f"  {len(rep['warnings'])} warning(s):")
        for w in rep["warnings"][:25]:
            print(f"    - {w}")
        if len(rep["warnings"]) > 25:
            print("    ... see report.json")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in ("convert", "extract", "build", "validate", "serve", "-h", "--help"):
        argv.insert(0, "convert")
    ap = argparse.ArgumentParser(prog="html2idml", description="HTML + CSS + assets -> InDesign IDML")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("convert", help="HTML -> IDML")
    p.add_argument("input")
    p.add_argument("-o", "--output", help="output .idml (default: <input>.idml in ./<name>_idml/)")
    p.add_argument("--save-ir", action="store_true", help="also write the editable IR json")
    _common(p)

    p = sub.add_parser("extract", help="HTML -> editable IR json")
    p.add_argument("input")
    p.add_argument("-o", "--output", required=True, help="output .json")
    _common(p)

    p = sub.add_parser("build", help="IR json -> IDML")
    p.add_argument("input")
    p.add_argument("-o", "--output", required=True)
    _common(p)

    p = sub.add_parser("validate", help="check an .idml package")
    p.add_argument("input")

    p = sub.add_parser("serve", help="start the web interface")
    p.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to share on your network")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--workdir", default="html2idml-work", help="where jobs are stored")
    p.add_argument("--workers", type=int, default=2, help="parallel conversions")
    p.add_argument("--no-browser", action="store_true")

    a = ap.parse_args(argv)
    if a.cmd == "serve":
        from .web import serve
        serve(a.host, a.port, a.workdir, a.workers, open_browser=not a.no_browser)
        return 0
    log = (lambda *x: None) if getattr(a, "quiet", False) else print

    if a.cmd == "validate":
        res = validate_idml(a.input)
        for i in res["info"]:
            print(i)
        for e in res["errors"]:
            print("ERROR:", e)
        print("OK" if not res["errors"] else f"{len(res['errors'])} problem(s)")
        return 0 if not res["errors"] else 1

    cfg = _cfg(a)
    if a.cmd == "convert":
        src = a.input
        out = Path(a.output) if a.output else Path.cwd() / f"{Path(src).stem}_idml" / f"{Path(src).stem}.idml"
        if a.save_ir:
            cfg["output"]["write_ir"] = True
        log(f"html2idml: {src} -> {out}")
        rep = convert(src, out, cfg, plugins=a.plugin)
        _print_report(rep, a.quiet)
        return 0
    if a.cmd == "extract":
        out = Path(a.output)
        doc = html_to_ir(a.input, out.parent, cfg, plugins=a.plugin)
        doc.to_json(out)
        log(f"IR written: {out}  ({len(doc.paragraph_styles)} paragraph styles, "
            f"{sum(1 for _ in doc.iter_paragraphs())} paragraphs, {len(doc.assets)} assets)")
        for w in doc.warnings:
            log("  -", w)
        return 0
    if a.cmd == "build":
        doc = Document.from_json(a.input)
        if a.pages:
            doc.page.page_count = a.pages
        rep = ir_to_idml(doc, a.output, cfg)
        _print_report(rep, a.quiet)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
