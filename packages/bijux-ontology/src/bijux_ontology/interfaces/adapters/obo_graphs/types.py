"""Supported OBOGraphs meaning and explicit projection loss evidence."""

from dataclasses import dataclass

from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.annotations.graphs.types import AnnotationStatement


@dataclass(frozen=True)
class OboGraphsDocument:
    """Owned original JSON and one selected ontology's supported RDF meaning.

    A source is published only after the selected supported mapping completes.
    This scope preserves axiom/annotation meaning, not exact RDF serialization
    or anonymous-expression identity across different syntaxes.
    """

    payload: bytes
    graph_id: str | None
    source: PreparedSource | None
    report: Report


@dataclass(frozen=True)
class OboGraphsExport:
    """Produced JSON beside every actual unrepresented original assertion.

    Explicit lossy output still carries its required loss report. Its availability
    does not certify lossless preservation or authorize a consumer to ignore it.
    """

    content: bytes | None
    omitted: tuple[AnnotationStatement, ...]
    report: Report
