"""Explicit maintained extraction of selected JSON-LD scripts from owned HTML."""

from .extraction import extract_html_jsonld
from .types import HtmlExtraction, HtmlScript

__all__ = ["HtmlExtraction", "HtmlScript", "extract_html_jsonld"]
