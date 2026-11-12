# html2idml

Convert **HTML + CSS + images + fonts** into an Adobe InDesign **.idml** package, with a fully
editable *intermediate representation* (IR) in the middle so you control every style,
paragraph, run, image and table before the IDML is written.

```
HTML + CSS ─► CSS cascade (browser-like computed styles)
           ─► Builder ─► IR Document   ◄── you edit here: Python, JSON, plugins, config
           ─► IDML writer ─► page.idml + Links/ + Document fonts/ + page.fit-pages.jsx + report
```

## Install

```bash
pip install -r requirements.txt
```

## Web interface

```bash
python -m html2idml serve
```

Opens http://127.0.0.1:8765 in your browser:

- **Upload**: files, a whole folder, a zipped website, a URL, or pasted HTML (drag and drop works).
- **Options**: page size and margins, columns, CSS media, font mapping, style names, image and font
  packaging, InDesign version, colour space, plus a free-form YAML box for any option.
- **Tracking**: every job is listed with its live stage and progress; jobs survive a restart
  (stored in `html2idml-work/`).
- **Results**: page/paragraph/table/image/style counts, warnings, fonts used, and downloads
  (package zip, .idml, fit-pages script, report, IR JSON; all documents at once for multi-page sites).
- **Styles editor**: change fonts, sizes, leading, colours, alignment, spacing, rename styles,
  change "based on", edit page size and margins, then **Save & rebuild** - the IDML is regenerated.
- **Outline / Assets / Source page / Log** tabs to check the mapping against the original page.

Options: `--port 9000`, `--host 0.0.0.0` (share on your network), `--workdir D:/jobs`, `--workers 4`.

## Command line

```bash
python -m html2idml convert page.html -o out/page.idml
```

```bash
python -m html2idml convert page.html -o out/page.idml -c examples/config.example.yaml --save-ir --zip
```

Output folder (an InDesign-style package):

| File | What it is |
|---|---|
| `page.idml` | open with InDesign (File > Open) |
| `Links/` | every image, copied (WEBP/AVIF/ICO converted to PNG, inline `<svg>` saved as .svg); images are also embedded in the .idml by default (`--link-images` to only link) |
| `Document fonts/` | every font the page uses: `@font-face` files plus the installed font files (e.g. Segoe UI from `C:\Windows\Fonts`); InDesign activates this folder for the document. Check the font licences before sharing |
| `page-package.zip` | everything above in one file, to move to the machine running InDesign |
| `page.fit-pages.jsx` | run once in InDesign: adds/removes pages so the page count is exact |
| `page.report.json` | stats, assets, fonts used, warnings |
| `page.ir.json` | the editable IR (with `--save-ir`) |

### Full control: edit the IR

```bash
python -m html2idml extract page.html -o out/page.ir.json
```

Edit `page.ir.json` (styles, colours, paragraphs, runs, tables, page setup), then:

```bash
python -m html2idml build out/page.ir.json -o out/page.idml
```

Or in Python:

```python
from html2idml import html_to_ir, ir_to_idml, Config

cfg = Config.load("examples/config.example.yaml")
doc = html_to_ir("page.html", "out", cfg)

doc.paragraph_styles["h1"].props.update(font_family="Minion Pro", font_size=30)
doc.page.columns = 2
for p in doc.iter_paragraphs():
    if p.style == "p.note":
        p.overrides["shading"] = doc.add_color((255, 240, 200))

ir_to_idml(doc, "out/page.idml", cfg)
```

### Control from the HTML itself

```html
<p data-idml-style="Intro">...</p>                        force a paragraph style name
<span data-idml-char-style="Brand">...</span>               force a character style name
<p data-idml-props='{"keep_with_next": 3, "align": "justify"}'>  paragraph overrides
<span data-idml-char-props='{"tracking": 50}'>              character overrides
<div data-idml-skip>...</div>                               leave out
<section data-idml-page-break>...</section>                 start on a new page
```

### Plugins

`--plugin my.py` (or `plugins:` in the config) with `transform_html(root, config)` (lxml tree,
before CSS) and/or `transform_ir(doc, config)` (before writing). See `examples/plugin_example.py`.

Any IR prop named `"idml:<Attribute>"` is written to the IDML verbatim, e.g.
`"idml:HorizontalScale": 90`, so every InDesign text attribute is reachable.

## What is converted

- **CSS**: `<style>`, `<link>`, `@import`, inline `style`, `@media` (screen/print + width queries),
  `@page` (size, margins), `@font-face`, custom properties `var()`, `calc()/min()/max()/clamp()`,
  full specificity and `!important`, inheritance, `::before/::after` text content, all common
  shorthands, px/pt/em/rem/%/vw/mm/cm/in units.
- **Text**: font family (first *installed* family of each stack on Windows), weight/italic → InDesign
  font style, size, line-height → leading, colour (with alpha flattened on paper), letter-spacing →
  tracking, uppercase/small-caps, underline, strike, super/subscript, inline background →
  highlight, `nowrap`, `white-space: pre*`, `<br>` → forced line break, non-breaking spaces.
- **Paragraphs**: alignment, margins (collapsed like a browser) → space before/after,
  padding/margins → indents, `text-indent`, block background → paragraph shading,
  `border-top/bottom` and `<hr>` → paragraph rules, `break-before/after: page`, headings keep with next.
- **Lists**: bullets (disc/circle/square), numbering (decimal, alpha, roman), `start`, `value`,
  nesting levels with hanging indents.
- **Tables**: thead/tbody/tfoot (header rows repeat across pages), colspan/rowspan, nested tables,
  column widths (CSS, attributes, `<col>`, or content-based auto layout), cell fill, padding → insets,
  per-edge borders, vertical alignment, captions.
- **Images**: `<img>` (src/srcset/lazy data-src, data: URIs, local or remote), size from CSS/attributes/
  natural size (96 dpi) with max-width and fit-to-column, inline `<svg>`; placed as inline
  graphics so they flow with the text.
- **Links**: `<a href>` → InDesign hyperlinks.
- **Styles**: one paragraph style per element signature (`h1`, `p.lead`, `ul li 2` …) based on a
  `Body` style, one character style per inline signature (`strong`, `em strong`, `a` …); only
  differences are stored as local overrides, so restyling in InDesign works as expected.

## Notes and limits

- InDesign does its own line breaking; the page count is estimated, then `*.fit-pages.jsx` makes it
  exact (Smart Text Reflow is also switched on in the document).
- Floats, absolute positioning, flex/grid layouts, CSS transforms, gradients, box-shadows and
  background images are flattened into the single text flow (content is kept, the 2-D layout is not).
- Left/right block borders become InDesign paragraph borders (InDesign 2019+); top/bottom become rules.
- WOFF/WOFF2 web fonts are copied but InDesign needs the TTF/OTF installed.
- Default `dom_version` is 16.0 (InDesign 2021+); use `--dom-version 8.0` for CS6.

## Tests

```bash
python tests/test_html2idml.py
python tests/test_web.py
```
