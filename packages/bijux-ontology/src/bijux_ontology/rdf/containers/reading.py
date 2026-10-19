"""Bounded original container occurrences, without closed-world RDF assumptions."""

from collections.abc import Sequence
from typing import cast

from rdflib import BNode, Graph, URIRef
from rdflib.namespace import RDF, RDFS
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.rdf.patches.planning import capture_source
from bijux_ontology.rdf.patches.types import PatchUnavailable
from bijux_ontology.rdf.roles.types import Statement
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_node, term_key
from bijux_ontology.rdf.transforms import _authorized_contexts, _budget

from .types import ContainerGap, ContainerInspection, ContainerRecord, ContainerSlot

_KINDS = (RDF.Seq, RDF.Bag, RDF.Alt, RDFS.Container)
_PREFIX = str(RDF) + "_"


def _ordinal(predicate: Node, budget: Budget) -> str | None:
    """Recognize the specified positive ASCII decimal spelling, never parse an int."""
    bound_node(predicate, budget)
    if not isinstance(predicate, URIRef):
        return None
    budget.consume("work", len(predicate) + 1)
    if not predicate.startswith(_PREFIX):
        return None
    budget.consume("bytes", len(predicate))
    digits = predicate[len(_PREFIX) :]
    if (
        not digits
        or digits[0] not in "123456789"
        or any(c < "0" or c > "9" for c in digits)
    ):
        return None
    return digits


def _successor(value: str, budget: Budget) -> str:
    """Bound one decimal-text increment solely to recognize adjacency."""
    budget.consume("work", 2 * len(value) + 1)
    budget.consume("bytes", 3 * len(value) + 3)
    prefix = value.rstrip("9")
    if not prefix:
        return "1" + "0" * len(value)
    return prefix[:-1] + chr(ord(prefix[-1]) + 1) + "0" * (len(value) - len(prefix))


def _record(
    node: URIRef | BNode, graph: Graph, name: URIRef | BNode | None, budget: Budget
) -> ContainerRecord | None:
    """Retain exact outgoing data, then freeze numeric slots and compact gaps."""
    statements: list[Statement] = []
    annotations: list[Statement] = []
    kinds: dict[URIRef, None] = {}
    members: dict[str, dict[tuple[str | None, ...], Node]] = {}
    assertions: dict[str, list[Statement]] = {}
    predicates: dict[str, URIRef] = {}
    for triple in graph.triples((node, None, None)):
        budget.consume("triples")
        budget.consume("work")
        for term in triple:
            bound_node(term, budget)
        budget.consume("states", 3)
        budget.consume("bytes", 128)
        statements.append(triple)
        ordinal = _ordinal(triple[1], budget)
        if ordinal is not None:
            members.setdefault(ordinal, {}).setdefault(term_key(triple[2]), triple[2])
            assertions.setdefault(ordinal, []).append(triple)
            predicates[ordinal] = cast(URIRef, triple[1])
        elif triple[1] == RDF.type and triple[2] in _KINDS:
            kinds[cast(URIRef, triple[2])] = None
        else:
            annotations.append(triple)
    if not statements:
        return None
    budget.consume(
        "work", sum(len(n) + 1 for n in members) * max(1, len(members).bit_length())
    )
    budget.consume("states", 2 * len(members) + 1)
    budget.consume("bytes", 128 + 64 * len(members))
    ordered = sorted(members, key=lambda n: (len(n), n))
    slots: list[ContainerSlot] = []
    gaps: list[ContainerGap] = []
    previous: str | None = None
    for ordinal in ordered:
        budget.consume("work")
        if (previous is None and ordinal != "1") or (
            previous is not None and _successor(previous, budget) != ordinal
        ):
            gaps.append(ContainerGap(previous, ordinal))
        slots.append(
            ContainerSlot(
                ordinal,
                predicates[ordinal],
                tuple(members[ordinal].values()),
                tuple(assertions[ordinal]),
            )
        )
        previous = ordinal
    return ContainerRecord(
        node,
        name,
        tuple(kinds),
        tuple(slots),
        tuple(gaps),
        tuple(statements),
        tuple(annotations),
    )


def _discover(graph: Graph, budget: Budget) -> tuple[URIRef | BNode, ...]:
    """Scan explicit predicate occurrences once; membership IRIs form an open family."""
    nodes: dict[URIRef | BNode, None] = {}
    for s, p, o in graph:
        budget.consume("work")
        budget.consume("triples")
        bound_node(s, budget)
        if _ordinal(p, budget) is not None or p == RDF.type and o in _KINDS:
            if not isinstance(s, URIRef | BNode):
                raise InputError("Container subjects require RDF resource terms")
            if s not in nodes:
                budget.consume("states")
                budget.consume("bytes", 32)
                nodes[s] = None
    return tuple(nodes)


def inspect_containers(
    source: PreparedSource,
    *,
    nodes: Sequence[URIRef | BNode] | None = None,
    graph_name: GraphSelection | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ContainerInspection:
    """Expose original numbered membership, not an inferred complete container.

    Ordinals are canonical decimal text ordered by numeric magnitude, never
    expanded into missing-member ranges. Physical graph occurrences stay apart.
    Equal literal aliases occupy one RDF value; equal values in different slots
    retain multiplicity. Unknown RDF predicates remain attached ordinary data.
    """
    operation = _budget(limits, budget)
    issues = IssueCollector(operation)
    records: list[ContainerRecord] = []
    missing: list[URIRef | BNode] = []
    owner = None
    complete = False
    try:
        if not isinstance(source, PreparedSource):
            raise InputError("Container inspection requires an owned source")
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
            "container:",
            operation,
            graph_name=graph_name,
            project_graph=graph_name is not None,
        )
        selected = None
        if nodes is not None:
            items: list[URIRef | BNode] = []
            seen: set[tuple[str | None, ...]] = set()
            for value in capture_items(nodes, object, operation):
                if not isinstance(value, URIRef | BNode):
                    raise InputError("Container selectors require RDF resource objects")
                bound_node(value, operation)
                if isinstance(value, BNode):
                    captured.reference(value)
                key = term_key(value)
                if key in seen:
                    raise InputError("Container selectors must be distinct RDF terms")
                operation.consume("states", 2)
                operation.consume("bytes", 32)
                seen.add(key)
                items.append(value)
            selected = tuple(items)
        owner = source
        found: set[URIRef | BNode] = set()
        for name, graph in _authorized_contexts(
            source, operation, graph_name=graph_name
        ):
            if name is not None:
                bound_node(name, operation)
            for node in _discover(graph, operation) if selected is None else selected:
                operation.consume("work")
                operation.consume("states", 2)
                operation.consume("bytes", 32)
                record = _record(node, graph, name, operation)
                if record is not None:
                    records.append(record)
                    found.add(node)
        if selected is not None:
            for node in selected:
                operation.consume("work")
                if node not in found:
                    operation.consume("states")
                    operation.consume("bytes", 16)
                    missing.append(node)
        complete = True
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except (InputError, PatchUnavailable) as error:
        try:
            issues.append(
                Issue(
                    "rdf.container_unverified",
                    str(error),
                    kind="unverified"
                    if isinstance(error, PatchUnavailable)
                    else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    checked = len(records) + len(missing)
    return ContainerInspection(
        owner,
        tuple(records),
        tuple(missing),
        Report(
            "rdf.container_inspection",
            checked,
            tuple(issues),
            allow_empty=True,
            requested=checked if complete else checked + 1,
            complete=complete,
            regime="asserted-physical-container-membership",
        ),
    )
