"""IR Document  ->  .idml package (a zip that Adobe InDesign opens directly)."""
from __future__ import annotations

import base64
import math
import re
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape, quoteattr

from .ir import (CHAR_KEYS, Document, ImageRun, Paragraph, Table, TextRun, table_grid)

PKG_NS = "http://ns.adobe.com/AdobeInDesign/idml/1.0/packaging"
NO_PSTYLE = "ParagraphStyle/$ID/[No paragraph style]"
NO_CSTYLE = "CharacterStyle/$ID/[No character style]"
_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")

# IDML attributes that are character-level (for raw "idml:Xxx" passthrough props)
IDML_CHAR_ATTRS = {
    "PointSize", "FontStyle", "FillColor", "FillTint", "Tracking", "Capitalization", "Position",
    "Underline", "StrikeThru", "NoBreak", "HorizontalScale", "VerticalScale", "BaselineShift",
    "Skew", "Ligatures", "KerningMethod", "KerningValue", "OTFFigureStyle", "StrokeColor",
    "StrokeWeight", "StrokeTint", "UnderlineColor", "UnderlineWeight", "UnderlineOffset",
    "UnderlineTint", "UnderlineType", "StrikeThroughColor", "StrikeThroughWeight",
    "StrikeThroughOffset", "CharacterAlignment", "OTFContextualAlternate", "OTFOrdinal",
    "OTFFraction", "OTFSwash", "OTFTitling", "OTFDiscretionaryLigature", "OTFSlashedZero",
    "Tsume", "CharacterRotation", "GlyphForm", "OverprintFill", "OverprintStroke",
}
IDML_CHAR_PROPS = {"AppliedFont", "Leading", "AppliedLanguage"}


def fmt(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return "0"
        s = f"{v:.4f}".rstrip("0").rstrip(".")
        return "0" if s in ("-0", "") else s
    return str(v)


def attrs_xml(attrs: Dict[str, Any]) -> str:
    return "".join(f" {k}={quoteattr(clean(fmt(v)))}" for k, v in attrs.items() if v is not None)


def clean(s: str) -> str:
    return _BAD_XML.sub("", s)


def text_xml(s: str) -> str:
    return escape(clean(s))


def safe_name(name: str) -> str:
    n = re.sub(r"[:/\\]", "-", str(name)).strip()
    if n.startswith("$ID"):
        n = n[3:]
    return n or "Style"


class IDMLWriter:
    def __init__(self, doc: Document, config: Dict[str, Any], warn=print):
        self.doc = doc
        self.cfg = config
        self.warn = warn
        idml = config["idml"]
        self.dom = str(idml.get("dom_version", "16.0"))
        try:
            self.dom_major = float(self.dom)
        except ValueError:
            self.dom_major = 16.0
        self.shading_ok = bool(idml.get("paragraph_shading", True)) and self.dom_major >= 14
        self.embed = bool(config["images"].get("embed", False))
        self._n = 0x100
        self.hyperlinks: List[Tuple[str, str, str]] = []   # (source_id, url, name)
        self.layer_id = self.uid()
        self.stats = {"paragraphs": 0, "tables": 0, "images": 0, "hyperlinks": 0}

    def uid(self) -> str:
        self._n += 1
        return f"u{self._n:x}"

    # ------------------------------------------------------------------ refs
    def color_ref(self, name: Optional[str]) -> str:
        if not name:
            return "Swatch/None"
        if name in ("Black", "Paper", "Registration"):
            return f"Color/{name}"
        if name == "None":
            return "Swatch/None"
        if name not in self.doc.colors:
            self.warn(f"unknown colour {name!r}, using Black")
            return "Color/Black"
        return f"Color/{safe_name(name)}"

    def pstyle_ref(self, name: Optional[str]) -> str:
        if not name or name not in self.doc.paragraph_styles:
            return NO_PSTYLE
        return f"ParagraphStyle/{safe_name(name)}"

    def cstyle_ref(self, name: Optional[str]) -> str:
        if not name or name not in self.doc.character_styles:
            return NO_CSTYLE
        return f"CharacterStyle/{safe_name(name)}"

    # ------------------------------------------------------------------ property mapping
    def map_props(self, props: Dict[str, Any]):
        """Return (para_attrs, char_attrs, para_props_xml, char_props_xml)."""
        pa: Dict[str, Any] = {}
        ca: Dict[str, Any] = {}
        pp: Dict[str, str] = {}
        cp: Dict[str, str] = {}
        p = props
        if "font_family" in p and p["font_family"]:
            cp["AppliedFont"] = f'<AppliedFont type="string">{text_xml(p["font_family"])}</AppliedFont>'
        if "font_style" in p and p["font_style"]:
            ca["FontStyle"] = p["font_style"]
        if "font_size" in p and p["font_size"]:
            ca["PointSize"] = float(p["font_size"])
        if "leading" in p and p["leading"] is not None:
            if p["leading"] == "auto":
                cp["Leading"] = '<Leading type="enumeration">Auto</Leading>'
            else:
                cp["Leading"] = f'<Leading type="unit">{fmt(float(p["leading"]))}</Leading>'
        if "color" in p:
            ca["FillColor"] = self.color_ref(p["color"] or "Black")
        if "tracking" in p and p["tracking"] is not None:
            ca["Tracking"] = float(p["tracking"])
        if "caps" in p:
            ca["Capitalization"] = {"all_caps": "AllCaps", "small_caps": "SmallCaps"}.get(p["caps"], "Normal")
        if "position" in p:
            ca["Position"] = {"superscript": "Superscript", "subscript": "Subscript"}.get(p["position"], "Normal")
        if "strike" in p:
            ca["StrikeThru"] = bool(p["strike"])
        if "no_break" in p:
            ca["NoBreak"] = bool(p["no_break"])
        hl = p.get("highlight")
        if hl:
            size = float(p.get("font_size") or 12.0)
            ca["Underline"] = True
            ca["UnderlineColor"] = self.color_ref(hl)
            ca["UnderlineWeight"] = round(size * 1.15, 2)
            ca["UnderlineOffset"] = round(-size * 0.28, 2)
        elif "underline" in p:
            ca["Underline"] = bool(p["underline"])

        # paragraph level
        if "align" in p:
            pa["Justification"] = {"center": "CenterAlign", "right": "RightAlign",
                                   "justify": "LeftJustified"}.get(p["align"], "LeftAlign")
        for key, attr in (("space_before", "SpaceBefore"), ("space_after", "SpaceAfter"),
                          ("left_indent", "LeftIndent"), ("right_indent", "RightIndent"),
                          ("first_line_indent", "FirstLineIndent")):
            if key in p and p[key] is not None:
                pa[attr] = float(p[key])
        if "keep_with_next" in p and p["keep_with_next"] is not None:
            pa["KeepWithNext"] = int(p["keep_with_next"])
        if "keep_together" in p:
            pa["KeepLinesTogether"] = bool(p["keep_together"])
            pa["KeepAllLinesTogether"] = bool(p["keep_together"])
        if "start_paragraph" in p:
            pa["StartParagraph"] = {"next_page": "NextPage", "next_column": "NextColumn",
                                    "next_frame": "NextFrame", "next_odd_page": "NextOddPage",
                                    "next_even_page": "NextEvenPage"}.get(p["start_paragraph"], "Anywhere")
        if "list_type" in p:
            lt = p["list_type"]
            pa["BulletsAndNumberingListType"] = {"bullet": "BulletList", "numbered": "NumberedList"}.get(lt, "NoList")
            if lt == "numbered":
                pa["NumberingExpression"] = "^#.^t"
        if "bullet_char" in p and p["bullet_char"]:
            pp["BulletChar"] = (f'<BulletChar BulletCharacterType="UnicodeOnly" '
                                f'BulletCharacterValue="{int(p["bullet_char"])}"/>')
        if "numbering_format" in p and p["numbering_format"]:
            pa["NumberingFormat"] = p["numbering_format"]
        if "numbering_start" in p and p["numbering_start"] is not None:
            pa["NumberingStartAt"] = int(p["numbering_start"])
        if "numbering_continue" in p:
            pa["NumberingContinue"] = bool(p["numbering_continue"])
        if "list_level" in p and p["list_level"]:
            pa["NumberingLevel"] = max(1, min(9, int(p["list_level"])))
        if "shading" in p and self.shading_ok:
            if p["shading"]:
                pa["ParagraphShadingOn"] = True
                pa["ParagraphShadingColor"] = self.color_ref(p["shading"])
                pa["ParagraphShadingTint"] = 100
            else:
                pa["ParagraphShadingOn"] = False
        if "border" in p and self.shading_ok:
            b = p["border"]
            if b and (b.get("left") or b.get("right")):
                pa["ParagraphBorderOn"] = True
                pa["ParagraphBorderColor"] = self.color_ref(b.get("color") or "Black")
                pa["ParagraphBorderTopLineWeight"] = 0.0
                pa["ParagraphBorderBottomLineWeight"] = 0.0
                pa["ParagraphBorderLeftLineWeight"] = float(b.get("left") or 0.0)
                pa["ParagraphBorderRightLineWeight"] = float(b.get("right") or 0.0)
            else:
                pa["ParagraphBorderOn"] = False
        for key, prefix in (("rule_above", "RuleAbove"), ("rule_below", "RuleBelow")):
            if key in p:
                r = p[key]
                if r:
                    pa[prefix] = True
                    pa[f"{prefix}LineWeight"] = float(r.get("weight", 1.0))
                    pa[f"{prefix}Color"] = self.color_ref(r.get("color") or "Black")
                    pa[f"{prefix}Offset"] = float(r.get("offset", 0.0))
                    pa[f"{prefix}Width"] = "ColumnWidth"
                    pa[f"{prefix}LeftIndent"] = float(p.get("left_indent") or 0.0)
                    pa[f"{prefix}RightIndent"] = float(p.get("right_indent") or 0.0)
                else:
                    pa[prefix] = False
        if "hyphenation" in p:
            pa["Hyphenation"] = bool(p["hyphenation"])
        # raw passthrough:  "idml:HorizontalScale": 90   /  "idml:Properties:Xyz": "<Xyz .../>"
        for k, v in p.items():
            if not isinstance(k, str) or not k.startswith("idml:"):
                continue
            name = k[5:]
            if name.startswith("Properties:"):
                pname = name.split(":", 1)[1]
                (cp if pname in IDML_CHAR_PROPS else pp)[pname] = str(v)
            elif name in IDML_CHAR_ATTRS:
                ca[name] = v
            else:
                pa[name] = v
        return pa, ca, pp, cp

    # ------------------------------------------------------------------ resources
    def graphic_xml(self) -> str:
        out = [f'<idPkg:Graphic xmlns:idPkg="{PKG_NS}" DOMVersion="{self.dom}">']
        out.append('<Color Self="Color/Black" Model="Process" Space="CMYK" ColorValue="0 0 0 100" '
                   'ColorOverride="Specialblack" AlternateSpace="NoAlternateColor" AlternateColorValue="" '
                   'Name="Black" ColorEditable="false" ColorRemovable="false" Visible="true" SwatchCreatorID="7937"/>')
        out.append('<Color Self="Color/Paper" Model="Process" Space="CMYK" ColorValue="0 0 0 0" '
                   'ColorOverride="Specialpaper" AlternateSpace="NoAlternateColor" AlternateColorValue="" '
                   'Name="Paper" ColorEditable="true" ColorRemovable="false" Visible="true" SwatchCreatorID="7937"/>')
        out.append('<Color Self="Color/Registration" Model="Registration" Space="CMYK" ColorValue="100 100 100 100" '
                   'ColorOverride="Specialregistration" AlternateSpace="NoAlternateColor" AlternateColorValue="" '
                   'Name="Registration" ColorEditable="false" ColorRemovable="false" Visible="true" SwatchCreatorID="7937"/>')
        for name, col in self.doc.colors.items():
            if name in ("Black", "Paper", "Registration", "None"):
                continue
            space = "CMYK" if col.space.upper() == "CMYK" else ("LAB" if col.space.upper() == "LAB" else "RGB")
            vals = " ".join(fmt(float(v)) for v in col.values)
            out.append(f'<Color Self={quoteattr("Color/" + safe_name(name))} Model="Process" Space="{space}" '
                       f'ColorValue="{vals}" ColorOverride="Normal" AlternateSpace="NoAlternateColor" '
                       f'AlternateColorValue="" Name={quoteattr(safe_name(name))} ColorEditable="true" '
                       f'ColorRemovable="true" Visible="true" SwatchCreatorID="7937"/>')
        out.append('<Swatch Self="Swatch/None" Name="None" ColorEditable="false" ColorRemovable="false" '
                   'Visible="true" SwatchCreatorID="7937"/>')
        out.append('<StrokeStyle Self="StrokeStyle/$ID/Solid" Name="$ID/Solid"/>')
        out.append("</idPkg:Graphic>")
        return "\n".join(out)

    def _style_xml(self, tag: str, self_id: str, name: str, props: Dict[str, Any],
                   based_on: Optional[str], extra: str = "") -> str:
        pa, ca, pp, cp = self.map_props(props)
        attrs = dict(pa)
        attrs.update(ca)
        inner = []
        if based_on:
            inner.append(f'<BasedOn type="object">{text_xml(based_on)}</BasedOn>')
        elif tag == "ParagraphStyle":
            inner.append('<BasedOn type="string">$ID/[No paragraph style]</BasedOn>')
        inner.append('<PreviewColor type="enumeration">Nothing</PreviewColor>')
        inner.extend(pp.values())
        inner.extend(cp.values())
        return (f"<{tag} Self={quoteattr(self_id)} Name={quoteattr(name)} Imported=\"false\"{extra}"
                f"{attrs_xml(attrs)}><Properties>{''.join(inner)}</Properties></{tag}>")

    def styles_xml(self) -> str:
        out = [f'<idPkg:Styles xmlns:idPkg="{PKG_NS}" DOMVersion="{self.dom}">']
        out.append(f'<RootCharacterStyleGroup Self="{self.uid()}">')
        out.append(f'<CharacterStyle Self="{NO_CSTYLE}" Imported="false" Name="$ID/[No character style]"/>')
        for name, st in self.doc.character_styles.items():
            sid = f"CharacterStyle/{safe_name(name)}"
            pa, ca, pp, cp = self.map_props(st.props)
            inner = ['<PreviewColor type="enumeration">Nothing</PreviewColor>'] + list(cp.values())
            out.append(f"<CharacterStyle Self={quoteattr(sid)} Imported=\"false\" KeyboardShortcut=\"0 0\" "
                       f"Name={quoteattr(safe_name(name))}{attrs_xml(ca)}><Properties>{''.join(inner)}"
                       f"</Properties></CharacterStyle>")
        out.append("</RootCharacterStyleGroup>")
        out.append(f'<RootParagraphStyleGroup Self="{self.uid()}">')
        out.append(f'<ParagraphStyle Self="{NO_PSTYLE}" Name="$ID/[No paragraph style]" Imported="false" '
                   f'NextStyle="{NO_PSTYLE}" KeyboardShortcut="0 0"/>')
        out.append('<ParagraphStyle Self="ParagraphStyle/$ID/NormalParagraphStyle" Name="$ID/NormalParagraphStyle" '
                   'Imported="false" NextStyle="ParagraphStyle/$ID/NormalParagraphStyle" KeyboardShortcut="0 0">'
                   '<Properties><BasedOn type="string">$ID/[No paragraph style]</BasedOn>'
                   '<PreviewColor type="enumeration">Nothing</PreviewColor></Properties></ParagraphStyle>')
        # parents must be written before children
        order, seen = [], set()

        def visit(n, depth=0):
            if n in seen or n not in self.doc.paragraph_styles or depth > 50:
                return
            seen.add(n)
            parent = self.doc.paragraph_styles[n].based_on
            if parent:
                visit(parent, depth + 1)
            order.append(n)

        for n in self.doc.paragraph_styles:
            visit(n)
        for name in order:
            st = self.doc.paragraph_styles[name]
            sid = f"ParagraphStyle/{safe_name(name)}"
            based = self.pstyle_ref(st.based_on) if st.based_on in self.doc.paragraph_styles else None
            out.append(self._style_xml("ParagraphStyle", sid, safe_name(name), st.props, based,
                                       extra=f' NextStyle={quoteattr(sid)} KeyboardShortcut="0 0"'))
        out.append("</RootParagraphStyleGroup>")
        out.append(f'<RootCellStyleGroup Self="{self.uid()}"><CellStyle Self="CellStyle/$ID/[None]" '
                   f'AppliedParagraphStyle="{NO_PSTYLE}" Name="$ID/[None]"/></RootCellStyleGroup>')
        out.append(f'<RootTableStyleGroup Self="{self.uid()}"><TableStyle Self="TableStyle/$ID/[No table style]" '
                   f'Name="$ID/[No table style]"/><TableStyle Self="TableStyle/$ID/[Basic Table]" '
                   f'Name="$ID/[Basic Table]"><Properties><BasedOn type="string">$ID/[No table style]</BasedOn>'
                   f'</Properties></TableStyle></RootTableStyleGroup>')
        out.append(f'<RootObjectStyleGroup Self="{self.uid()}">'
                   '<ObjectStyle Self="ObjectStyle/$ID/[None]" Name="$ID/[None]"/>'
                   '<ObjectStyle Self="ObjectStyle/$ID/[Normal Graphics Frame]" Name="$ID/[Normal Graphics Frame]">'
                   '<Properties><BasedOn type="object">ObjectStyle/$ID/[None]</BasedOn></Properties></ObjectStyle>'
                   '<ObjectStyle Self="ObjectStyle/$ID/[Normal Text Frame]" Name="$ID/[Normal Text Frame]">'
                   '<Properties><BasedOn type="object">ObjectStyle/$ID/[None]</BasedOn></Properties></ObjectStyle>'
                   '</RootObjectStyleGroup>')
        out.append("</idPkg:Styles>")
        return "\n".join(out)

    def preferences_xml(self, n_pages: int) -> str:
        pg = self.doc.page
        units = self.cfg["idml"].get("ruler_units", "Points")
        margin = self._margin_pref()
        reflow = self.cfg["idml"].get("smart_text_reflow", True)
        return "\n".join([
            f'<idPkg:Preferences xmlns:idPkg="{PKG_NS}" DOMVersion="{self.dom}">',
            f'<DocumentPreference PageHeight="{fmt(pg.height)}" PageWidth="{fmt(pg.width)}" '
            f'PagesPerDocument="{n_pages}" FacingPages="{fmt(pg.facing_pages)}" '
            f'DocumentBleedTopOffset="0" DocumentBleedBottomOffset="0" DocumentBleedInsideOrLeftOffset="0" '
            f'DocumentBleedOutsideOrRightOffset="0" DocumentBleedUniformSize="true" '
            f'PageBinding="LeftToRight" AllowPageShuffle="true"/>',
            margin,
            f'<ViewPreference HorizontalMeasurementUnits="{units}" VerticalMeasurementUnits="{units}"/>',
            f'<TextPreference SmartTextReflow="{fmt(bool(reflow))}" AddPages="EndOfStory" '
            f'LimitToMasterTextFrames="false" PreserveFacingPageSpreads="false" DeleteEmptyPages="true"/>',
            "</idPkg:Preferences>",
        ])

    def _margin_pref(self) -> str:
        pg = self.doc.page
        cols = max(1, int(pg.columns))
        cw = pg.column_width
        positions = []
        x = 0.0
        for i in range(cols):
            positions += [x, x + cw]
            x += cw + pg.gutter
        return (f'<MarginPreference ColumnCount="{cols}" ColumnGutter="{fmt(float(pg.gutter))}" '
                f'Top="{fmt(pg.margin_top)}" Bottom="{fmt(pg.margin_bottom)}" Left="{fmt(pg.margin_left)}" '
                f'Right="{fmt(pg.margin_right)}" ColumnDirection="Horizontal" '
                f'ColumnsPositions="{" ".join(fmt(float(v)) for v in positions)}"/>')

    # ------------------------------------------------------------------ geometry
    @staticmethod
    def path_xml(x1, y1, x2, y2) -> str:
        pts = [(x1, y1), (x1, y2), (x2, y2), (x2, y1)]
        inner = "".join(
            f'<PathPointType Anchor="{fmt(float(x))} {fmt(float(y))}" LeftDirection="{fmt(float(x))} {fmt(float(y))}" '
            f'RightDirection="{fmt(float(x))} {fmt(float(y))}"/>' for x, y in pts)
        return (f'<Properties><PathGeometry><GeometryPathType PathOpen="false"><PathPointArray>{inner}'
                f'</PathPointArray></GeometryPathType></PathGeometry></Properties>')

    def page_layout(self, n_pages: int):
        """Return list of spreads; each spread is a list of (page_index, side) with side 'single'|'left'|'right'."""
        if not self.doc.page.facing_pages:
            return [[(i, "single")] for i in range(n_pages)]
        spreads = [[(0, "right")]]
        i = 1
        while i < n_pages:
            if i + 1 < n_pages:
                spreads.append([(i, "left"), (i + 1, "right")])
                i += 2
            else:
                spreads.append([(i, "left")])
                i += 1
        return spreads

    def page_geom(self, side: str):
        """(page ItemTransform tx, frame x1, frame x2)"""
        pg = self.doc.page
        W = pg.width
        if side == "left":
            return -W, -W + pg.margin_right, -pg.margin_left
        return 0.0, pg.margin_left, W - pg.margin_right

    def master_xml(self, master_id: str) -> str:
        pg = self.doc.page
        H, W = pg.height, pg.width
        sides = ["left", "right"] if pg.facing_pages else ["single"]
        pages = []
        for side in sides:
            tx = -W if side == "left" else 0.0
            name = {"left": "L", "right": "R", "single": "A"}[side] if pg.facing_pages else "A"
            pages.append(
                f'<Page Self="{self.uid()}" Name="{name}" AppliedMaster="n" OverrideList="" TabOrder="" '
                f'GridStartingPoint="TopOutside" UseMasterGrid="true" MasterPageTransform="1 0 0 1 0 0" '
                f'GeometricBounds="0 0 {fmt(H)} {fmt(W)}" ItemTransform="1 0 0 1 {fmt(tx)} {fmt(-H / 2)}">'
                f'<Properties><PageColor type="enumeration">UseMasterColor</PageColor></Properties>'
                f'{self._margin_pref()}</Page>')
        return (f'<idPkg:MasterSpread xmlns:idPkg="{PKG_NS}" DOMVersion="{self.dom}">\n'
                f'<MasterSpread Self="{master_id}" Name="A-Master" NamePrefix="A" BaseName="Master" '
                f'ShowMasterItems="true" PageCount="{len(sides)}" OverriddenPageItemProps="" '
                f'PrimaryTextFrame="n" ItemTransform="1 0 0 1 0 0">'
                f'<Properties><PageColor type="enumeration">UseMasterColor</PageColor></Properties>'
                f'{"".join(pages)}</MasterSpread>\n</idPkg:MasterSpread>')

    # ------------------------------------------------------------------ stories
    def story_xml(self, story_id: str, blocks) -> str:
        out = [f'<idPkg:Story xmlns:idPkg="{PKG_NS}" DOMVersion="{self.dom}">',
               f'<Story Self="{story_id}" AppliedTOCStyle="n" TrackChanges="false" StoryTitle="$ID/" '
               f'AppliedNamedGrid="n">',
               '<StoryPreference OpticalMarginAlignment="false" OpticalMarginSize="12" '
               'FrameType="TextFrameType" StoryOrientation="Horizontal" StoryDirection="LeftToRightDirection"/>',
               '<InCopyExportOption IncludeGraphicProxies="true" IncludeAllResources="false"/>']
        if not blocks:
            out.append(f'<ParagraphStyleRange AppliedParagraphStyle="{NO_PSTYLE}">'
                       f'<CharacterStyleRange AppliedCharacterStyle="{NO_CSTYLE}"/></ParagraphStyleRange>')
        self.blocks_xml(blocks, out)
        out.append("</Story>")
        out.append("</idPkg:Story>")
        return "\n".join(out)

    def blocks_xml(self, blocks, out: List[str]):
        n = len(blocks)
        for i, block in enumerate(blocks):
            last = i == n - 1
            if isinstance(block, Table):
                self.table_xml(block, out, last)
            else:
                self.paragraph_xml(block, out, last)

    def _psr_open(self, style: str, overrides: Dict[str, Any]):
        pa, ca, pp, cp = self.map_props(overrides or {})
        attrs = {"AppliedParagraphStyle": self.pstyle_ref(style)}
        attrs.update(pa)
        head = f"<ParagraphStyleRange{attrs_xml(attrs)}>"
        if pp:
            head += f"<Properties>{''.join(pp.values())}</Properties>"
        return head, ca, cp

    def _csr_open(self, cstyle, base_ca, base_cp, run_overrides):
        _, ca, _, cp = self.map_props(run_overrides or {})
        attrs = {"AppliedCharacterStyle": self.cstyle_ref(cstyle)}
        merged = dict(base_ca)
        merged.update(ca)
        attrs.update(merged)
        props = dict(base_cp)
        props.update(cp)
        head = f"<CharacterStyleRange{attrs_xml(attrs)}>"
        if props:
            head += f"<Properties>{''.join(props.values())}</Properties>"
        return head

    def paragraph_xml(self, para: Paragraph, out: List[str], last: bool):
        self.stats["paragraphs"] += 1
        head, pca, pcp = self._psr_open(para.style, para.overrides)
        out.append(head)
        runs = para.runs
        if not runs:
            out.append(self._csr_open(None, pca, pcp, {}) + ("" if last else "<Br/>") + "</CharacterStyleRange>")
        for j, run in enumerate(runs):
            csr = self._csr_open(run.char_style, pca, pcp, run.overrides)
            body = self.image_xml(run) if isinstance(run, ImageRun) else self.text_run_xml(run)
            tail = "<Br/>" if (j == len(runs) - 1 and not last) else ""
            out.append(f"{csr}{body}{tail}</CharacterStyleRange>")
        out.append("</ParagraphStyleRange>")

    def text_run_xml(self, run: TextRun) -> str:
        content = self.content_xml(run.text)
        if run.link and self.cfg["links"].get("hyperlinks", True):
            hid = self.uid()
            name = f"Hyperlink {len(self.hyperlinks) + 1}"
            self.hyperlinks.append((hid, run.link, name))
            self.stats["hyperlinks"] += 1
            return (f'<HyperlinkTextSource Self="{hid}" Name={quoteattr(name)} Hidden="false" '
                    f'AppliedCharacterStyle="n">{content}</HyperlinkTextSource>')
        return content

    @staticmethod
    def content_xml(text: str) -> str:
        if text == "":
            return ""
        return f"<Content>{text_xml(text)}</Content>"

    def image_xml(self, run: ImageRun) -> str:
        asset = self.doc.assets.get(run.asset)
        if asset is None:
            self.warn(f"missing asset {run.asset}")
            return ""
        self.stats["images"] += 1
        w, h = float(run.width), float(run.height)
        fmt_name = (asset.format or "").upper()
        if fmt_name == "SVG":
            gw, gh = (asset.width_px or 300) * 0.75, (asset.height_px or 150) * 0.75
        else:
            ppi = asset.ppi or 72.0
            gw = (asset.width_px or w) * 72.0 / ppi
            gh = (asset.height_px or h) * 72.0 / ppi
        sx = w / gw if gw else 1.0
        sy = h / gh if gh else 1.0
        rid, iid, lid = self.uid(), self.uid(), self.uid()
        uri = Path(asset.path).resolve().as_uri()
        elem = {"PDF": "PDF", "AI": "PDF", "EPS": "EPS"}.get(fmt_name, "Image")
        type_attr = {"JPEG": ' ImageTypeName="$ID/JPEG"',
                     "PNG": ' ImageTypeName="$ID/Portable Network Graphics (PNG)"'}.get(fmt_name, "") \
            if elem == "Image" else ""
        link_fmt = {"JPEG": ' LinkResourceFormat="$ID/JPEG"',
                    "PNG": ' LinkResourceFormat="$ID/Portable Network Graphics (PNG)"'}.get(fmt_name, "")
        contents = ""
        stored = "Normal"
        if self.embed and elem == "Image":
            try:
                data = Path(asset.path).read_bytes()
                contents = f"<Contents><![CDATA[{base64.b64encode(data).decode('ascii')}]]></Contents>"
                stored = "Embedded"
            except OSError:
                pass
        return (
            f'<Rectangle Self="{rid}" ContentType="GraphicType" StoryTitle="$ID/" StrokeWeight="0" '
            f'StrokeColor="Swatch/None" FillColor="Swatch/None" ItemLayer="{self.layer_id}" '
            f'ItemTransform="1 0 0 1 0 0" AppliedObjectStyle="ObjectStyle/$ID/[None]">'
            f'{self.path_xml(0, 0, w, h)}'
            f'<AnchoredObjectSetting AnchoredPosition="InlinePosition" AnchorYoffset="0"/>'
            f'<{elem} Self="{iid}" ItemTransform="{fmt(sx)} 0 0 {fmt(sy)} 0 0"{type_attr} '
            f'AppliedObjectStyle="ObjectStyle/$ID/[None]">'
            f'<Properties><GraphicBounds Left="0" Top="0" Right="{fmt(gw)}" Bottom="{fmt(gh)}"/>{contents}</Properties>'
            f'<Link Self="{lid}" LinkResourceURI={quoteattr(uri)} StoredState="{stored}"{link_fmt}/>'
            f'</{elem}></Rectangle>')

    def table_xml(self, table: Table, out: List[str], last: bool):
        self.stats["tables"] += 1
        kinds_order = {"header": 0, "body": 1, "footer": 2}
        rows = table.rows
        if [kinds_order.get(r.kind, 1) for r in rows] != sorted(kinds_order.get(r.kind, 1) for r in rows):
            rows = sorted(rows, key=lambda r: kinds_order.get(r.kind, 1))
        positions, ncols = table_grid(rows)
        widths = list(table.column_widths[:ncols])
        if len(widths) < ncols:
            fill = (sum(widths) / len(widths)) if widths else self.doc.page.column_width / max(1, ncols)
            widths += [fill] * (ncols - len(widths))
        n_head = sum(1 for r in rows if r.kind == "header")
        n_foot = sum(1 for r in rows if r.kind == "footer")
        tid = self.uid()
        head, pca, pcp = self._psr_open(table.style, table.overrides)
        out.append(head)
        out.append(self._csr_open(None, pca, pcp, {}))
        tattrs = {"Self": tid, "HeaderRowCount": n_head, "FooterRowCount": n_foot,
                  "BodyRowCount": len(rows) - n_head - n_foot, "ColumnCount": ncols,
                  "AppliedTableStyle": "TableStyle/$ID/[Basic Table]", "TableDirection": "LeftToRightDirection",
                  "SpaceBefore": 0.0, "SpaceAfter": 0.0}
        for side in ("Top", "Bottom", "Left", "Right"):
            b = table.props.get(f"border_{side.lower()}")
            tattrs[f"{side}BorderStrokeWeight"] = float(b["weight"]) if b else 0.0
            if b:
                tattrs[f"{side}BorderStrokeColor"] = self.color_ref(b.get("color"))
        out.append(f"<Table{attrs_xml(tattrs)}>")
        for i, r in enumerate(rows):
            mh = max(3.0, float(r.min_height or 3.0))
            out.append(f'<Row Self="{tid}Row{i}" Name="{i}" SingleRowHeight="{fmt(mh)}" '
                       f'MinimumHeight="{fmt(mh)}" AutoGrow="true"/>')
        for j, w in enumerate(widths):
            out.append(f'<Column Self="{tid}Column{j}" Name="{j}" SingleColumnWidth="{fmt(max(3.0, float(w)))}"/>')
        for k, (r, c, cell) in enumerate(positions):
            p = cell.props or {}
            cattrs = {"Self": f"{tid}i{k}", "Name": f"{c}:{r}", "RowSpan": cell.row_span,
                      "ColumnSpan": min(cell.col_span, ncols - c),
                      "AppliedCellStyle": "CellStyle/$ID/[None]", "AppliedCellStylePriority": 0,
                      "FillColor": self.color_ref(p.get("fill")) if p.get("fill") else "Swatch/None",
                      "TopInset": float(p.get("inset_top", 4.0)), "LeftInset": float(p.get("inset_left", 4.0)),
                      "BottomInset": float(p.get("inset_bottom", 4.0)), "RightInset": float(p.get("inset_right", 4.0)),
                      "VerticalJustification": {"top": "TopAlign", "bottom": "BottomAlign"}.get(
                          p.get("vertical_align"), "CenterAlign")}
            for side in ("Top", "Left", "Bottom", "Right"):
                e = p.get(f"edge_{side.lower()}")
                cattrs[f"{side}EdgeStrokeWeight"] = float(e["weight"]) if e else 0.0
                if e:
                    cattrs[f"{side}EdgeStrokeColor"] = self.color_ref(e.get("color"))
            out.append(f"<Cell{attrs_xml(cattrs)}>")
            if cell.blocks:
                self.blocks_xml(cell.blocks, out)
            out.append("</Cell>")
        out.append("</Table>")
        if not last:
            out.append("<Br/>")
        out.append("</CharacterStyleRange></ParagraphStyleRange>")

    # ------------------------------------------------------------------ package
    def write(self, path: Path, n_pages: int) -> Dict[str, Any]:
        path = Path(path)
        doc = self.doc
        pg = doc.page
        H = pg.height
        master_id = self.uid()
        story_ids = {s.id: self.uid() for s in doc.stories}
        main_story = story_ids[doc.stories[0].id] if doc.stories else self.uid()
        files: Dict[str, str] = {}

        # stories
        for s in doc.stories:
            files[f"Stories/Story_{story_ids[s.id]}.xml"] = self.story_xml(story_ids[s.id], s.blocks)
        if not doc.stories:
            files[f"Stories/Story_{main_story}.xml"] = self.story_xml(main_story, [])

        # spreads with threaded frames for the main story
        spreads = self.page_layout(n_pages)
        frame_ids = [self.uid() for _ in range(n_pages)]
        page_ids = [self.uid() for _ in range(n_pages)]
        spread_files = []
        cols = max(1, int(pg.columns))
        for sp in spreads:
            sid = self.uid()
            items = []
            pages_xml = []
            for idx, side in sp:
                tx, x1, x2 = self.page_geom(side)
                pages_xml.append(
                    f'<Page Self="{page_ids[idx]}" Name="{idx + 1}" AppliedMaster="{master_id}" OverrideList="" '
                    f'TabOrder="" GridStartingPoint="TopOutside" UseMasterGrid="true" '
                    f'MasterPageTransform="1 0 0 1 0 0" GeometricBounds="0 0 {fmt(H)} {fmt(pg.width)}" '
                    f'ItemTransform="1 0 0 1 {fmt(tx)} {fmt(-H / 2)}">'
                    f'<Properties><PageColor type="enumeration">UseMasterColor</PageColor></Properties>'
                    f'{self._margin_pref()}</Page>')
                y1 = -H / 2 + pg.margin_top
                y2 = H / 2 - pg.margin_bottom
                prev_f = frame_ids[idx - 1] if idx > 0 else "n"
                next_f = frame_ids[idx + 1] if idx + 1 < n_pages else "n"
                items.append(
                    f'<TextFrame Self="{frame_ids[idx]}" ParentStory="{main_story}" PreviousTextFrame="{prev_f}" '
                    f'NextTextFrame="{next_f}" ContentType="TextType" ItemLayer="{self.layer_id}" '
                    f'ItemTransform="1 0 0 1 0 0" AppliedObjectStyle="ObjectStyle/$ID/[Normal Text Frame]">'
                    f'{self.path_xml(x1, y1, x2, y2)}'
                    f'<TextFramePreference TextColumnCount="{cols}" TextColumnGutter="{fmt(float(pg.gutter))}" '
                    f'TextColumnFixedWidth="{fmt(pg.column_width)}"/></TextFrame>')
            binding = 0 if (len(sp) == 1 and sp[0][1] in ("single", "right")) else 1
            if len(sp) == 1 and sp[0][1] == "left":
                binding = 1
            xml = (f'<idPkg:Spread xmlns:idPkg="{PKG_NS}" DOMVersion="{self.dom}">\n'
                   f'<Spread Self="{sid}" PageCount="{len(sp)}" BindingLocation="{binding}" ShowMasterItems="true" '
                   f'AllowPageShuffle="true" ItemTransform="1 0 0 1 0 0" FlattenerOverride="Default">'
                   f'{"".join(pages_xml)}{"".join(items)}</Spread>\n</idPkg:Spread>')
            fname = f"Spreads/Spread_{sid}.xml"
            files[fname] = xml
            spread_files.append(fname)

        files[f"MasterSpreads/MasterSpread_{master_id}.xml"] = self.master_xml(master_id)
        files["Resources/Graphic.xml"] = self.graphic_xml()
        files["Resources/Styles.xml"] = self.styles_xml()
        files["Resources/Preferences.xml"] = self.preferences_xml(n_pages)

        # designmap (written last: hyperlinks are collected while writing stories)
        story_list = " ".join(story_ids.values()) or main_story
        dm = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
              f'<?aid style="50" type="document" readerVersion="6.0" featureSet="257" product="{self.dom}(1)" ?>',
              f'<Document xmlns:idPkg="{PKG_NS}" DOMVersion="{self.dom}" Self="d" StoryList="{story_list}" '
              f'ZeroPoint="0 0" ActiveLayer="{self.layer_id}" CMYKProfile="U.S. Web Coated (SWOP) v2" '
              f'RGBProfile="sRGB IEC61966-2.1" SolidColorIntent="UseColorSettings" '
              f'AfterBlendingIntent="UseColorSettings" DefaultImageIntent="UseColorSettings" '
              f'RGBPolicy="PreserveEmbeddedProfiles" CMYKPolicy="CombinationOfPreserveAndSafeCmyk" '
              f'AccurateLABSpots="false">',
              '<idPkg:Graphic src="Resources/Graphic.xml"/>',
              '<idPkg:Styles src="Resources/Styles.xml"/>',
              '<idPkg:Preferences src="Resources/Preferences.xml"/>',
              f'<Layer Self="{self.layer_id}" Name="Layer 1" Visible="true" Locked="false" IgnoreWrap="false" '
              f'ShowGuides="true" LockGuides="false" UI="true" Expendable="true" Printable="true">'
              f'<Properties><LayerColor type="enumeration">LightBlue</LayerColor></Properties></Layer>',
              f'<idPkg:MasterSpread src="MasterSpreads/MasterSpread_{master_id}.xml"/>']
        dm += [f'<idPkg:Spread src="{f}"/>' for f in spread_files]
        dm.append(f'<Section Self="{self.uid()}" Length="{n_pages}" Name="" ComboName="" ContinueNumbering="false" '
                  f'IncludeSectionPrefix="false" PageNumberStyle="Arabic" PageStart="{page_ids[0]}" '
                  f'SectionPrefix="" Marker="" PageNumberStart="1"/>')
        dm += [f'<idPkg:Story src="Stories/Story_{sid}.xml"/>' for sid in story_ids.values()]
        if not doc.stories:
            dm.append(f'<idPkg:Story src="Stories/Story_{main_story}.xml"/>')
        dest_ids: Dict[str, Tuple[str, int]] = {}
        key = 0
        for _, url, _ in self.hyperlinks:
            if url not in dest_ids:
                key += 1
                dest_ids[url] = (self.uid(), key)
                did, k = dest_ids[url]
                dm.append(f'<HyperlinkURLDestination Self="{did}" DestinationURL={quoteattr(clean(url))} '
                          f'Name={quoteattr(clean(url)[:200] + " #" + str(k))} Hidden="false" '
                          f'DestinationUniqueKey="{k}"/>')
        for hid, url, name in self.hyperlinks:
            did, _ = dest_ids[url]
            dm.append(f'<Hyperlink Self="{self.uid()}" Name={quoteattr(name)} Source="{hid}" Visible="false" '
                      f'Highlight="None" Width="Thin" BorderStyle="Solid" Hidden="false">'
                      f'<Properties><BorderColor type="enumeration">Black</BorderColor>'
                      f'<Destination type="object">{did}</Destination></Properties></Hyperlink>')
        dm.append("</Document>")

        container = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                     '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                     '<rootfiles><rootfile full-path="designmap.xml" media-type="text/xml"/></rootfiles>'
                     '</container>')
        decl = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".idml.tmp")
        with zipfile.ZipFile(tmp, "w") as z:
            z.writestr(zipfile.ZipInfo("mimetype"), "application/vnd.adobe.indesign-idml-package",
                       compress_type=zipfile.ZIP_STORED)
            z.writestr("designmap.xml", "\n".join(dm), compress_type=zipfile.ZIP_DEFLATED)
            z.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
            for name, xml in files.items():
                z.writestr(name, decl + xml, compress_type=zipfile.ZIP_DEFLATED)
        tmp.replace(path)
        stats = dict(self.stats)
        stats["pages"] = n_pages
        return stats
