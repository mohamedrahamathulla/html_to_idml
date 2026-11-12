"""html2idml - convert HTML + CSS + images + fonts into Adobe InDesign IDML.

Pipeline (every stage is replaceable / controllable):

    HTML + CSS ──► css.Cascade (computed styles, like a browser)
               ──► builder.Builder ──► ir.Document   (the intermediate representation you control)
               ──► [plugins / your code / JSON editing]
               ──► idml_writer.IDMLWriter ──► .idml + Links/ + Document fonts/ + fit-pages.jsx
"""
from .config import Config
from .ir import (Asset, Cell, Color, Document, ImageRun, PageSetup, Paragraph, Row, Story, Style,
                 Table, TextRun)
from .pipeline import convert, html_to_ir, ir_to_idml
from .validate import validate_idml

__version__ = "1.0.0"
__all__ = ["convert", "html_to_ir", "ir_to_idml", "validate_idml", "Config", "Document",
           "PageSetup", "Style", "Color", "Asset", "Story", "Paragraph", "TextRun", "ImageRun",
           "Table", "Row", "Cell"]
