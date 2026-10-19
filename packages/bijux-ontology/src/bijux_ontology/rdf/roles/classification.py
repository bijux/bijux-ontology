"""Finite standard RDF axiom positions, without global entity-role guessing."""

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS
from rdflib.term import Node

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, bound_text
from bijux_ontology.rdf.terms import bound_node, literal_datatype, validate_triple

from .types import Statement, TermPosition, TermRole, TermRoleOccurrence

_DECLARATIONS: dict[Node, TermRole] = {
    OWL.Class: "class",
    RDFS.Class: "class",
    OWL.Restriction: "class",
    OWL.DeprecatedClass: "class",
    OWL.DeprecatedProperty: "property",
    OWL.NegativePropertyAssertion: "structural",
    RDFS.Datatype: "datatype",
    OWL.DataRange: "datatype",
    RDF.Property: "property",
    OWL.ObjectProperty: "object_property",
    OWL.DatatypeProperty: "datatype_property",
    OWL.AnnotationProperty: "annotation_property",
    OWL.NamedIndividual: "individual",
    OWL.Ontology: "structural",
    OWL.Axiom: "structural",
    OWL.AllDisjointClasses: "structural",
    OWL.AllDisjointProperties: "structural",
    OWL.AllDifferent: "structural",
    OWL.FunctionalProperty: "property",
    OWL.InverseFunctionalProperty: "object_property",
    OWL.TransitiveProperty: "object_property",
    OWL.SymmetricProperty: "object_property",
    OWL.AsymmetricProperty: "object_property",
    OWL.ReflexiveProperty: "object_property",
    OWL.IrreflexiveProperty: "object_property",
}
_CLASS_RELATIONS = frozenset(
    (RDFS.subClassOf, OWL.equivalentClass, OWL.disjointWith, OWL.complementOf)
)
_PROPERTY_RELATIONS = frozenset(
    (
        RDFS.subPropertyOf,
        OWL.equivalentProperty,
        OWL.propertyDisjointWith,
        OWL.inverseOf,
    )
)
_CONCEPT_RELATIONS = frozenset(
    (
        SKOS.semanticRelation,
        SKOS.mappingRelation,
        SKOS.broader,
        SKOS.narrower,
        SKOS.broaderTransitive,
        SKOS.narrowerTransitive,
        SKOS.related,
        SKOS.broadMatch,
        SKOS.narrowMatch,
        SKOS.relatedMatch,
        SKOS.closeMatch,
        SKOS.exactMatch,
    )
)


def declaration_role(node: Node) -> TermRole | None:
    """Recognize only standard declaration markers, not application classes."""
    return _DECLARATIONS.get(node)


def _endpoints(
    statement: Statement,
) -> tuple[tuple[TermRole, ...], tuple[TermRole, ...]]:
    """Classify each supported axiom position independently of equal IRI spelling."""
    _, predicate, value = statement
    if predicate == RDF.type:
        declared = declaration_role(value)
        if declared is not None:
            return (declared,), ("declaration",)
        if value == SKOS.Concept:
            return ("individual", "concept"), ("class",)
        return ("individual",), (
            ("class",) if isinstance(value, URIRef | BNode) else ("unknown",)
        )
    if predicate in _CLASS_RELATIONS:
        return ("class",), ("class",)
    if predicate in _PROPERTY_RELATIONS:
        return ("property",), ("property",)
    if predicate == RDFS.domain:
        return ("property",), ("class",)
    if predicate == RDFS.range:
        return ("property",), ("unknown",)
    if predicate in _CONCEPT_RELATIONS:
        return ("individual", "concept"), ("individual", "concept")
    if predicate == SKOS.topConceptOf:
        return ("individual", "concept"), ("individual",)
    if predicate == SKOS.hasTopConcept:
        return ("individual",), ("individual", "concept")
    if predicate in (SKOS.inScheme, OWL.sameAs, OWL.differentFrom):
        return ("individual",), ("individual",)
    return ("unknown",), ("unknown",)


def classify_statement(
    statement: Statement,
    *,
    source_scope: str = "",
    graph: URIRef | BNode | None = None,
    subject_role: TermRole | None = None,
    object_role: TermRole | None = None,
    budget: Budget | None = None,
) -> tuple[TermRoleOccurrence, ...]:
    """Return finite per-position facts; custom endpoint roles must be explicit.

    Caller endpoint rules qualify otherwise unknown positions only. They cannot
    reinterpret a standard class axiom as an individual assertion. No graph or
    anonymous-expression traversal, class closure or entailment is performed.
    """
    if type(statement) is not tuple or len(statement) != 3:
        raise InputError("Role classification requires one RDF triple tuple")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Expected a shared Budget")
    operation = budget if budget is not None else Budget()
    bound_text(source_scope, operation)
    for node in statement:
        bound_node(node, operation)
    validate_triple(*statement)
    if graph is not None:
        if not isinstance(graph, URIRef | BNode):
            raise InputError("Graph role evidence requires an RDF graph identifier")
        bound_node(graph, operation)
    subject, predicate, value = statement
    first, second = _endpoints(statement)
    roles = {
        "class",
        "concept",
        "individual",
        "property",
        "object_property",
        "datatype_property",
        "annotation_property",
        "datatype",
        "structural",
        "declaration",
        "unknown",
    }
    for authored, known in ((subject_role, first), (object_role, second)):
        if authored is not None and (
            not isinstance(authored, str)
            or authored not in roles
            or known != ("unknown",)
            and authored not in known
        ):
            raise InputError(
                "Endpoint role conflicts with the supported axiom position"
            )
    result: list[TermRoleOccurrence] = []

    def retain(term: Node, position: TermPosition, role: TermRole) -> None:
        """Reserve each record before retaining its original term references."""
        operation.consume("work")
        operation.consume("states")
        operation.consume("bytes", 160)
        if isinstance(term, Literal) and role not in ("unknown", "structural"):
            role = "unknown"
        result.append(
            TermRoleOccurrence(term, position, role, statement, source_scope, graph)
        )

    for role in (subject_role,) if subject_role is not None else first:
        retain(subject, "subject", role)
    retain(
        predicate,
        "predicate",
        "structural" if first != ("unknown",) or second != ("unknown",) else "property",
    )
    for role in (object_role,) if object_role is not None else second:
        retain(value, "object", role)
    if isinstance(value, Literal):
        retain(literal_datatype(value), "datatype", "datatype")
    if graph is not None:
        retain(graph, "graph", "unknown")
    return tuple(result)
