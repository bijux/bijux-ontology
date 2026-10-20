"""Bounded original RDF reification inspection over authorized physical graphs."""

from collections.abc import Sequence
from typing import Literal as Choice

from rdflib import BNode, Graph, URIRef
from rdflib.namespace import RDF
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.rdf.patches.planning import capture_source
from bijux_ontology.rdf.records.types import PatchUnavailable
from bijux_ontology.rdf.reification.types import (
    ReificationInspection,
    ReificationRecord,
)
from bijux_ontology.rdf.roles.types import Statement
from bijux_ontology.rdf.sources.selection import selected_contexts
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_node, term_key
from bijux_ontology.rdf.transforms import _budget

_COMPONENTS = (RDF.subject, RDF.predicate, RDF.object)


def _record(
    node: URIRef | BNode,
    name: URIRef | BNode | None,
    graph: Graph,
    budget: Budget,
) -> ReificationRecord | None:
    """Read actual outgoing assertions once, preserving every malformed component."""
    statements: list[Statement] = []
    annotations: list[Statement] = []
    components: list[dict[tuple[str | None, ...], Node]] = [{}, {}, {}]
    typed = False
    for triple in graph.triples((node, None, None)):
        budget.consume("triples")
        budget.consume("work")
        for term in triple:
            bound_node(term, budget)
        budget.consume("states")
        budget.consume("bytes", 96)
        statements.append(triple)
        predicate, value = triple[1:]
        if predicate in _COMPONENTS:
            components[_COMPONENTS.index(predicate)].setdefault(term_key(value), value)
        elif predicate == RDF.type and value == RDF.Statement:
            typed = True
        else:
            annotations.append(triple)
    if not statements:
        return None
    subjects, predicates, objects = (tuple(values.values()) for values in components)
    counts = (len(subjects), len(predicates), len(objects))
    valid_roles = all(isinstance(term, URIRef | BNode) for term in subjects) and all(
        isinstance(term, URIRef) for term in predicates
    )
    status: Choice["invalid_roles", "ambiguous", "incomplete", "complete"] = (
        "invalid_roles"
        if not valid_roles
        else "ambiguous"
        if any(n > 1 for n in counts)
        else "incomplete"
        if any(n == 0 for n in counts)
        else "complete"
    )
    statement = (
        (subjects[0], predicates[0], objects[0]) if status == "complete" else None
    )
    budget.consume("states")
    budget.consume("bytes", 128)
    return ReificationRecord(
        node,
        name,
        subjects,
        predicates,
        objects,
        typed,
        tuple(statements),
        tuple(annotations),
        statement,
        status,
    )


def _discover(graph: Graph, budget: Budget) -> tuple[URIRef | BNode, ...]:
    """Use maintained RDF indices; only actual stored subjects become candidates."""
    result: dict[URIRef | BNode, None] = {}
    for predicate, value in (
        (RDF.type, RDF.Statement),
        *((p, None) for p in _COMPONENTS),
    ):
        for subject, _, _ in graph.triples((None, predicate, value)):
            budget.consume("work")
            budget.consume("triples")
            bound_node(subject, budget)
            if not isinstance(subject, URIRef | BNode):
                raise InputError("Reification subjects require RDF resource terms")
            if subject not in result:
                budget.consume("states")
                budget.consume("bytes", 32)
                result[subject] = None
    return tuple(result)


def inspect_reifications(
    source: PreparedSource,
    *,
    nodes: Sequence[URIRef | BNode] | None = None,
    graph_name: GraphSelection | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ReificationInspection:
    """Inspect selected exact records without asserting their described statements.

    Unpinned datasets retain separate physical occurrences, including blank graph
    names. An explicit graph_name narrows that scope through existing owned capture.
    Union, normalized literal fidelity and incomplete graph inventory are unknown.
    Explicit blank selectors must be actual objects owned by the selected snapshot.
    Missing components and repeated bindings are raw data here, not RDF errors.
    """
    operation = _budget(limits, budget)
    issues = IssueCollector(operation)
    records: list[ReificationRecord] = []
    missing: list[URIRef | BNode] = []
    owner = None
    completed = False
    try:
        if not isinstance(source, PreparedSource):
            raise InputError("Reification inspection requires an owned PreparedSource")
        for text in (
            source.document_iri,
            source.effective_url,
            source.graph_name,
            source.literal_fidelity,
            source.graph_fidelity,
        ):
            if text is not None:
                bound_text(text, operation)
        captured = capture_source(
            source,
            "reifier:",
            operation,
            graph_name=graph_name,
            project_graph=graph_name is not None,
        )
        selected = None
        if nodes is not None:
            selected_list: list[URIRef | BNode] = []
            keys: set[tuple[str | None, ...]] = set()
            for node in capture_items(nodes, object, operation):
                if not isinstance(node, URIRef | BNode):
                    raise InputError(
                        "Reification selectors require RDF resource objects"
                    )
                bound_node(node, operation)
                if isinstance(node, BNode):
                    captured.reference(node)
                key = term_key(node)
                if key in keys:
                    raise InputError("Reification selectors must be distinct RDF terms")
                operation.consume("states")
                operation.consume("bytes", 32)
                keys.add(key)
                selected_list.append(node)
            selected = tuple(selected_list)
        owner = source
        seen: set[URIRef | BNode] = set()
        for name, graph in selected_contexts(source, operation, graph_name=graph_name):
            if name is not None:
                bound_node(name, operation)
            candidates = _discover(graph, operation) if selected is None else selected
            for node in candidates:
                operation.consume("work")
                operation.consume("states")
                operation.consume("bytes", 32)
                record = _record(node, name, graph, operation)
                if record is not None:
                    records.append(record)
                    seen.add(node)
        if selected is not None:
            for node in selected:
                operation.consume("work")
                if node not in seen:
                    operation.consume("states")
                    operation.consume("bytes", 16)
                    missing.append(node)
        completed = True
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except (PatchUnavailable, InputError) as error:
        try:
            issues.append(
                Issue(
                    "rdf.reification_unverified",
                    str(error),
                    kind="unverified"
                    if isinstance(error, PatchUnavailable)
                    else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    checked = len(records) + len(missing)
    return ReificationInspection(
        owner,
        tuple(records),
        tuple(missing),
        Report(
            "rdf.reification_inspection",
            checked,
            tuple(issues),
            allow_empty=True,
            requested=checked if completed else checked + 1,
            complete=completed,
            regime="asserted-physical-reification-inspection",
        ),
    )
