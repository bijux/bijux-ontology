"""Original curated xrefs, explicit local catalogue scope and separate resolution."""

from dataclasses import dataclass
from typing import Literal

from rdflib import URIRef

from bijux_ontology.errors import InputError
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.accessions.types import (
    AccessionCheck,
    AccessionObservation,
)
from bijux_ontology.vocabulary.annotations.graphs.types import AnnotationGraph
from bijux_ontology.vocabulary.inspection import VocabularyView
from bijux_ontology.vocabulary.obo.types import OboAttachment, OboRecord, OboRecords
from bijux_ontology.vocabulary.results import Edge, TermInfo


@dataclass(frozen=True)
class CrossReferenceAssertion:
    """An original dbxref assertion with its exact native annotation boundary.

    owner is the requested term. An unresolved attachment does not establish
    that its qualifier belongs to a particular definition or synonym record.
    """

    owner: URIRef
    assertion: Edge
    attachment: OboAttachment | None = None
    record: OboRecord | None = None
    annotations: AnnotationGraph | None = None


@dataclass(frozen=True)
class LocalCatalogue:
    """An explicitly supplied namespace inventory, never a remote resolver.

    Closed applies only to exact IRIs under namespace in the selected view.
    The longest supplied prefix selects the applicable catalogue. Membership is
    either an RDF declaration or an actual outgoing assertion. An accession's
    alternate representation does not establish IRI equivalence.
    """

    namespace: str
    view: VocabularyView
    closed: bool = False
    membership: Literal["declared", "subject"] = "declared"

    def __post_init__(self) -> None:
        """Require explicit local policy without scanning a resource at declaration."""
        if type(self.namespace) is not str or not isinstance(self.view, VocabularyView):
            raise InputError(
                "Local catalogues require namespace text and a vocabulary view"
            )
        if type(self.closed) is not bool or self.membership not in (
            "declared",
            "subject",
        ):
            raise InputError("Catalogue closure and membership policy must be explicit")


@dataclass(frozen=True)
class CrossReferenceObservation:
    """Separate accession validity, exact local resolution and raw RDF relevance."""

    reference: CrossReferenceAssertion
    accession: AccessionObservation | None
    iri: URIRef | None
    resolution: Literal[
        "resolved", "dangling", "external", "unknown", "invalid", "excluded"
    ]
    catalogue: LocalCatalogue | None = None
    term: TermInfo | None = None
    assertions: tuple[Edge, ...] = ()


@dataclass(frozen=True)
class CrossReferenceCheck:
    """Captured xrefs and their required identifier/resolution checks.

    obo_records retains raw native annotation interpretation separately. Text and
    synonym-type validity outside the xref contract is observational evidence;
    actual capture completeness and each xref attachment remain required.
    """

    references: tuple[CrossReferenceAssertion, ...]
    observations: tuple[CrossReferenceObservation, ...]
    accessions: AccessionCheck | None
    report: Report
    obo_records: tuple[OboRecords, ...] = ()
