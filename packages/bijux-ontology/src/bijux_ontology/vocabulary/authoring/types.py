"""Explicit vocabulary intent without implicit semantic or scientific corrections."""

from dataclasses import dataclass
from typing import Literal as Choice
from typing import Protocol

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import RDF, XSD

from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import Budget, IssueCollector
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.patches.planning import Captured
from bijux_ontology.rdf.records.types import (
    PatchBlank,
    PatchQuad,
    PatchResource,
    PatchValue,
)
from bijux_ontology.rdf.terms import bound_node, literal_datatype
from bijux_ontology.vocabulary.annotations.graphs.types import (
    AnnotationLiteralRepair,
    AnnotationValueEdit,
)
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView

AnnotationValue = URIRef | BNode | Literal


def _iri(value: object, budget: Budget) -> None:
    """Require actual named RDF terms, qualifying metadata before retention."""
    if not isinstance(value, URIRef):
        raise InputError("Vocabulary authoring identifiers require URIRef terms")
    bound_node(value, budget)
    absolute_iri(value)


def _values(values: object, budget: Budget) -> tuple[AnnotationValue, ...]:
    """Own finite exact RDF term tuples without normalizing literals or blanks."""
    if type(values) is not tuple:
        raise InputError("Annotation values require immutable term tuples")
    budget.consume("states", len(values))
    budget.consume("bytes", 64 + 16 * len(values))
    result: list[AnnotationValue] = []
    for value in values:
        budget.consume("work")
        if not isinstance(value, URIRef | BNode | Literal):
            raise InputError("Annotation values require RDF terms")
        bound_node(value, budget)
        if isinstance(value, URIRef):
            absolute_iri(value)
        result.append(value)
    return tuple(result)


def _label(value: object, budget: Budget) -> None:
    """SKOS lexical labels retain their supplied string/language representation."""
    if not isinstance(value, Literal):
        raise InputError("SKOS labels require literal terms")
    bound_node(value, budget)
    if literal_datatype(value) not in (XSD.string, RDF.langString):
        raise InputError("SKOS labels require ordinary or language-tagged strings")


@dataclass(frozen=True)
class CreateSkosResource:
    """Declare a named concept/scheme only if it has no selected outgoing facts."""

    subject: URIRef
    kind: Choice["concept", "scheme"]

    def __post_init__(self) -> None:
        """Keep creation distinct from silently retagging an existing resource."""
        _iri(self.subject, Budget())
        if self.kind not in ("concept", "scheme"):
            raise InputError("Select concept or scheme creation")


@dataclass(frozen=True)
class PreferredLabelChange:
    """Replace one exact preferred label, optionally retaining it as an alternative."""

    subject: URIRef
    previous: Literal | Absent
    replacement: Literal
    demote_previous: bool = True

    def __post_init__(self) -> None:
        """Require exact replacement in one language or an absent before-state."""
        budget = Budget()
        _iri(self.subject, budget)
        _label(self.replacement, budget)
        if self.previous is not ABSENT:
            _label(self.previous, budget)
            if (self.previous.language or "").lower() != (
                self.replacement.language or ""
            ).lower():
                raise InputError("Preferred label replacement must retain its language")
        if type(self.demote_previous) is not bool:
            raise InputError("Label demotion permission must be boolean")


@dataclass(frozen=True)
class SchemeMembership:
    """Set explicit scheme membership and both top-concept directions coherently."""

    concept: URIRef
    scheme: URIRef
    member: bool = True
    top: bool = False

    def __post_init__(self) -> None:
        """A top concept must remain a member of its selected scheme."""
        budget = Budget()
        _iri(self.concept, budget)
        _iri(self.scheme, budget)
        if (
            type(self.member) is not bool
            or type(self.top) is not bool
            or self.top
            and not self.member
        ):
            raise InputError("Membership/top policies require coherent booleans")


@dataclass(frozen=True)
class DeprecateResource:
    """Set a selected resource's explicit deprecated flag with exact before-state."""

    subject: URIRef
    previous: Literal | Absent = ABSENT
    value: bool = True

    def __post_init__(self) -> None:
        """Preserve old physical spelling as a precondition, not a guessed boolean."""
        budget = Budget()
        _iri(self.subject, budget)
        if self.previous is not ABSENT:
            if not isinstance(self.previous, Literal):
                raise InputError(
                    "Deprecation preconditions require exact literal terms"
                )
            bound_node(self.previous, budget)
            if literal_datatype(self.previous) != XSD.boolean:
                raise InputError("Deprecation preconditions require boolean literals")
        if type(self.value) is not bool:
            raise InputError("Deprecation values require booleans")


@dataclass(frozen=True)
class AnnotationChange:
    """Replace an exact selected annotation value set under an accepted profile."""

    subject: URIRef
    predicate: URIRef
    previous: tuple[AnnotationValue, ...]
    values: tuple[AnnotationValue, ...]

    def __post_init__(self) -> None:
        """Capture exact RDF terms; operation checks annotation role and ownership."""
        budget = Budget()
        _iri(self.subject, budget)
        _iri(self.predicate, budget)
        _values(self.previous, budget)
        _values(self.values, budget)


@dataclass(frozen=True)
class QualifiedAnnotationChange:
    """Add or remove one finite OWL.Axiom OBO annotation and its exact qualifiers.

    Add allocates a fresh blank attachment. Remove requires an existing owned
    attachment and exclusive complete source ownership; qualifier bodies are
    never recursively deleted. Base assertions remain when removing a qualifier
    record, preserving independent unqualified or separately qualified uses.
    """

    subject: URIRef
    predicate: URIRef
    value: Literal
    qualifiers: tuple[tuple[URIRef, AnnotationValue], ...]
    action: Choice["add", "remove"] = "add"
    attachment: URIRef | BNode | None = None

    def __post_init__(self) -> None:
        """Retain one explicit attachment profile, never infer another RDF dialect."""
        budget = Budget()
        _iri(self.subject, budget)
        _iri(self.predicate, budget)
        if not isinstance(self.value, Literal):
            raise InputError("OBO annotation values require literal terms")
        bound_node(self.value, budget)
        if type(self.qualifiers) is not tuple:
            raise InputError("Qualifiers require immutable predicate/value pairs")
        budget.consume("states", len(self.qualifiers))
        for pair in self.qualifiers:
            if type(pair) is not tuple or len(pair) != 2:
                raise InputError("Qualifiers require predicate/value pairs")
            _iri(pair[0], budget)
            _values((pair[1],), budget)
        if self.action not in ("add", "remove") or (self.action == "add") != (
            self.attachment is None
        ):
            raise InputError(
                "Add requires a fresh attachment; remove requires an existing one"
            )
        if self.attachment is not None:
            if not isinstance(self.attachment, URIRef | BNode):
                raise InputError("Attachments require actual RDF resource terms")
            bound_node(self.attachment, budget)
            if isinstance(self.attachment, URIRef):
                absolute_iri(self.attachment)


VocabularyEdit = (
    CreateSkosResource
    | PreferredLabelChange
    | SchemeMembership
    | DeprecateResource
    | AnnotationChange
    | QualifiedAnnotationChange
    | AnnotationValueEdit
    | AnnotationLiteralRepair
)


class VocabularyEditContext(Protocol):
    """The exact overlay operations consumed by SKOS and annotation edits."""

    operation: Operation
    captured: Captured
    issues: IssueCollector
    children: list[Report]
    current: set[PatchQuad]
    graph_ref: PatchResource | None
    annotation_predicates: frozenset[URIRef]

    def ref(self, value: AnnotationValue) -> PatchValue:
        """Qualify an original RDF term as an owned portable reference."""
        ...

    def values(
        self, subject: PatchResource, predicate: URIRef
    ) -> tuple[PatchValue, ...]:
        """Read exact selected values from the pending overlay."""
        ...

    def outgoing(self, subject: PatchResource) -> tuple[PatchQuad, ...]:
        """Read selected outgoing assertions without materializing a graph."""
        ...

    def put(self, subject: PatchResource, predicate: URIRef, value: PatchValue) -> None:
        """Add one exact assertion inside the selected graph."""
        ...

    def drop(
        self, subject: PatchResource, predicate: URIRef, value: PatchValue
    ) -> None:
        """Remove only one explicitly selected assertion."""
        ...

    def fresh(self) -> PatchBlank:
        """Allocate a new owned attachment reference."""
        ...

    def view(self) -> VocabularyView:
        """Inspect the owned original scope through existing vocabulary indexes."""
        ...
