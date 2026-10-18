"""Exact immutable annotation statements and original OWL attachment witnesses."""

from dataclasses import dataclass
from typing import Literal as Choice

from rdflib import BNode, Literal, URIRef

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, validate_triple
from bijux_ontology.reports import Report

AnnotationResource = URIRef | BNode
AnnotationValue = URIRef | BNode | Literal


@dataclass(frozen=True)
class AnnotationStatement:
    """One original assertion; blank terms have meaning only in its owned source."""

    subject: AnnotationResource
    predicate: URIRef
    value: AnnotationValue

    def __post_init__(self) -> None:
        """Keep exact RDF roles and metadata without converting resource values."""
        budget = Budget()
        for term in (self.subject, self.predicate, self.value):
            bound_node(term, budget)
        validate_triple(self.subject, self.predicate, self.value)


@dataclass(frozen=True)
class AnnotationAttachment:
    """One original reifier, distinct even when another has equal lexical values."""

    node: AnnotationResource
    kind: URIRef | None
    annotated: AnnotationStatement | None
    statements: tuple[AnnotationStatement, ...]
    qualifiers: tuple[AnnotationStatement, ...]
    status: Choice["attached", "malformed", "unresolved"]
    references: tuple[AnnotationStatement, ...] = ()


@dataclass(frozen=True)
class AnnotationPath:
    """Original root and actual reifier chain, never an invented direct assertion."""

    root: AnnotationStatement
    attachments: tuple[AnnotationResource, ...]
    statement: AnnotationStatement


@dataclass(frozen=True)
class AnnotationGraph:
    """Selected finite annotation closure; unrelated resource bodies are excluded.

    The source retains immutable ownership and physical graph authority. Paths
    preserve all selected routes and exact attachment identities. Completeness
    concerns only this explicit annotation profile, never OWL consequences.
    """

    source: PreparedSource
    graph: URIRef | BNode | None
    roots: tuple[AnnotationStatement, ...]
    annotation_predicates: tuple[URIRef, ...]
    attachments: tuple[AnnotationAttachment, ...]
    paths: tuple[AnnotationPath, ...]
    statements: tuple[AnnotationStatement, ...]
    report: Report


@dataclass(frozen=True)
class AnnotationGraphComparison:
    """Joint RDF truth plus exact selected roots whose local closure changed."""

    changed_roots: tuple[AnnotationStatement, ...]
    uncertain_roots: tuple[AnnotationStatement, ...]
    before: AnnotationGraph
    after: AnnotationGraph
    report: Report


@dataclass(frozen=True)
class AnnotationValueEdit:
    """Replace one leaf qualifier under an exact original root/attachment path.

    No resource body or attachment is deleted. The ordinary authoring operation
    checks the entire path against its owned snapshot, pending before-state and
    shared uses before applying this exact assertion replacement.
    """

    root: AnnotationStatement
    attachments: tuple[AnnotationResource, ...]
    statement: AnnotationStatement
    replacement: AnnotationValue

    def __post_init__(self) -> None:
        """Require finite explicit witnesses and exact RDF replacement terms."""
        if not isinstance(self.root, AnnotationStatement) or not isinstance(
            self.statement, AnnotationStatement
        ):
            raise InputError("Annotation edits require typed original statements")
        if type(self.attachments) is not tuple or not self.attachments:
            raise InputError("A qualifier edit requires an immutable attachment path")
        budget = Budget()
        budget.consume("states", len(self.attachments))
        if len(self.attachments) > budget.limits.max_depth:
            raise InputError("Annotation edit path exceeds max_depth")
        for node in self.attachments:
            if not isinstance(node, URIRef | BNode):
                raise InputError("Annotation paths require RDF resource terms")
            bound_node(node, budget)
        bound_node(self.replacement, budget)
