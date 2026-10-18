"""Immutable OBO-in-OWL records with separate annotation attachment evidence."""

from dataclasses import dataclass
from typing import Literal as Role

from rdflib import BNode, Literal, URIRef

from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.results import Edge

_XREF = URIRef("http://www.geneontology.org/formats/oboInOwl#hasDbXref")
_SYNONYM_TYPE = URIRef("http://www.geneontology.org/formats/oboInOwl#hasSynonymType")


@dataclass(frozen=True)
class OboAttachment:
    """One original reification boundary, never a merged lexical qualifier set.

    statements preserve original anchors and opaque qualifier edges. Qualifiers
    are not recursively interpreted. Blank identities belong to this view only.
    An unresolved or malformed attachment never supplies a record's qualifiers.
    complete describes the outgoing structural inventory; status and assertions
    separately describe whether the exact base association was established.
    """

    node: URIRef | BNode
    scope: str
    status: Role["attached", "malformed", "unresolved"]
    assertions: tuple[Edge, ...]
    statements: tuple[Edge, ...]
    qualifiers: tuple[Edge, ...]
    complete: bool

    @property
    def cross_references(self) -> tuple[Edge, ...]:
        """Retain each original xref term without interpreting external IDs."""
        return tuple(edge for edge in self.qualifiers if edge.predicate == _XREF)

    @property
    def synonym_types(self) -> tuple[Edge, ...]:
        """Expose explicit synonym-type assertions within this attachment only."""
        return tuple(
            edge for edge in self.qualifiers if edge.predicate == _SYNONYM_TYPE
        )

    @property
    def opaque(self) -> tuple[Edge, ...]:
        """Retain other annotation predicates and values without guessed meaning."""
        return tuple(
            edge
            for edge in self.qualifiers
            if edge.predicate not in (_XREF, _SYNONYM_TYPE)
        )


@dataclass(frozen=True)
class OboRecord:
    """One scoped definition or synonym assertion, including malformed values."""

    role: Role["definition", "exact", "broad", "narrow", "related"]
    assertion: Edge
    attachments: tuple[OboAttachment, ...] = ()
    complete: bool = False

    @property
    def literal(self) -> Literal | None:
        """Return the original literal, without normalizing its lexical form."""
        return (
            self.assertion.object
            if isinstance(self.assertion.object, Literal)
            else None
        )

    @property
    def language(self) -> str | None:
        """Retain the actual language; absence does not imply a default language."""
        value = self.literal
        return value.language if value is not None else None


@dataclass(frozen=True)
class OboRecords:
    """Selected records and every relevant resolved or unresolved attachment.

    assertions retain the original bounded base-assertion prefix even when the
    operation cannot finish wrapping it into records. Completeness covers only
    the selected native convention, not OBO text parsing or OWL consistency.
    """

    records: tuple[OboRecord, ...]
    attachments: tuple[OboAttachment, ...]
    report: Report
    assertions: tuple[Edge, ...] = ()
