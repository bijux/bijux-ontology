"""Explicit local terminology inputs and immutable original SDRF cell evidence."""

from dataclasses import dataclass

from rdflib import URIRef
from rdflib.namespace import RDFS

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.browsing.results import Edge
from bijux_ontology.vocabulary.sources.view import Vocabulary


@dataclass(frozen=True)
class SdrfOntology:
    """An owned vocabulary and explicit accession/lexical interpretation.

    ``closed`` declares the selected source complete for accession and asserted
    subclass absence checks. It does not assert an OWL closed-world semantics.
    """

    name: str
    vocabulary: Vocabulary | VocabularyView
    accession_predicate: URIRef
    label_predicates: tuple[URIRef, ...] = (RDFS.label,)
    synonym_predicates: tuple[URIRef, ...] = ()
    closed: bool = False

    def __post_init__(self) -> None:
        """Reject guessed identifiers, mutable inventories and invalid source kinds."""
        operation = Budget()
        if type(self.name) is not str or not self.name.strip():
            raise InputError("An SDRF ontology requires an explicit name")
        bound_text(self.name, operation)
        if not isinstance(self.vocabulary, Vocabulary | VocabularyView):
            raise InputError("SDRF terminology requires an owned vocabulary or view")
        for values in (
            (self.accession_predicate,),
            self.label_predicates,
            self.synonym_predicates,
        ):
            if type(values) is not tuple:
                raise InputError("SDRF predicate inventories require tuples")
            operation.consume("states", len(values))
            for predicate in values:
                if not isinstance(predicate, URIRef):
                    raise InputError("SDRF mappings require exact predicate IRIs")
                bound_node(predicate, operation)
        if not self.label_predicates or type(self.closed) is not bool:
            raise InputError("SDRF label and completeness policies must be explicit")


@dataclass(frozen=True)
class SdrfTerm:
    """Original accession and asserted lexical forms, without lowercasing identity."""

    ontology: str
    iri: URIRef
    accession: str
    labels: tuple[str, ...]
    assertions: tuple[Edge, ...]


@dataclass(frozen=True)
class SdrfCell:
    """Original parsed text at its actual row and physical column ordinal."""

    row: int
    column: int
    name: str
    value: str


@dataclass(frozen=True)
class SdrfDiagnostic:
    """Bounded owned evidence from the unchanged maintained validator."""

    code: str
    message: str
    severity: str
    row: int
    column: str | None
    value: str | None


@dataclass(frozen=True)
class SdrfObligation:
    """Effective original ontology-validator policy for a physical column."""

    column: int | None
    name: str
    validation: int
    requirement: str
    ontologies: tuple[str, ...]
    parent_accession: str | None
    allow_not_applicable: bool
    allow_not_available: bool


@dataclass(frozen=True)
class SdrfCheck:
    """Consumer validation and required local semantic completion remain distinct."""

    cells: tuple[SdrfCell, ...]
    obligations: tuple[SdrfObligation, ...]
    diagnostics: tuple[SdrfDiagnostic, ...]
    consumer_report: Report
    report: Report
