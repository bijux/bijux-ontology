"""Explicit closed application policies over original open RDF containers."""

from collections.abc import Sequence

from rdflib import BNode, URIRef
from rdflib.namespace import RDF

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.rdf.transforms import _budget
from bijux_ontology.reports import Issue, Location, Report

from .reading import inspect_containers
from .types import ContainerCheck, ContainerContract, ContainerRecord


def _finding(record: ContainerRecord, message: str) -> Issue:
    """Attach already bounded physical node/graph evidence without a guessed path."""
    return Issue(
        "rdf.container_contract",
        message,
        details=Location(
            term=str(record.node),
            graph=str(record.graph) if record.graph is not None else None,
        ),
    )


def check_containers(
    source: PreparedSource,
    contract: ContainerContract,
    *,
    nodes: Sequence[URIRef | BNode] | None = None,
    graph_name: GraphSelection | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ContainerCheck:
    """Apply caller-selected finite closure; never infer closure from rdf:Seq/Bag."""
    if not isinstance(contract, ContainerContract):
        raise InputError("Container checking requires an explicit application contract")
    operation = _budget(limits, budget)
    issues = IssueCollector(operation)
    inspection = inspect_containers(
        source, nodes=nodes, graph_name=graph_name, budget=operation
    )
    checked = 0
    complete = False
    try:
        retain_report(inspection.report, operation, issues)
        if contract.size is not None:
            operation.consume("work", contract.size.bit_length() + 1)
            operation.consume("bytes", (contract.size.bit_length() + 7) // 8)
        required = RDF.Seq if contract.kind == "sequence" else RDF.Bag
        for record in inspection.records:
            operation.consume("work", len(record.kinds) + len(record.slots) + 1)
            bound_node(record.node, operation)
            if record.graph is not None:
                bound_node(record.graph, operation)
            if contract.require_type and required not in record.kinds:
                issues.append(
                    _finding(record, "The explicit selected container type is absent")
                )
            if any(len(slot.values) != 1 for slot in record.slots):
                issues.append(
                    _finding(record, "A numbered slot has multiple distinct RDF values")
                )
            if contract.kind == "sequence" and record.gaps:
                issues.append(
                    _finding(
                        record, "The closed sequence has unobserved numbered positions"
                    )
                )
            if contract.size is not None and len(record.slots) != contract.size:
                issues.append(
                    _finding(
                        record,
                        "The actual slot count differs from the explicit closed size",
                    )
                )
            checked += 1
        for node in inspection.missing:
            operation.consume("work")
            bound_node(node, operation)
            issues.append(
                Issue(
                    "rdf.container_contract",
                    "The selected container has no outgoing occurrence",
                    details=Location(term=str(node)),
                )
            )
            checked += 1
        if not inspection.records and not inspection.missing:
            issues.append(
                Issue(
                    "rdf.container_unverified",
                    "No container was available for the required application contract",
                    kind="unverified",
                )
            )
        complete = inspection.report.complete
    except LimitExceeded as error:
        issues.exhausted(str(error))
    return ContainerCheck(
        inspection,
        contract,
        Report(
            "rdf.container_contract",
            checked,
            tuple(issues),
            requested=max(1, len(inspection.records) + len(inspection.missing)),
            complete=complete,
            children=(inspection.report,),
            regime="explicit-closed-container-contract",
        ),
    )
