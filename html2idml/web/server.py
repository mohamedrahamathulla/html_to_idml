"""Flask web app: upload -> convert -> track -> edit styles -> download.

    python -m html2idml serve                 # http://127.0.0.1:8765
    python -m html2idml serve --port 9000 --workdir D:/idml-jobs
"""
from __future__ import annotations

import io
import json
import mimetypes
import threading
import webbrowser
import zipfile
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file

from ..config import DEFAULTS, PAGE_SIZES
from ..fonts import installed_font_families
from ..ir import Document
from .jobs import JobManager, extract_zip, find_html, ir_summary, safe_rel

STATIC = Path(__file__).parent / "static"


def create_app(workdir: Path, workers: int = 2, max_upload_mb: int = 1024) -> Flask:
    app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = max_upload_mb * 1024 * 1024
    jobs = JobManager(Path(workdir), workers=workers)
    app.config["JOBS"] = jobs
    fonts_cache = {}

    def job_or_404(job_id):
        job = jobs.get(job_id)
        if job is None:
            abort(404, "job not found")
        return job

    def safe_file(root: Path, rel: str) -> Path:
        r = safe_rel(rel)
        if r is None:
            abort(400, "bad path")
        p = (root / Path(*r.parts)).resolve()
        if root.resolve() not in p.parents and p != root.resolve():
            abort(400, "bad path")
        if not p.is_file():
            abort(404, "file not found")
        return p

    @app.get("/")
    def index():
        return send_file(STATIC / "index.html")

    @app.get("/api/meta")
    def meta():
        if "fonts" not in fonts_cache:
            fonts_cache["fonts"] = sorted(installed_font_families(), key=str.lower)
        return jsonify({"page_sizes": sorted(PAGE_SIZES), "defaults": DEFAULTS,
                        "installed_fonts": fonts_cache["fonts"], "workdir": str(Path(workdir).resolve())})

    # ------------------------------------------------------------------ jobs
    @app.get("/api/jobs")
    def list_jobs():
        return jsonify(jobs.list())

    @app.post("/api/jobs")
    def create_job():
        try:
            options = json.loads(request.form.get("options") or "{}")
        except ValueError:
            abort(400, "options must be JSON")
        kind = request.form.get("kind") or "files"
        name = (request.form.get("name") or "").strip()
        url = (request.form.get("url") or "").strip()
        html = request.form.get("html") or ""
        uploads = request.files.getlist("files")
        if kind == "url" and not url:
            abort(400, "enter a URL")
        if kind == "paste" and not html.strip():
            abort(400, "paste some HTML")
        if kind in ("files", "folder", "zip") and not uploads:
            abort(400, "choose files to upload")

        job = jobs.create(name or (url or (uploads[0].filename if uploads else "pasted.html")),
                          options, {"kind": kind, "url": url or None})
        inp = jobs.dir(job["id"]) / "input"
        n_files = 0
        try:
            if kind == "url":
                job["entries"] = [url]
            elif kind == "paste":
                (inp / "index.html").write_text(html, encoding="utf-8")
                job["entries"] = ["index.html"]
                n_files = 1
            else:
                for f in uploads:
                    rel = safe_rel(f.filename or "")
                    if rel is None:
                        continue
                    target = inp.joinpath(*rel.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    f.save(target)
                    n_files += 1
                    if target.suffix.lower() == ".zip":
                        n_files += extract_zip(target, target.parent) - 1
                        target.unlink()
                found = find_html(inp)
                if not found:
                    raise ValueError("no .html file found in the upload")
                entry = (request.form.get("entry") or "").strip()
                if request.form.get("all_entries") == "1":
                    job["entries"] = found
                elif entry and entry in found:
                    job["entries"] = [entry]
                else:
                    job["entries"] = [found[0]]
                job["available_entries"] = found
        except Exception as e:  # noqa: BLE001
            job.update(status="failed", stage="Upload failed", error=str(e))
            jobs.save(job)
            return jsonify(job), 400
        jobs.log(job, f"Received {n_files} file(s); converting {len(job['entries'])} document(s)")
        if not name and job["entries"] and kind != "url":
            job["name"] = Path(job["entries"][0]).name if len(job["entries"]) == 1 else \
                f"{len(job['entries'])} documents"
        jobs.submit(job)
        return jsonify(job), 201

    @app.get("/api/jobs/<job_id>")
    def get_job(job_id):
        return jsonify(job_or_404(job_id))

    @app.delete("/api/jobs/<job_id>")
    def delete_job(job_id):
        job_or_404(job_id)
        if not jobs.delete(job_id):
            abort(409, "job is still running")
        return jsonify({"deleted": job_id})

    @app.post("/api/jobs/<job_id>/rerun")
    def rerun(job_id):
        job = job_or_404(job_id)
        if job["status"] in ("queued", "running"):
            abort(409, "job is still running")
        body = request.get_json(silent=True) or {}
        if "options" in body:
            job["options"] = body["options"]
        if body.get("entries"):
            avail = job.get("available_entries") or job["entries"]
            job["entries"] = [e for e in body["entries"] if e in avail] or job["entries"]
        job["version"] = int(job.get("version", 1)) + 1
        jobs.log(job, f"Re-run requested (version {job['version']})")
        jobs.submit(job)
        return jsonify(job)

    # ------------------------------------------------------------------ files
    @app.get("/api/jobs/<job_id>/download/<path:rel>")
    def download(job_id, rel):
        job_or_404(job_id)
        p = safe_file(jobs.dir(job_id) / "output", rel)
        return send_file(p, as_attachment=True, download_name=p.name)

    @app.get("/api/jobs/<job_id>/download-zip")
    def download_zip(job_id):
        job = job_or_404(job_id)
        out = jobs.dir(job_id) / "output"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in out.rglob("*"):
                if p.is_file() and not p.name.endswith(".zip"):
                    z.write(p, p.relative_to(out).as_posix())
        buf.seek(0)
        return send_file(buf, as_attachment=True, mimetype="application/zip",
                         download_name=f"{job['results'][0]['name'] if job.get('results') else job_id}-idml.zip")

    @app.get("/api/jobs/<job_id>/files")
    def list_files(job_id):
        job_or_404(job_id)
        out = jobs.dir(job_id) / "output"
        files = [{"path": p.relative_to(out).as_posix(), "size": p.stat().st_size}
                 for p in sorted(out.rglob("*")) if p.is_file()]
        return jsonify(files)

    @app.get("/api/jobs/<job_id>/source/<path:rel>")
    def source(job_id, rel):
        job_or_404(job_id)
        p = safe_file(jobs.dir(job_id) / "input", rel)
        mt = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        resp = send_file(p, mimetype=mt)
        # the original page is shown sandboxed: no scripts, same layout
        resp.headers["Content-Security-Policy"] = "script-src 'none'; object-src 'none'"
        return resp

    @app.get("/api/jobs/<job_id>/asset/<int:idx>/<path:rel>")
    def asset(job_id, idx, rel):
        job_or_404(job_id)
        p = safe_file(jobs.dir(job_id) / "output", rel)
        return send_file(p, mimetype=mimetypes.guess_type(p.name)[0] or "application/octet-stream")

    # ------------------------------------------------------------------ IR / styles
    @app.get("/api/jobs/<job_id>/results/<int:idx>/ir")
    def get_ir(job_id, idx):
        job = job_or_404(job_id)
        if idx >= len(job.get("results", [])):
            abort(404, "no such result")
        doc = Document.from_json(jobs.ir_path(job, idx))
        summary = ir_summary(doc)
        out = (jobs.dir(job_id) / "output").resolve()
        for a in summary["assets"]:
            try:
                a["rel"] = Path(a.pop("path")).resolve().relative_to(out).as_posix()
            except ValueError:
                a["rel"] = None
        return jsonify(summary)

    @app.post("/api/jobs/<job_id>/results/<int:idx>/edit")
    def edit(job_id, idx):
        job = job_or_404(job_id)
        if job["status"] in ("queued", "running"):
            abort(409, "job is still running")
        if idx >= len(job.get("results", [])):
            abort(404, "no such result")
        changes = request.get_json(silent=True) or {}
        jobs.rebuild(job_id, idx, changes)
        return jsonify({"ok": True})

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(409)
    @app.errorhandler(413)
    def err(e):
        return jsonify({"error": getattr(e, "description", str(e))}), getattr(e, "code", 500)

    return app


def serve(host: str = "127.0.0.1", port: int = 8765, workdir: str = "html2idml-work",
          workers: int = 2, open_browser: bool = True):
    app = create_app(Path(workdir), workers=workers)
    url = f"http://{host if host not in ('0.0.0.0', '::') else '127.0.0.1'}:{port}/"
    print(f"html2idml web UI: {url}   (jobs stored in {Path(workdir).resolve()})")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=host, port=port, threaded=True, debug=False, use_reloader=False)
