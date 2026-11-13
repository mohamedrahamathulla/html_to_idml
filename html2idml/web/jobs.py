"""Conversion jobs: storage, background queue and progress tracking for the web UI.

Layout on disk (everything survives a server restart):

    <workdir>/jobs/<job_id>/job.json     status, progress, log, results
    <workdir>/jobs/<job_id>/input/       uploaded files / extracted zip
    <workdir>/jobs/<job_id>/output/      .idml, Links/, Document fonts/, package zip, report, IR
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
import traceback
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

import yaml

from ..config import Config, deep_merge
from ..ir import Document
from ..pipeline import convert, ir_to_idml

HTML_EXT = (".html", ".htm", ".xhtml")


def now() -> float:
    return round(time.time(), 3)


def safe_rel(path: str) -> Optional[PurePosixPath]:
    """Normalise an uploaded relative path; None if it tries to escape the folder."""
    p = PurePosixPath(str(path).replace("\\", "/").lstrip("/"))
    parts = [x for x in p.parts if x not in ("", ".")]
    if not parts or any(x == ".." for x in parts) or re.match(r"^[a-zA-Z]:", parts[0]):
        return None
    return PurePosixPath(*parts)


def extract_zip(zpath: Path, dest: Path, max_bytes: int = 2 * 1024 ** 3) -> int:
    total = 0
    count = 0
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            rel = safe_rel(info.filename)
            if rel is None or rel.parts[0] == "__MACOSX":
                continue
            total += info.file_size
            if total > max_bytes:
                raise ValueError("zip is too large once extracted")
            target = dest.joinpath(*rel.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            count += 1
    return count


def find_html(folder: Path) -> List[str]:
    files = [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in HTML_EXT]
    files.sort(key=lambda p: (len(p.relative_to(folder).parts),
                              0 if p.stem.lower() in ("index", "default", "main") else 1,
                              str(p).lower()))
    return [p.relative_to(folder).as_posix() for p in files]


def _lines_map(text: str) -> Dict[str, str]:
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("=>", "->", "=", ":"):
            if sep in line:
                k, v = line.split(sep, 1)
                if k.strip() and v.strip():
                    out[k.strip().strip("'\"")] = v.strip().strip("'\"")
                break
    return out


def options_to_config(o: Dict[str, Any], source_kind: str = "files") -> Config:
    """Turn the web form options into a converter Config."""
    base: Dict[str, Any] = {}
    adv = (o.get("advanced_yaml") or "").strip()
    if adv:
        loaded = yaml.safe_load(adv)
        if isinstance(loaded, dict):
            base = loaded
    page: Dict[str, Any] = {}
    if o.get("page_size") and o["page_size"] != "css":
        page["size"] = o["page_size"] if o["page_size"] != "custom" else None
        if o["page_size"] == "custom":
            page["width"], page["height"] = o.get("page_width"), o.get("page_height")
    if o.get("orientation"):
        page["orientation"] = o["orientation"]
    if (o.get("margins") or "").strip():
        page["margins"] = o["margins"].strip()
    if o.get("columns"):
        page["columns"] = int(o["columns"])
    if (o.get("gutter") or "").strip():
        page["gutter"] = o["gutter"].strip()
    page["facing_pages"] = bool(o.get("facing_pages"))
    if o.get("page_count"):
        page["page_count"] = int(o["page_count"])
    form = {
        "page": page,
        "css": {"media": o.get("media") or "screen",
                "viewport_width": int(o.get("viewport_width") or 1280),
                "user_css": o.get("user_css") or "",
                "ignore_body_margin": o.get("ignore_body_margin", True)},
        "fonts": {"map": _lines_map(o.get("font_map", "")),
                  "weight_mode": o.get("weight_mode") or "simple",
                  "check_installed": bool(o.get("check_installed", True)),
                  "package_fonts": bool(o.get("package_fonts", True))},
        "styles": {"names": _lines_map(o.get("style_names", "")),
                   "char_names": _lines_map(o.get("char_style_names", "")),
                   "hyphenation": bool(o.get("hyphenation", False))},
        "images": {"embed": bool(o.get("embed_images", True)),
                   "download_remote": bool(o.get("download_remote", True)) or source_kind == "url",
                   "inline_svg": bool(o.get("inline_svg", True))},
        "links": {"hyperlinks": bool(o.get("hyperlinks", True))},
        "idml": {"dom_version": str(o.get("dom_version") or "16.0"),
                 "color_space": o.get("color_space") or "RGB",
                 "ruler_units": o.get("ruler_units") or "Points"},
        "output": {"write_ir": True, "write_jsx": True, "write_report": True,
                   "zip_package": bool(o.get("zip_package", True))},
    }
    # empty maps must not wipe maps given in the advanced YAML
    for sect, key in (("fonts", "map"), ("styles", "names"), ("styles", "char_names")):
        if not form[sect][key]:
            del form[sect][key]
    return Config.load(deep_merge(base, form))


class JobManager:
    def __init__(self, workdir: Path, workers: int = 2):
        self.root = Path(workdir) / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="html2idml")
        self._load()

    # ------------------------------------------------------------------ storage
    def _load(self):
        for jf in self.root.glob("*/job.json"):
            try:
                job = json.loads(jf.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if job.get("status") in ("queued", "running"):
                job["status"] = "failed"
                job["error"] = "interrupted (server was restarted)"
            self.jobs[job["id"]] = job

    def dir(self, job_id: str) -> Path:
        return self.root / job_id

    def save(self, job: Dict[str, Any]):
        job["updated"] = now()
        d = self.dir(job["id"])
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "job.json.tmp"
        tmp.write_text(json.dumps(job, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(d / "job.json")

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        return self.jobs.get(job_id)

    def list(self) -> List[Dict[str, Any]]:
        with self.lock:
            jobs = sorted(self.jobs.values(), key=lambda j: j.get("created", 0), reverse=True)
            return [{k: j.get(k) for k in ("id", "name", "status", "stage", "progress", "created",
                                           "updated", "error", "version")} |
                    {"entries": len(j.get("entries", [])),
                     "pages": sum((r.get("stats") or {}).get("pages", 0) for r in j.get("results", []))}
                    for j in jobs]

    def delete(self, job_id: str) -> bool:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job.get("status") in ("queued", "running"):
                return False
            self.jobs.pop(job_id)
        shutil.rmtree(self.dir(job_id), ignore_errors=True)
        return True

    # ------------------------------------------------------------------ creation
    def create(self, name: str, options: Dict[str, Any], source: Dict[str, Any]) -> Dict[str, Any]:
        job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        job = {"id": job_id, "name": name or "conversion", "created": now(), "updated": now(),
               "status": "new", "stage": "Uploading", "progress": 0.0, "log": [], "warnings": [],
               "error": None, "options": options, "source": source, "entries": [], "results": [],
               "version": 1}
        (self.dir(job_id) / "input").mkdir(parents=True, exist_ok=True)
        (self.dir(job_id) / "output").mkdir(parents=True, exist_ok=True)
        with self.lock:
            self.jobs[job_id] = job
        self.save(job)
        return job

    def log(self, job, msg: str, level: str = "info"):
        with self.lock:
            job["log"].append({"t": now(), "msg": msg, "level": level})
            if len(job["log"]) > 2000:
                job["log"] = job["log"][-2000:]

    def submit(self, job):
        job["status"] = "queued"
        job["stage"] = "Waiting for a worker"
        self.log(job, "Queued")
        self.save(job)
        self.pool.submit(self._run, job["id"])

    # ------------------------------------------------------------------ work
    def _run(self, job_id: str):
        job = self.jobs[job_id]
        d = self.dir(job_id)
        out_root = d / "output"
        job.update(status="running", progress=0.0, error=None, warnings=[], results=[])
        self.log(job, "Started")
        self.save(job)
        t0 = time.time()
        try:
            cfg = options_to_config(job["options"], job["source"].get("kind", "files"))
            cfg["images"]["url_map"] = dict(cfg["images"].get("url_map") or {},
                                            **{u: str(d / "input" / rel)
                                               for u, rel in (job.get("url_map") or {}).items()})
            entries = job["entries"]
            n = max(1, len(entries))
            last_stage = [None]
            for i, entry in enumerate(entries):
                is_url = job["source"].get("kind") == "url"
                stem = _stem(entry, is_url)
                out_dir = out_root if n == 1 else out_root / stem
                src = entry if is_url else str(d / "input" / entry)
                label = entry if n > 1 else ""

                def prog(stage, frac, i=i, label=label):
                    st = f"{label}: {stage}" if label else stage
                    job["stage"] = st
                    job["progress"] = round((i + frac) / n, 4)
                    if st != last_stage[0]:
                        last_stage[0] = st
                        self.log(job, st)
                        self.save(job)

                def warn(msg, label=label):
                    job["warnings"].append(f"{label}: {msg}" if label else msg)

                t1 = time.time()
                report = convert(src, out_dir / f"{stem}.idml", cfg, on_warning=warn, on_progress=prog)
                job["results"].append(self._result(entry, stem, out_root, report, time.time() - t1))
            if n > 1:
                self._zip_all(job)
            job["missing_urls"] = missing_urls(job["warnings"])
            job.update(status="done", stage="Done", progress=1.0)
            self.log(job, f"Finished in {time.time() - t0:.1f}s")
        except Exception as e:  # noqa: BLE001 - reported to the UI
            job.update(status="failed", stage="Failed", error=f"{type(e).__name__}: {e}")
            self.log(job, traceback.format_exc(), "error")
        self.save(job)

    @staticmethod
    def _result(entry, stem, out_root: Path, report, seconds) -> Dict[str, Any]:
        outputs = {}
        for k, v in report["outputs"].items():
            try:
                outputs[k] = Path(v).resolve().relative_to(out_root.resolve()).as_posix()
            except ValueError:
                pass
        return {"entry": entry, "name": stem, "status": "done", "outputs": outputs,
                "stats": report["stats"], "paragraph_styles": report["paragraph_styles"],
                "character_styles": report["character_styles"], "colors": report["colors"],
                "assets": len(report["assets"]), "fonts_used": report.get("fonts_used", {}),
                "warnings": len(report["warnings"]), "seconds": round(seconds, 2)}

    def _zip_all(self, job):
        out_root = self.dir(job["id"]) / "output"
        zp = out_root / "all-outputs.zip"
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
            for p in out_root.rglob("*"):
                if p.is_file() and p != zp and not p.name.endswith("-package.zip"):
                    z.write(p, p.relative_to(out_root).as_posix())
        job["all_zip"] = "all-outputs.zip"

    # ------------------------------------------------------------------ IR editing + rebuild
    def ir_path(self, job, idx: int) -> Path:
        res = job["results"][idx]
        return self.dir(job["id"]) / "output" / res["outputs"]["ir"]

    def rebuild(self, job_id: str, idx: int, changes: Dict[str, Any]):
        job = self.jobs[job_id]
        job.update(status="queued", stage="Waiting to rebuild", progress=0.0)
        self.save(job)
        self.pool.submit(self._rebuild, job_id, idx, changes)

    def _rebuild(self, job_id: str, idx: int, changes: Dict[str, Any]):
        job = self.jobs[job_id]
        d = self.dir(job_id)
        try:
            job.update(status="running", stage="Applying style edits", progress=0.1)
            self.log(job, "Rebuild: applying style edits")
            self.save(job)
            cfg = options_to_config(job["options"], job["source"].get("kind", "files"))
            irp = self.ir_path(job, idx)
            doc = Document.from_json(irp)
            summary = apply_changes(doc, changes, cfg["idml"]["color_space"])
            for line in summary:
                self.log(job, line)
            res = job["results"][idx]
            idml = d / "output" / res["outputs"]["idml"]

            def prog(stage, frac):
                job["stage"] = f"Rebuild: {stage}"
                job["progress"] = round(0.1 + 0.9 * frac, 4)

            report = ir_to_idml(doc, idml, cfg, on_progress=prog)
            job["results"][idx] = self._result(res["entry"], res["name"], d / "output", report,
                                               res.get("seconds", 0))
            if len(job["results"]) > 1:
                self._zip_all(job)
            job["version"] = int(job.get("version", 1)) + 1
            job.update(status="done", stage="Done", progress=1.0)
            self.log(job, f"Rebuilt as version {job['version']}")
        except Exception as e:  # noqa: BLE001
            job.update(status="failed", stage="Rebuild failed", error=f"{type(e).__name__}: {e}")
            self.log(job, traceback.format_exc(), "error")
        self.save(job)


_MISSING_RE = re.compile(r"(?:could not download|remote file skipped[^:]*:|file not found:)\s*(\S+?)(?::\s|$)")


def missing_urls(warnings: List[str]) -> List[Dict[str, str]]:
    """Remote/local images that could not be loaded, with the reason, for the UI."""
    out, seen = [], set()
    for w in warnings:
        msg = w.split(": ", 1)[1] if re.match(r"^[^:/]+\.x?html?: ", w) else w
        m = _MISSING_RE.search(msg)
        if not m:
            continue
        url = m.group(1)
        if url in seen or not re.search(r"\.(jpe?g|png|gif|webp|avif|svg|tiff?|bmp)(\?|$)|^https?://", url, re.I):
            continue
        seen.add(url)
        reason = msg[m.end():].strip() or ("download switched off" if "skipped" in msg else "not found")
        out.append({"url": url, "reason": reason})
    return out


def _stem(entry: str, is_url: bool) -> str:
    if is_url:
        tail = re.sub(r"^[a-z]+://", "", entry).rstrip("/").split("/")[-1] or "page"
        tail = tail.split("?")[0]
        stem = Path(tail).stem or "page"
    else:
        stem = Path(entry).stem
    return re.sub(r"[^\w\-]+", "_", stem).strip("_") or "document"


# ---------------------------------------------------------------------------- IR helpers
def color_hex(doc: Document, name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    if name == "Black":
        return "#000000"
    if name == "Paper":
        return "#ffffff"
    c = doc.colors.get(name)
    if not c:
        return None
    if c.space.upper() == "CMYK":
        cc, m, y, k = [v / 100 for v in c.values]
        rgb = [255 * (1 - x) * (1 - k) for x in (cc, m, y)]
    else:
        rgb = c.values[:3]
    return "#" + "".join(f"{int(round(max(0, min(255, v)))):02x}" for v in rgb)


def ir_summary(doc: Document, limit: int = 3000) -> Dict[str, Any]:
    def style_rows(styles, para):
        rows = []
        for name, st in styles.items():
            eff = doc.resolve_paragraph_style(name) if para else dict(st.props)
            rows.append({"name": name, "based_on": st.based_on, "source": st.source,
                         "props": st.props, "effective": eff,
                         "color_hex": color_hex(doc, eff.get("color")),
                         "uses": 0})
        return rows

    pstyles = style_rows(doc.paragraph_styles, True)
    cstyles = style_rows(doc.character_styles, False)
    puse: Dict[str, int] = {}
    cuse: Dict[str, int] = {}
    outline = []
    for b in doc.iter_blocks():
        puse[b.style] = puse.get(b.style, 0) + 1
        if getattr(b, "type", "") == "table":
            if len(outline) < limit:
                outline.append({"kind": "table", "style": b.style,
                                "text": f"Table {len(b.rows)} rows x {len(b.column_widths)} columns"})
            continue
        for r in b.runs:
            if r.char_style:
                cuse[r.char_style] = cuse.get(r.char_style, 0) + 1
        if len(outline) < limit:
            txt = b.plain_text().replace(" ", " / ")
            imgs = sum(1 for r in b.runs if r.type == "image")
            outline.append({"kind": "paragraph", "style": b.style,
                            "text": (txt[:160] + ("…" if len(txt) > 160 else "")) or
                            (f"[{imgs} image(s)]" if imgs else "[empty]"),
                            "overrides": sorted(b.overrides.keys()),
                            "chars": sorted({r.char_style for r in b.runs if r.char_style})})
    for row in pstyles:
        row["uses"] = puse.get(row["name"], 0)
    for row in cstyles:
        row["uses"] = cuse.get(row["name"], 0)
    return {
        "page": doc.page.__dict__,
        "paragraph_styles": pstyles,
        "character_styles": cstyles,
        "colors": [{"name": n, "space": c.space, "values": c.values, "hex": color_hex(doc, n)}
                   for n, c in doc.colors.items()] +
                  [{"name": "Black", "space": "CMYK", "values": [0, 0, 0, 100], "hex": "#000000"},
                   {"name": "Paper", "space": "CMYK", "values": [0, 0, 0, 0], "hex": "#ffffff"}],
        "assets": [{"id": a.id, "kind": a.kind, "file": Path(a.path).name, "path": a.path,
                    "source": a.source,
                    "format": a.format, "size": [a.width_px, a.height_px], "ppi": a.ppi,
                    "family": a.family} for a in doc.assets.values()],
        "outline": outline,
        "outline_total": sum(1 for _ in doc.iter_blocks()),
        "metadata": doc.metadata,
    }


NUM_PROPS = {"font_size", "tracking", "space_before", "space_after", "left_indent", "right_indent",
             "first_line_indent"}
INT_PROPS = {"keep_with_next", "list_level", "numbering_start"}
BOOL_PROPS = {"underline", "strike", "no_break", "keep_together", "hyphenation",
              "numbering_continue"}


def _coerce(doc: Document, key: str, value, space: str):
    if key in ("color", "highlight", "shading"):
        if value in (None, "", "none"):
            return None if key != "color" else "Black"
        if isinstance(value, str) and value.startswith("#") and len(value) == 7:
            rgb = tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))
            return doc.add_color(rgb, space)
        return value
    if key == "leading":
        if value in ("auto", "Auto", "", None):
            return "auto"
        return float(value)
    if key in NUM_PROPS:
        return float(value) if value not in ("", None) else 0.0
    if key in INT_PROPS:
        return int(value) if value not in ("", None) else None
    if key in BOOL_PROPS:
        return bool(value)
    return value


def _rename(doc: Document, kind: str, old: str, new: str):
    styles = doc.paragraph_styles if kind == "paragraph" else doc.character_styles
    if old not in styles or not new or new == old or new in styles:
        return False
    st = styles.pop(old)
    st.name = new
    styles[new] = st
    if kind == "paragraph":
        for s in doc.paragraph_styles.values():
            if s.based_on == old:
                s.based_on = new
        for b in doc.iter_blocks():
            if b.style == old:
                b.style = new
    else:
        for p in doc.iter_paragraphs():
            for r in p.runs:
                if r.char_style == old:
                    r.char_style = new
    return True


def apply_changes(doc: Document, changes: Dict[str, Any], space: str = "RGB") -> List[str]:
    """changes = {"paragraph_styles": {name: {"props": {...}, "rename": str, "based_on": str}},
                  "character_styles": {...}, "page": {...}}"""
    log = []
    for kind, key in (("paragraph", "paragraph_styles"), ("character", "character_styles")):
        styles = doc.paragraph_styles if kind == "paragraph" else doc.character_styles
        for name, ch in (changes.get(key) or {}).items():
            st = styles.get(name)
            if st is None:
                continue
            props = ch.get("props") or {}
            for k, v in props.items():
                st.props[k] = _coerce(doc, k, v, space)
            if props:
                log.append(f"{kind} style '{name}': {', '.join(sorted(props))}")
            if kind == "paragraph" and "based_on" in ch and ch["based_on"] != st.based_on:
                if ch["based_on"] in (None, "") or ch["based_on"] in styles:
                    st.based_on = ch["based_on"] or None
            if ch.get("rename") and _rename(doc, kind, name, ch["rename"].strip()):
                log.append(f"{kind} style '{name}' renamed to '{ch['rename'].strip()}'")
    page = changes.get("page") or {}
    for k, v in page.items():
        if hasattr(doc.page, k) and v not in ("", None):
            cur = getattr(doc.page, k)
            if isinstance(cur, bool) or k == "facing_pages":
                setattr(doc.page, k, bool(v))
            elif k in ("columns",):
                setattr(doc.page, k, max(1, int(v)))
            elif k == "page_count":
                setattr(doc.page, k, int(v) if int(v) > 0 else None)
            else:
                setattr(doc.page, k, float(v))
            log.append(f"page {k} = {v}")
    if "page" in changes and changes["page"].get("page_count") in ("", 0, "0"):
        doc.page.page_count = None
    return log
