"""Copy explicit path witnesses and requested descriptions within one graph."""

import time
from collections.abc import Sequence
from dataclasses import replace
from threading import Event
from typing import cast

from rdflib import BNode, Dataset, Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD
from rdflib.term import Node

from bijux_ontology.data.projection.reading import project_records
from bijux_ontology.data.projection.types import ReadBinding
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.path_traversal import finite_path_steps, path_values
from bijux_ontology.rdf.paths import capture_path
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.structure import _list_slot
from bijux_ontology.rdf.terms import bound_node, literal_term, term_key
from bijux_ontology.reports import Issue, Report

from .types import RecordExtraction, RecordField, RecordReference


def _reserve(budget: Budget, count: int = 1) -> None:
    """Reserve traversal and output inventories before retaining records."""
    budget.consume("states", count)
    budget.consume("bytes", 128 * count)


def _key(node: Node, budget: Budget) -> tuple[str | None, ...]:
    """Qualify exact lexical term fields before hashing their identity."""
    bound_node(node, budget)
    key = term_key(node)
    budget.consume("work", 1 + sum(len(part or "") for part in key))
    return key


def _fields(
    fields: Sequence[RecordField], budget: Budget, active: frozenset[int] = frozenset()
) -> tuple[RecordField, ...]:
    """Own a bounded acyclic description tree, rejecting unsupported paths."""
    if len(active) >= min(budget.limits.max_depth, 64):
        raise LimitExceeded("Record description nesting exceeds its depth bound")
    result = []
    names = set()
    for item in capture_items(fields, RecordField, budget):
        if id(item) in active:
            raise InputError("Recursive record plans are outside the finite profile")
        bound_text(item.read.name, budget)
        if item.read.name in names:
            raise InputError("Sibling record field names must be distinct")
        _reserve(budget, 2)
        names.add(item.read.name)
        path = capture_path(item.read.path, budget=budget)
        steps = finite_path_steps(path, budget)
        if steps is None:
            raise InputError("Record extraction supports finite sequence/inverse paths")
        children = (
            _fields(item.children, budget, active | {id(item)}) if item.children else ()
        )
        result.append(RecordField(replace(item.read, path=path), children))
    if not result:
        raise InputError("Record extraction requires nonempty field selections")
    return tuple(result)


def _copy_path(
    graph: Graph,
    target: Graph,
    subject: Node,
    field: RecordField,
    budget: Budget,
) -> None:
    """Retain actual edges on successful finite paths, including inverse edges.

    Endpoint evaluation reuses the ordinary exact native path operation. A
    backward pass removes dead prefixes, so failed branches are not descriptions.
    """
    steps = finite_path_steps(capture_path(field.read.path, budget=budget), budget)
    assert steps is not None
    current = {_key(subject, budget): subject}
    levels = []
    for step in steps:
        following = {}
        edges = []
        predicate, inverse = step
        for start_key, node in current.items():
            valid = set()
            for end in path_values(graph, node, (step,), budget):
                end_key = _key(end, budget)
                _reserve(budget, 3)
                valid.add(end_key)
                following[end_key] = end
            # Scan each actual adjacency once, retaining source representation
            # while respecting the native RDF 1.1 string-identity join.
            lookups: tuple[Node, ...] = (node,)
            if (
                inverse
                and isinstance(node, Literal)
                and not node.language
                and node.datatype in (None, XSD.string)
            ):
                budget.consume("work", len(node) + 1)
                budget.consume("bytes", 4 * len(node) + 128)
                _reserve(budget, 2)
                alternate = literal_term(
                    str(node),
                    datatype=str(XSD.string) if node.datatype is None else None,
                )
                lookups = (node, alternate)
            for lookup in lookups:
                pattern = (
                    (None, predicate, lookup) if inverse else (lookup, predicate, None)
                )
                for triple in graph.triples(pattern):
                    budget.consume("work")
                    budget.consume("triples")
                    end_key = _key(triple[0] if inverse else triple[2], budget)
                    if end_key in valid:
                        _reserve(budget)
                        edges.append((start_key, end_key, triple))
        _reserve(budget)
        levels.append(edges)
        current = following
    _reserve(budget, len(current))
    viable = set(current)
    for edges in reversed(levels):
        preceding = set()
        for start_key, end_key, triple in edges:
            budget.consume("work")
            if end_key in viable:
                _reserve(budget, 2)
                target.add(triple)
                preceding.add(start_key)
        viable = preceding


def _copy_list(graph: Graph, target: Graph, head: Node, budget: Budget) -> None:
    """Copy only qualified first/rest topology, preserving shared existing cells."""
    current = head
    visited: set[Node] = set()
    while current != RDF.nil:
        budget.consume("work")
        if not isinstance(current, URIRef | BNode) or current in visited:
            raise InputError("An extracted list must be a finite resource chain")
        _reserve(budget)
        visited.add(current)
        first = _list_slot(graph, current, RDF.first, budget)
        rest = _list_slot(graph, current, RDF.rest, budget)
        if len(first) != 1 or len(rest) != 1:
            raise InputError("An extracted list requires unique first/rest slots")
        for predicate in (RDF.first, RDF.rest):
            for triple in graph.triples((current, predicate, None)):
                budget.consume("triples")
                budget.consume("work")
                for node in triple:
                    bound_node(node, budget)
                _reserve(budget)
                target.add(triple)
        current = rest[0]


def extract_records(
    source: PreparedSource | Graph,
    roots: Sequence[URIRef | BNode],
    fields: Sequence[RecordField],
    *,
    graph_name: GraphSelection | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> RecordExtraction:
    """Extract a finite raw record plan in one physical graph without inference.

    Children explicitly authorize reference expansion. A resource without child
    fields stays reference-only even when its description exists in another
    graph. Finite sequence/inverse paths and ordered lists are supported; path
    repetition and recursive description plans remain required unknown. The
    result does not assert SHACL conformance or ontology module conservativity.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    reports: list[Report] = []
    references: list[RecordReference] = []
    output = None
    checked = 0
    requested = 1
    finished = False
    try:
        if not isinstance(source, PreparedSource | Graph):
            raise InputError(
                "Record extraction requires owned RDF or an ordinary Graph"
            )
        prepared = (
            source
            if isinstance(source, PreparedSource)
            else prepare_rdf(source, budget=operation)
        )
        metadata = SnapshotMetadata.capture(prepared, operation)
        if metadata.union or metadata.graph_fidelity != "complete":
            raise InputError("Record extraction requires one complete physical graph")
        if prepared.literal_fidelity not in ("source_terms", "supplied_terms"):
            raise InputError("Record extraction requires original RDF term identity")
        if isinstance(graph_name, BNode) and not any(
            name is graph_name for name in prepared._graph_names(budget=operation)
        ):
            raise InputError("Blank graph selection requires exact snapshot ownership")
        contexts = prepared._contexts(graph_name, budget=operation)
        _, name, graph = next(contexts)
        if next(contexts, None) is not None:
            raise InputError("Record extraction requires one physical graph")
        plan = _fields(fields, operation)
        captured = capture_items(
            roots, cast(type[URIRef | BNode], URIRef | BNode), operation
        )
        if not captured:
            raise InputError("Record extraction requires explicit nonempty roots")
        requested = len(captured)
        dataset = Dataset()
        target = dataset.default_graph if name is None else dataset.graph(name)
        _reserve(operation)
        # The plan is acyclic; graph cycles therefore consume only the finite
        # requested path/description depth. Shared descriptions are checked once.
        visited: set[tuple[tuple[str | None, ...], int]] = set()

        def visit(
            root: URIRef | BNode,
            subject: URIRef | BNode,
            selected: tuple[RecordField, ...],
            where: str,
        ) -> None:
            """Run ordinary public field checks before copying their native evidence."""
            operation.consume("work")
            key = (_key(subject, operation), id(selected))
            if key in visited:
                return
            _reserve(operation)
            visited.add(key)
            remaining = operation.deadline - time.monotonic()
            if remaining <= 0:
                raise LimitExceeded("Record extraction exceeded its deadline")
            _reserve(operation)
            read = project_records(
                prepared,
                (ReadBinding(where, subject, tuple(item.read for item in selected)),),
                graph_name=graph_name,
                budget=operation,
                timeout=remaining,
                cancel=cancel,
            )
            reports.append(replace(read.report, scope=where))
            if not read.report.verified:
                return
            record = read.records[0]
            for item, values in zip(selected, record.fields, strict=True):
                _copy_path(graph, target, subject, item, operation)
                if item.read.mode == "list":
                    for head in values.endpoints:
                        _copy_list(graph, target, head, operation)
                for index, node in enumerate(values.terms):
                    if not isinstance(node, URIRef | BNode):
                        if item.children:
                            issues.append(
                                Issue(
                                    "extraction.reference_invalid",
                                    "A requested child description requires a resource",
                                    where + "/" + item.read.name,
                                )
                            )
                        continue
                    _reserve(operation)
                    operation.consume(
                        "bytes", 4 * (len(where) + len(item.read.name) + 32)
                    )
                    location = where + "/" + item.read.name + "/" + str(index)
                    references.append(
                        RecordReference(
                            prepared,
                            root,
                            location,
                            node,
                            name,
                            "included" if item.children else "reference_only",
                        )
                    )
                    if item.children:
                        visit(root, node, item.children, location)

        for index, root in enumerate(captured):
            visit(root, root, plan, "record[" + str(index) + "]")
            checked += 1
        operation.consume("work", 0)
        finished = True
        if not issues and all(report.verified for report in reports):
            # The public constructor establishes fresh blank ownership while
            # preserving sharing, graph roles and exact literal lexical forms.
            _reserve(operation, 4 * len(target) + 8)
            for triple in target:
                operation.consume("work")
                for node in triple:
                    bound_node(node, operation)
            output = PreparedSource(
                dataset,
                is_dataset=prepared.is_dataset,
                document_iri=metadata.document_iri,
                effective_url=metadata.effective_url,
                literal_fidelity=metadata.literal_fidelity,
                graph_fidelity="complete",
                graph_name=str(name)
                if isinstance(name, URIRef)
                else "@default"
                if prepared.is_dataset and name is None
                else None,
                max_triples=operation.limits.max_triples,
            )
            if isinstance(name, BNode):
                # Exactly one selected named context was copied. Its new owner
                # supplies the fresh graph term; an input label is not a binding.
                names = tuple(output._graph_names(budget=operation))
                if len(names) != 1:
                    raise InputError("Extracted anonymous graph inventory changed")
                output.graph_name = names[0]
            operation.consume("work", 0)
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(
                Issue("extraction.plan_unverified", str(error), kind="unverified")
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    report = Report(
        "data.extraction",
        checked,
        tuple(issues),
        requested=requested,
        complete=finished,
        regime="explicit-selected-rdf-extraction",
        children=tuple(reports),
    )
    return RecordExtraction(
        output if report.verified else None, tuple(references), report
    )
