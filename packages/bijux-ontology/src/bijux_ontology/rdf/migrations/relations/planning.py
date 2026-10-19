"""Exact physical relation occurrences become existing checked RDF patch records."""

from dataclasses import replace

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import OWL, RDF

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.patches.planning import Captured, Rejected
from bijux_ontology.rdf.patches.types import (
    DatasetPatch,
    PatchBlank,
    PatchLiteral,
    PatchQuad,
    PatchResource,
    PatchUnavailable,
    PatchValue,
    bound_term,
    validate_witness,
)
from bijux_ontology.rdf.terms import bound_node, literal_datatype

from .types import (
    BinaryOccurrence,
    RelationOccurrence,
    RelationPattern,
    RelationRole,
    Value,
)

Request = BinaryOccurrence | RelationOccurrence


def reserve(budget: Budget, count: int = 1) -> None:
    """Reserve finite vector/set/record workspace before constructing its values."""
    budget.consume("states", count)
    budget.consume("bytes", count * 256)


class RelationRejected(Rejected):
    """Identify the exact original request whose declared preservation failed."""

    def __init__(self, error: Rejected, index: int) -> None:
        """Retain the rejected request position and its stable diagnostic."""
        super().__init__(error.code, str(error))
        self.index = index


def _value(value: Value, captured: Captured, budget: Budget) -> PatchValue:
    """Preserve exact lexical RDF terms and require original anonymous authority."""
    bound_node(value, budget)
    if isinstance(value, BNode):
        return captured.reference(value)
    if isinstance(value, Literal):
        return PatchLiteral(str(value), str(literal_datatype(value)), value.language)
    return URIRef(value)


def _resource(
    value: URIRef | BNode, captured: Captured, budget: Budget
) -> PatchResource:
    """Bind only supplied source blanks rather than another node's equal label."""
    result = _value(value, captured, budget)
    assert isinstance(result, URIRef | PatchBlank)
    return result


def _quad(
    subject: PatchResource,
    predicate: URIRef,
    value: PatchValue,
    graph: PatchResource | None,
    budget: Budget,
) -> PatchQuad:
    """Prepay constructor scalar copies and retain exact RDF positions."""
    reserve(budget)
    for term in (subject, predicate, value, graph):
        if term is not None:
            bound_term(term, budget)
    return PatchQuad(subject, predicate, value, graph)


def _role(
    event: PatchResource,
    participant: PatchValue,
    role: RelationRole,
    graph: PatchResource | None,
    budget: Budget,
) -> PatchQuad:
    """Apply the supplied orientation instead of guessing a distinguished owner."""
    if role.direction == "outgoing":
        return _quad(event, role.predicate, participant, graph, budget)
    if not isinstance(participant, URIRef | PatchBlank):
        raise Rejected(
            "rdf.relation_role_kind",
            "An incoming role requires a resource participant, not a literal subject",
        )
    return _quad(participant, role.predicate, event, graph, budget)


def _graph(
    request: Request, captured: Captured, budget: Budget
) -> PatchResource | None:
    """None means one default context; every named/blank selector is explicit."""
    graph = (
        None if request.graph is None else _resource(request.graph, captured, budget)
    )
    base = captured.witness
    budget.consume("work", len(base.graphs) + 1)
    if (
        base.kind == "named"
        and graph != base.graph_selection
        or graph is not None
        and graph not in base.graphs
    ):
        raise Rejected(
            "rdf.relation_graph_scope",
            "The requested physical graph is outside the authorized snapshot",
        )
    return graph


def _uses(quad: PatchQuad, event: PatchResource, event_text: str | None) -> bool:
    """Include incoming, predicate, graph and intrinsic literal datatype uses."""
    return (
        event in (quad.subject, quad.predicate, quad.value, quad.graph)
        or isinstance(event, URIRef)
        and isinstance(quad.value, PatchLiteral)
        and quad.value.datatype == event_text
    )


def _expand(
    request: BinaryOccurrence,
    pattern: RelationPattern,
    captured: Captured,
    graph: PatchResource | None,
    budget: Budget,
) -> tuple[tuple[PatchQuad, ...], tuple[PatchQuad, ...]]:
    """Replace one present edge with an independently allocated positive event."""
    bound_node(request.event, budget)
    event = URIRef(request.event)
    event_text = str(event)
    if event in (
        RDF.type,
        pattern.predicate,
        pattern.relation_class,
        pattern.subject_role.predicate,
        pattern.object_role.predicate,
    ):
        raise Rejected(
            "rdf.relation_identity_collision",
            "A relation event cannot reuse the declared vocabulary identity",
        )
    for quad in captured.witness.quads:
        budget.consume("work")
        if _uses(quad, event, event_text):
            raise Rejected(
                "rdf.relation_identity_collision",
                "The caller event IRI already occurs in the authorized source",
            )
    budget.consume("work", len(captured.witness.graphs))
    if event in captured.witness.graphs:
        raise Rejected(
            "rdf.relation_identity_collision",
            "The caller event IRI already names a supplied graph",
        )
    subject = _resource(request.subject, captured, budget)
    value = _value(request.value, captured, budget)
    original = _quad(subject, pattern.predicate, value, graph, budget)
    budget.consume("work", len(captured.witness.quads))
    if original not in captured.witness.quads:
        raise Rejected(
            "rdf.relation_occurrence_absent",
            "The exact selected binary edge is absent in this physical graph",
        )
    reserve(budget, 4 + len(request.qualifiers))
    output = [
        _quad(event, RDF.type, pattern.relation_class, graph, budget),
        _role(event, subject, pattern.subject_role, graph, budget),
        _role(event, value, pattern.object_role, graph, budget),
    ]
    predicates = (
        RDF.type,
        pattern.subject_role.predicate,
        pattern.object_role.predicate,
    )
    seen: set[PatchQuad] = set()
    for qualifier in request.qualifiers:
        budget.consume("work")
        bound_node(qualifier.predicate, budget)
        if qualifier.predicate in predicates:
            raise Rejected(
                "rdf.relation_qualifier_role_collision",
                "Qualifiers cannot overwrite class or declared participant roles",
            )
        edge = _quad(
            event,
            qualifier.predicate,
            _value(qualifier.value, captured, budget),
            graph,
            budget,
        )
        if edge in seen:
            raise Rejected(
                "rdf.relation_qualifier_duplicate",
                "One source occurrence repeats an indistinguishable qualifier",
            )
        seen.add(edge)
        output.append(edge)
    return (original,), tuple(output)


def _participant(
    event: PatchResource,
    role: RelationRole,
    graph: PatchResource | None,
    captured: Captured,
    budget: Budget,
) -> tuple[PatchValue, PatchQuad]:
    """Require one actual declared role in the selected finite physical scope."""
    match: tuple[PatchValue, PatchQuad] | None = None
    for quad in captured.witness.quads:
        budget.consume("work")
        if quad.graph != graph or quad.predicate != role.predicate:
            continue
        value = None
        if role.direction == "outgoing" and quad.subject == event:
            value = quad.value
        elif role.direction == "incoming" and quad.value == event:
            value = quad.subject
        if value is not None:
            if match is not None:
                raise Rejected(
                    "rdf.relation_role_cardinality",
                    "A declared relation role has more than one participant",
                )
            reserve(budget)
            match = value, quad
    if match is None:
        raise Rejected(
            "rdf.relation_role_cardinality",
            "A required declared relation role has no supplied participant",
        )
    return match


def _collapse(
    request: RelationOccurrence,
    pattern: RelationPattern,
    captured: Captured,
    graph: PatchResource | None,
    budget: Budget,
) -> tuple[tuple[PatchQuad, ...], tuple[PatchQuad, ...]]:
    """Only isolated unqualified positive events can lose their separate identity."""
    event = _resource(request.event, captured, budget)
    event_text = str(event) if isinstance(event, URIRef) else None
    if event in (
        RDF.type,
        pattern.predicate,
        pattern.relation_class,
        pattern.subject_role.predicate,
        pattern.object_role.predicate,
    ):
        raise Rejected(
            "rdf.relation_identity_collision",
            "An event also naming declared vocabulary cannot discard its identity",
        )
    declared = _quad(event, RDF.type, pattern.relation_class, graph, budget)
    budget.consume("work", len(captured.witness.quads))
    if declared not in captured.witness.quads:
        raise Rejected(
            "rdf.relation_occurrence_absent",
            "The selected event lacks its explicitly declared relation class",
        )
    for quad in captured.witness.quads:
        budget.consume("work")
        if (
            quad.subject == event
            and quad.predicate == RDF.type
            and quad.value == OWL.NegativePropertyAssertion
        ):
            raise Rejected(
                "rdf.relation_negative_unrepresentable",
                "A qualified OWL negative assertion cannot become a positive edge",
            )
    subject, subject_edge = _participant(
        event, pattern.subject_role, graph, captured, budget
    )
    value, value_edge = _participant(
        event, pattern.object_role, graph, captured, budget
    )
    if not isinstance(subject, URIRef | PatchBlank):
        raise Rejected(
            "rdf.relation_role_kind", "A binary subject requires a resource participant"
        )
    if (
        subject == event
        or value == event
        or isinstance(value, PatchLiteral)
        and value.datatype == event_text
    ):
        raise Rejected(
            "rdf.relation_occurrence_shared",
            "Event participation or datatype use requires retaining its identity",
        )
    reserve(budget, 3)
    managed = (declared, subject_edge, value_edge)
    for quad in captured.witness.quads:
        budget.consume("work")
        if quad in managed or not _uses(quad, event, event_text):
            continue
        if quad.subject == event and quad.graph == graph:
            raise Rejected(
                "rdf.relation_type_loss"
                if quad.predicate == RDF.type
                else "rdf.relation_qualifier_loss",
                "An additional class or qualifier has no binary representation",
            )
        raise Rejected(
            "rdf.relation_occurrence_shared",
            "An incoming or other physical-scope use requires retaining event identity",
        )
    budget.consume("work", len(captured.witness.graphs))
    if event in captured.witness.graphs:
        raise Rejected(
            "rdf.relation_occurrence_shared",
            "An event also naming a graph cannot discard its identity",
        )
    output = _quad(subject, pattern.predicate, value, graph, budget)
    budget.consume("work", len(captured.witness.quads))
    if output in captured.witness.quads:
        raise Rejected(
            "rdf.relation_occurrence_collision",
            "The binary edge already exists and cannot retain another event occurrence",
        )
    return managed, (output,)


def plan_relations(
    captured: Captured,
    pattern: RelationPattern,
    requests: tuple[Request, ...],
    budget: Budget,
) -> DatasetPatch:
    """Account for every original request without merging distinct event occurrences."""
    for value in (
        pattern.predicate,
        pattern.relation_class,
        pattern.subject_role.predicate,
        pattern.object_role.predicate,
    ):
        bound_node(value, budget)
    if (
        pattern.assertion != "positive"
        or pattern.relation_class == OWL.NegativePropertyAssertion
    ):
        raise PatchUnavailable(
            "Negative relation representations cannot become positive edges"
        )
    if not requests:
        raise InputError("Relation migration requires explicit source occurrences")
    reserve(budget, len(captured.witness.quads) + len(requests) * 3)
    removals: set[PatchQuad] = set()
    additions: set[PatchQuad] = set()
    identities: set[URIRef] = set()
    for index, request in enumerate(requests):
        budget.consume("work")
        try:
            graph = _graph(request, captured, budget)
            if isinstance(request, BinaryOccurrence):
                bound_node(request.event, budget)
                if request.event in identities:
                    raise Rejected(
                        "rdf.relation_identity_collision",
                        "Allocate a distinct event IRI for each source occurrence",
                    )
                identities.add(request.event)
                removed, added = _expand(request, pattern, captured, graph, budget)
            else:
                removed, added = _collapse(request, pattern, captured, graph, budget)
            reserve(budget, len(removed) + len(added))
            if any(quad in removals for quad in removed):
                raise Rejected(
                    "rdf.relation_occurrence_overlap",
                    "Original occurrence selections overlap",
                )
            if any(quad in additions for quad in added):
                raise Rejected(
                    "rdf.relation_occurrence_collision",
                    "Distinct relation occurrences would collapse to one binary edge",
                )
            removals.update(removed)
            additions.update(added)
        except Rejected as error:
            raise RelationRejected(error, index) from error
    base = captured.witness
    reserve(budget, 3 * (len(base.quads) + len(additions)) + len(base.graphs))
    budget.consume("work", len(base.quads) + len(additions))
    output = tuple(quad for quad in base.quads if quad not in removals) + tuple(
        additions
    )
    for quad in output:
        for term in (quad.subject, quad.predicate, quad.value, quad.graph):
            if term is not None:
                bound_term(term, budget)
    target = replace(base, quads=output)
    validate_witness(base, budget)
    validate_witness(target, budget)
    return DatasetPatch(base, target, tuple(removals), tuple(additions))
