"""Selected HTML script locations beside independently owned JSON-LD and RDF."""

import json
from dataclasses import dataclass

from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.snapshots import PreparedSource


@dataclass(frozen=True)
class HtmlScript:
    """One selected script's original document order, ID and parser line."""

    position: int
    identifier: str | None
    line: int | None


@dataclass(frozen=True)
class HtmlExtraction:
    """Maintained extraction and conversion truth, without executing scripts."""

    document_json: str | None
    base: str | None
    scripts: tuple[HtmlScript, ...]
    source: PreparedSource | None
    report: Report

    @property
    def document(self) -> object:
        """Return an independent JSON copy of the selected semantic input."""
        return (
            json.loads(self.document_json) if self.document_json is not None else None
        )
