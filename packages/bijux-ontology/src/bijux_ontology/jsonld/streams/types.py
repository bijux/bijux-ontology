"""Explicit independent JSON document frames and their actual checked results."""

from collections.abc import Callable
from dataclasses import dataclass

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget
from bijux_ontology.jsonld.processing.preparation import JSONLDPreparation
from bijux_ontology.rdf.sources.snapshots import PreparedSource

RecordCheck = Callable[[PreparedSource, Budget], Report]


@dataclass(frozen=True)
class JSONLDRecord:
    """One complete serialized JSON document, never a URL or document fragment.

    Iterator boundaries frame the records. Text and bytes are immutable; their
    size, syntax and explicit identity metadata are qualified on the actual pull.
    """

    payload: str | bytes
    base: str | None = None
    document_iri: str | None = None

    def __post_init__(self) -> None:
        """Reject ambiguous framing types without parsing or reading later input."""
        if type(self.payload) not in (str, bytes):
            raise InputError("A JSON-LD record requires serialized JSON text or bytes")
        if any(
            type(value) is not str
            for value in (self.base, self.document_iri)
            if value is not None
        ):
            raise InputError("JSON-LD record identities require plain text or None")


@dataclass(frozen=True)
class JSONLDRecordResult:
    """One document's independent RDF scope and separately qualified check truth."""

    index: int
    base: str | None
    document_iri: str | None
    preparation: JSONLDPreparation
    report: Report
