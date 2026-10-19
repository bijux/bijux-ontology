"""Observe every selected record through the existing owned RDF read boundary."""

from collections.abc import Sequence
from typing import cast

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF
from rdflib.term import Node

from bijux_ontology.data.projection.reading import project_records
from bijux_ontology.data.projection.types import ReadBinding, ReadField
from bijux_ontology.declarations.path_fields import PathFieldDeclaration
from bijux_ontology.declarations.types import OutputRequirement, ValueDeclaration
from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, IssueCollector, bound_text, retain_report
from bijux_ontology.rdf.literals import check_literal
from bijux_ontology.rdf.paths import capture_path
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_node, literal_datatype, term_key
from bijux_ontology.reports import Issue, Report

from .types import DiscoveryField, FieldObservation, ObservedField, RecordObservation


def reserve(budget: Budget, count: int = 1) -> None:
    """Pay owned result/reference storage before retaining it."""
    budget.consume("states", count)
    budget.consume("bytes", 128 * count)


def capture(
    records: Sequence[URIRef | BNode], fields: Sequence[DiscoveryField], budget: Budget
) -> tuple[tuple[URIRef | BNode, ...], tuple[DiscoveryField, ...]]:
    """Capture caller inventories before invoking another operation."""
    if not isinstance(records, Sequence) or isinstance(records, str | bytes):
        raise InputError("Discovery records require a finite sequence")
    if not isinstance(fields, Sequence) or isinstance(
        cast(object, fields), str | bytes
    ):
        raise InputError("Discovery fields require a finite sequence")
    captured_records: list[URIRef | BNode] = []
    seen: set[tuple[str | None, ...]] = set()
    for i in range(len(records)):
        reserve(budget, 2)
        budget.consume("work")
        record = records[i]
        if not isinstance(record, URIRef | BNode):
            raise InputError("Discovery records require resource terms")
        bound_node(record, budget)
        key = term_key(record)
        if key in seen:
            raise InputError("Discovery records must be distinct")
        seen.add(key)
        captured_records.append(record)
    captured_fields: list[DiscoveryField] = []
    names: set[str] = set()
    for i in range(len(fields)):
        reserve(budget, 2)
        budget.consume("work")
        field = fields[i]
        if type(field) is not DiscoveryField:
            raise InputError("Expected typed DiscoveryField records")
        bound_text(field.name, budget)
        if field.name in names:
            raise InputError("Discovery field names must be distinct")
        names.add(field.name)
        path = capture_path(field.path, budget=budget)
        captured_fields.append(DiscoveryField(field.name, path))
    if not captured_records or not captured_fields:
        raise InputError("Discovery requires nonempty explicit records and fields")
    return tuple(captured_records), tuple(captured_fields)


def observe(
    source: PreparedSource | Graph,
    records: tuple[URIRef | BNode, ...],
    fields: tuple[DiscoveryField, ...],
    graph_name: GraphSelection | None,
    budget: Budget,
    issues: IssueCollector,
    children: list[Report],
    observations: list[RecordObservation],
) -> tuple[
    PreparedSource | None,
    URIRef | BNode | None,
    tuple[RecordObservation, ...],
    tuple[ObservedField, ...],
]:
    """Retain raw exceptions and full alternatives; frequency never removes a branch."""
    reserve(budget, 3 * len(records) * (len(fields) + 1) + 1)
    reads = tuple(ReadField(f.name, f.path, mode="many", minimum=0) for f in fields)
    bindings = tuple(ReadBinding(str(i), node, reads) for i, node in enumerate(records))
    projection = project_records(source, bindings, graph_name=graph_name, budget=budget)
    # Retention was reserved before the nested read. Preserve every paid raw
    # record even if graph inspection or the next literal check cannot run.
    children.append(projection.report)
    for record in projection.records:
        observations.append(
            RecordObservation(
                record.reference,
                tuple(
                    FieldObservation(cell.name, cell.endpoints, (), (), False)
                    for cell in record.fields
                ),
            )
        )
    retain_report(projection.report, budget, issues)
    if not projection.records:
        return None, None, (), ()
    prepared = projection.records[0].reference.source
    graph = projection.records[0].reference.graph
    selected = prepared._read(graph, budget=budget)
    alternatives: list[list[ValueDeclaration]] = [[] for _ in fields]
    counts: list[list[int]] = [[] for _ in fields]
    for record_index, record in enumerate(projection.records):
        cells: list[FieldObservation] = []
        for index, cell in enumerate(record.fields):
            reserve(budget, len(cell.endpoints) + 3)
            bound_text(cell.name, budget)
            counts[index].append(len(cell.endpoints))
            qualified = cell.state in ("raw", "missing")
            branches: list[ValueDeclaration] = []
            type_evidence: list[tuple[Node, ...]] = []
            for node in cell.endpoints:
                bound_node(node, budget)
                kinds: list[Node] = []
                if isinstance(node, Literal):
                    if prepared.literal_fidelity not in (
                        "source_terms",
                        "supplied_terms",
                    ):
                        issues.append(
                            Issue(
                                "discovery.identity_unverified",
                                "Literal identity was not retained",
                                record.name + "/" + cell.name,
                                kind="unverified",
                            )
                        )
                        qualified = False
                    literal = check_literal(node, budget=budget)
                    reserve(budget)
                    children.append(literal)
                    retain_report(literal, budget, issues)
                    qualified = qualified and literal.verified
                    datatype = literal_datatype(node)
                    bound_node(datatype, budget)
                    branch = ValueDeclaration("literal", datatype=str(datatype))
                else:
                    for kind in selected.objects(node, RDF.type):
                        budget.consume("work")
                        bound_node(kind, budget)
                        reserve(budget)
                        kinds.append(kind)
                    if any(not isinstance(kind, URIRef) for kind in kinds):
                        issues.append(
                            Issue(
                                "discovery.types_unverified",
                                "Anonymous or non-IRI asserted types cannot become "
                                "native type declarations",
                                record.name + "/" + cell.name,
                                kind="unverified",
                            )
                        )
                        qualified = False
                    budget.consume(
                        "work",
                        sum(len(str(k)) for k in kinds)
                        * max(1, len(kinds).bit_length())
                        + 1,
                    )
                    branch = ValueDeclaration(
                        "iri" if isinstance(node, URIRef) else "object",
                        types=tuple(
                            sorted(str(k) for k in kinds if isinstance(k, URIRef))
                        ),
                    )
                type_evidence.append(tuple(kinds))
                reserve(budget, 2)
                budget.consume(
                    "work",
                    (len(branches) + len(alternatives[index]) + 1)
                    * (
                        1
                        + len(branch.datatype or "")
                        + len(branch.types)
                        + sum(len(iri) for iri in branch.types)
                    ),
                )
                if branch not in branches:
                    branches.append(branch)
                if branch not in alternatives[index]:
                    alternatives[index].append(branch)
            reserve(budget, 2)
            cells.append(
                FieldObservation(
                    cell.name,
                    cell.endpoints,
                    tuple(type_evidence),
                    tuple(branches),
                    qualified,
                )
            )
        reserve(budget)
        observations[record_index] = RecordObservation(record.reference, tuple(cells))
    summaries: list[ObservedField] = []
    for index, field in enumerate(fields):
        reserve(budget, 2)
        count = counts[index]
        low, high = (min(count), max(count)) if count else (0, 0)
        values = tuple(alternatives[index])
        declaration = None
        if values:
            declaration = PathFieldDeclaration(
                field.name,
                field.path,
                values,
                output=OutputRequirement(
                    allow_null=True,
                    allow_empty=True,
                    minimum_values=low,
                    maximum_values=high,
                ),
            )
        else:
            issues.append(
                Issue(
                    "discovery.values_unverified",
                    "An entirely missing field establishes no value alternatives",
                    field.name,
                    kind="unverified",
                )
            )
        summaries.append(ObservedField(field, low, high, values, declaration))
    return prepared, graph, tuple(observations), tuple(summaries)
