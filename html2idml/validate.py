"""Structural validator for .idml packages (well-formedness + cross references + links)."""
from __future__ import annotations

import re
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, List

from lxml import etree

REF_ATTRS = {
    "AppliedParagraphStyle", "AppliedCharacterStyle", "FillColor", "StrokeColor", "ParentStory",
    "NextTextFrame", "PreviousTextFrame", "AppliedMaster", "Source", "ActiveLayer", "ItemLayer",
    "AppliedCellStyle", "AppliedTableStyle", "AppliedObjectStyle", "RuleAboveColor",
    "RuleBelowColor", "ParagraphShadingColor", "UnderlineColor", "NextStyle", "PageStart",
    "TopEdgeStrokeColor", "BottomEdgeStrokeColor", "LeftEdgeStrokeColor", "RightEdgeStrokeColor",
    "TopBorderStrokeColor", "BottomBorderStrokeColor", "LeftBorderStrokeColor",
    "RightBorderStrokeColor",
}
BUILTIN_OK = {"n", "Text Color", ""}


def validate_idml(path) -> Dict[str, List[str]]:
    errors: List[str] = []
    info: List[str] = []
    path = Path(path)
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        first = z.infolist()[0]
        if first.filename != "mimetype" or first.compress_type != zipfile.ZIP_STORED:
            errors.append("mimetype must be the first, uncompressed entry")
        if z.read("mimetype") != b"application/vnd.adobe.indesign-idml-package":
            errors.append("wrong mimetype content")
        trees = {}
        for n in names:
            if n.endswith(".xml"):
                try:
                    trees[n] = etree.fromstring(z.read(n))
                except etree.XMLSyntaxError as e:
                    errors.append(f"{n}: XML error {e}")
        dm = trees.get("designmap.xml")
        if dm is None:
            errors.append("designmap.xml missing")
            return {"errors": errors, "info": info}
        for el in dm.iter():
            src = el.get("src")
            if src and src not in names:
                errors.append(f"designmap references missing part {src}")
        selfs = set()
        for t in trees.values():
            for el in t.iter():
                s = el.get("Self")
                if s:
                    if s in selfs and not el.tag.endswith(("Row", "Column")):
                        errors.append(f"duplicate Self id {s}")
                    selfs.add(s)
        for n, t in trees.items():
            for el in t.iter():
                for a in REF_ATTRS:
                    v = el.get(a)
                    if v is None or v in BUILTIN_OK:
                        continue
                    if v not in selfs:
                        errors.append(f"{n}: <{etree.QName(el).localname} {a}='{v}'> points to nothing")
                tag = etree.QName(el).localname if isinstance(el.tag, str) else ""
                if tag in ("BasedOn", "Destination") and el.get("type") == "object":
                    if el.text and el.text not in selfs:
                        errors.append(f"{n}: {tag} -> {el.text} points to nothing")
                if tag == "Link":
                    uri = el.get("LinkResourceURI", "")
                    if uri.startswith("file:"):
                        p = Path(urllib.request.url2pathname(urllib.parse.urlparse(uri).path))
                        if not p.exists():
                            errors.append(f"linked file missing: {p}")
        story_list = (dm.get("StoryList") or "").split()
        for sid in story_list:
            if sid not in selfs:
                errors.append(f"StoryList id {sid} has no story")
        n_stories = sum(1 for n in names if n.startswith("Stories/"))
        n_spreads = sum(1 for n in names if n.startswith("Spreads/"))
        info.append(f"{n_stories} stories, {n_spreads} spreads, {len(selfs)} objects")
    return {"errors": errors, "info": info}
