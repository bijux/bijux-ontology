"""Original markup terms beside explicitly scoped maintained parser evidence."""

from dataclasses import dataclass
from typing import Literal as Choice

from rdflib import Literal

from bijux_ontology.reports import Report

MarkupCanonicalization = Choice["xml-exc-c14n-with-comments"]
MarkupRelation = Choice["equal", "different", "indeterminate"]


@dataclass(frozen=True)
class MarkupInspection:
    """Owned inspection, never an RDF graph or a rewritten original literal.

    canonical contains exclusive XML C14N 1.0 bytes with comments only when
    explicitly requested and qualified. HTML evidence is limited to the actual
    lxml/libxml2 fragment parser; it does not establish HTML5 RDF value equality.
    Inline namespaces, xml:lang and xml:base remain literal content, without
    inherited context, resource resolution or browser rendering.
    """

    original: Literal | None
    report: Report
    kind: Choice["xml", "html"] | None = None
    canonical: bytes | None = None
    text: str = ""
    element_names: tuple[str, ...] = ()
    comment_count: int = 0
    backend: str = ""


@dataclass(frozen=True)
class MarkupComparison:
    """Canonical comparison truth separate from exact RDF term identity."""

    before: MarkupInspection | None
    after: MarkupInspection | None
    relation: MarkupRelation
    report: Report
