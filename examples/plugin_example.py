"""Example plugin:  python -m html2idml page.html --plugin examples/plugin_example.py

A plugin may define either or both hooks. Return None to keep working on the same object.
"""


def transform_html(root, config):
    """Runs on the lxml HTML tree BEFORE CSS is applied."""
    # drop navigation and footers that should not go to print
    for el in root.xpath("//nav | //footer | //*[contains(@class,'no-print')]"):
        el.getparent().remove(el)
    # mark every <h1> to start on a new page
    for h1 in root.iter("h1"):
        h1.set("data-idml-page-break", "")


def transform_ir(doc, config):
    """Runs on the IR Document AFTER conversion, BEFORE writing IDML."""
    # 1. Rename a style everywhere
    rename = {"p": "Body Text"}
    for old, new in rename.items():
        if old in doc.paragraph_styles and new not in doc.paragraph_styles:
            st = doc.paragraph_styles.pop(old)
            st.name = new
            doc.paragraph_styles[new] = st
            for s in doc.paragraph_styles.values():
                if s.based_on == old:
                    s.based_on = new
            for block in doc.iter_blocks():
                if getattr(block, "style", None) == old:
                    block.style = new

    # 2. Tweak typography globally
    body = doc.paragraph_styles.get("Body")
    if body:
        body.props["hyphenation"] = True
        body.props["idml:Ligatures"] = True   # raw IDML attribute passthrough

    # 3. Remove empty paragraphs
    for story in doc.stories:
        story.blocks = [b for b in story.blocks
                        if getattr(b, "type", "") != "paragraph" or b.runs or b.source == "hr"]
