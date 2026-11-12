"""Regression tests.  Run with:  python tests/test_html2idml.py   (or pytest)"""
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from html2idml import Config, Document, html_to_ir, ir_to_idml, validate_idml  # noqa: E402
from html2idml.ir import Table, TextRun  # noqa: E402

CFG = Config.load({"fonts": {"check_installed": False}})


def build(html, cfg=CFG):
    tmp = Path(tempfile.mkdtemp())
    src = tmp / "t.html"
    src.write_text(html, encoding="utf-8")
    doc = html_to_ir(src, tmp, cfg)
    rep = ir_to_idml(doc, tmp / "t.idml", cfg)
    res = validate_idml(tmp / "t.idml")
    assert not res["errors"], res["errors"]
    return doc, rep, tmp / "t.idml"


def paras(doc):
    return list(doc.iter_paragraphs())


def test_whitespace_and_breaks():
    doc, _, _ = build("<p>  Hello   <b> world </b>\n  again<br></p><p>a<br>b</p><div>   </div>")
    ps = paras(doc)
    assert [p.plain_text() for p in ps] == ["Hello world again", "a b"]


def test_cascade_specificity_important_and_vars():
    doc, _, _ = build("""<style>:root{--c:#ff0000} p{color:blue} .a{color:var(--c)}
      #x{color:green} .b{color:purple !important}</style>
      <p class="a">1</p><p class="a" id="x">2</p><p id="x" class="b" style="color:black">3</p>""")
    resolved = [dict(doc.resolve_paragraph_style(p.style), **p.overrides)["color"] for p in paras(doc)]
    assert resolved == ["R=255 G=0 B=0", "R=0 G=128 B=0", "R=128 G=0 B=128"]


def test_inline_styles_become_character_styles():
    doc, _, _ = build("<p>a <strong>b</strong> <em>c</em> <strong><em>d</em></strong> x<sup>2</sup></p>")
    p = paras(doc)[0]
    names = [r.char_style for r in p.runs if isinstance(r, TextRun)]
    assert "strong" in names and "em" in names and "strong em" in names and "sup" in names
    assert doc.character_styles["strong em"].props["font_style"] == "Bold Italic"
    assert doc.character_styles["sup"].props["position"] == "superscript"


def test_lists_numbering():
    doc, _, _ = build("<ol start='5'><li>a</li><li>b</li></ol><ul><li>x<ul><li>y</li></ul></li></ul>")
    ps = paras(doc)
    eff = [dict(doc.resolve_paragraph_style(p.style), **p.overrides) for p in ps]
    assert eff[0]["list_type"] == "numbered" and eff[0]["numbering_start"] == 5
    assert eff[0]["numbering_continue"] is False and eff[1]["numbering_continue"] is True
    assert eff[2]["list_type"] == "bullet" and eff[3]["list_level"] == 2
    assert eff[3]["left_indent"] > eff[2]["left_indent"]


def test_table_spans_header_and_nested():
    doc, rep, path = build("""<table border=1><tr><th>A</th><th>B</th></tr>
      <tr><td rowspan=2>x</td><td>y</td></tr><tr><td><table><tr><td>in</td></tr></table></td></tr></table>""")
    t = doc.stories[0].blocks[0]
    assert isinstance(t, Table)
    assert t.rows[0].kind == "header"
    assert t.rows[1].cells[0].row_span == 2
    assert rep["stats"]["tables"] == 2
    story = [n for n in zipfile.ZipFile(path).namelist() if n.startswith("Stories/")][0]
    xml = zipfile.ZipFile(path).read(story).decode()
    assert 'Name="1:2"' in xml  # column 1, row 2 (the cell after the row-spanned one)


def test_images_links_and_page_rule():
    from PIL import Image
    tmp = Path(tempfile.mkdtemp())
    Image.new("RGB", (400, 200), "red").save(tmp / "a.png")
    (tmp / "t.html").write_text("""<style>@page{size:A5 landscape;margin:10mm}</style>
      <p><img src="a.png" style="width:100px"> <a href="https://x.org">x</a></p>""", encoding="utf-8")
    doc = html_to_ir(tmp / "t.html", tmp / "out", CFG)
    rep = ir_to_idml(doc, tmp / "out" / "t.idml", CFG)
    assert not validate_idml(tmp / "out" / "t.idml")["errors"]
    assert doc.page.width > doc.page.height
    assert abs(doc.page.margin_top - 28.346) < 0.01
    img = [r for r in paras(doc)[0].runs if r.type == "image"][0]
    assert img.width == 75.0 and img.height == 37.5
    assert (tmp / "out" / "Links" / "a.png").exists()
    assert rep["stats"]["hyperlinks"] == 1


def test_ir_json_roundtrip_and_edit():
    doc, _, _ = build("<h1>Title</h1><p>Body</p>")
    doc2 = Document.from_json_text(doc.to_json()) if hasattr(Document, "from_json_text") else \
        Document.from_dict(__import__("json").loads(doc.to_json()))
    doc2.paragraph_styles["h1"].props["font_size"] = 40
    tmp = Path(tempfile.mkdtemp())
    ir_to_idml(doc2, tmp / "e.idml", CFG)
    styles = zipfile.ZipFile(tmp / "e.idml").read("Resources/Styles.xml").decode()
    assert 'Name="h1"' in styles and 'PointSize="40"' in styles


def test_config_style_names_and_overrides():
    cfg = Config.load({"fonts": {"check_installed": False},
                       "styles": {"names": {"h1": "Heading 1", "p": "Body Text"},
                                  "overrides": {"Heading 1": {"font_family": "Minion Pro"}}}})
    doc, _, _ = build("<h1>T</h1><p>b</p>", cfg)
    assert "Heading 1" in doc.paragraph_styles and "Body Text" in doc.paragraph_styles
    assert doc.paragraph_styles["Heading 1"].props["font_family"] == "Minion Pro"


def test_empty_and_hidden():
    doc, rep, _ = build("<body><div style='display:none'>x</div><script>var a</script></body>")
    assert paras(doc) == [] and rep["stats"]["pages"] == 1


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
