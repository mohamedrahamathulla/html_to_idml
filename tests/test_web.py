"""Web UI API tests (Flask test client).  Run with:  python tests/test_web.py   (or pytest)"""
import io
import json
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from html2idml.web import create_app  # noqa: E402

OPTS = json.dumps({"check_installed": False, "package_fonts": False})


def client():
    app = create_app(Path(tempfile.mkdtemp()), workers=1)
    return app.test_client(), app.config["JOBS"]


def wait(c, jid, timeout=60):
    t = time.time()
    while time.time() - t < timeout:
        j = c.get(f"/api/jobs/{jid}").get_json()
        if j["status"] in ("done", "failed"):
            return j
        time.sleep(0.2)
    raise TimeoutError(jid)


def zbytes(entries):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, d in entries:
            z.writestr(n, d)
    b.seek(0)
    return b


def test_paste_convert_edit_download():
    c, _ = client()
    r = c.post("/api/jobs", data={"kind": "paste", "options": OPTS,
                                  "html": "<h1>T</h1><p>Hi <b>there</b></p>"})
    assert r.status_code == 201
    j = wait(c, r.get_json()["id"])
    assert j["status"] == "done", j.get("error")
    res = j["results"][0]
    assert {"idml", "ir", "report", "jsx", "package"} <= set(res["outputs"])
    ir = c.get(f"/api/jobs/{j['id']}/results/0/ir").get_json()
    assert any(s["name"] == "h1" for s in ir["paragraph_styles"])
    r = c.post(f"/api/jobs/{j['id']}/results/0/edit",
               json={"paragraph_styles": {"h1": {"props": {"font_size": 40, "color": "#ff0000"},
                                                 "rename": "Title"}}})
    assert r.status_code == 200
    j = wait(c, j["id"])
    assert j["status"] == "done" and j["version"] == 2
    d = c.get(f"/api/jobs/{j['id']}/download/{res['outputs']['idml']}")
    assert d.status_code == 200
    styles = zipfile.ZipFile(io.BytesIO(d.data)).read("Resources/Styles.xml").decode()
    assert 'Name="Title"' in styles and 'PointSize="40"' in styles and "R=255 G=0 B=0" in styles


def test_zip_multi_and_errors():
    c, _ = client()
    z = zbytes([("site/index.html", "<link rel=stylesheet href=s.css><p>a</p>"),
                ("site/s.css", "p{color:red}"), ("site/b.html", "<p>b</p>"),
                ("../evil.html", "x")])
    r = c.post("/api/jobs", data={"kind": "zip", "options": OPTS, "all_entries": "1",
                                  "files": (z, "site.zip")})
    assert r.status_code == 201
    j = wait(c, r.get_json()["id"])
    assert j["status"] == "done" and len(j["results"]) == 2 and j.get("all_zip")
    assert c.get(f"/api/jobs/{j['id']}/download-zip").status_code == 200
    assert c.get(f"/api/jobs/{j['id']}/download/..%2Fjob.json").status_code in (400, 404)
    r = c.post("/api/jobs", data={"kind": "zip", "options": OPTS,
                                  "files": (zbytes([("a.css", "p{}")]), "x.zip")})
    assert r.status_code == 400
    r = c.post("/api/jobs", data={"kind": "url", "options": OPTS})
    assert r.status_code == 400
    assert c.delete(f"/api/jobs/{j['id']}").status_code == 200
    assert c.get(f"/api/jobs/{j['id']}").status_code == 404


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                import traceback
                traceback.print_exc()
                print("FAIL", name, e)
    sys.exit(1 if fails else 0)
