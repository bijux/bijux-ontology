"""Application reification constraints, separate from RDF assertion semantics."""

from collections.abc import Sequence

from rdflib import BNode, URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Location, Report
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.reification.reading import inspect_reifications
from bijux_ontology.rdf.reification.types import (
    ReificationCheck,
    ReificationContract,
    ReificationRecord,
)
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_node, term_key
from bijux_ontology.rdf.transforms import _budget


def _finding(record: ReificationRecord, message: str) -> Issue:
    """Use already qualified exact node/graph anchors without fabricated paths."""
    return Issue(
        "rdf.reification_contract",
        message,
        details=Location(
            term=str(record.node),
            graph=str(record.graph) if record.graph is not None else None,
        ),
    )


def check_reifications(
    source: PreparedSource,
    contract: ReificationContract,
    *,
    nodes: Sequence[URIRef | BNode] | None = None,
    graph_name: GraphSelection | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ReificationCheck:
    """Check explicit application completeness and uniqueness over actual records.

    Inspection happens once under the same budget. No contract can promote an
    incomplete extraction to verified agreement. Duplicate described statements
    are permitted unless unique_statements is selected; that rule is per physical
    graph. No base assertion, identifier equivalence or annotation propagation is
    inferred, even when different reifiers have identical component tuples.
    """
    if not isinstance(contract, ReificationContract):
        raise InputError("Reification checks require a typed application contract")
    operation = _budget(limits, budget)
    issues = IssueCollector(operation)
    inspection = inspect_reifications(
        source, nodes=nodes, graph_name=graph_name, budget=operation
    )
    checked = 0
    completed = False
    try:
        retain_report(inspection.report, operation, issues)
        prior: dict[tuple[tuple[str | None, ...], ...], ReificationRecord] = {}
        for record in inspection.records:
            operation.consume("work")
            bound_node(record.node, operation)
            if record.graph is not None:
                bound_node(record.graph, operation)
            values = (record.subjects, record.predicates, record.objects)
            counts = tuple(len(items) for items in values)
            if record.status == "invalid_roles":
                issues.append(
                    _finding(
                        record,
                        "Statement subject must be a resource and predicate an IRI",
                    )
                )
            if contract.require_complete and any(count == 0 for count in counts):
                issues.append(
                    _finding(
                        record, "The selected record is missing a statement component"
                    )
                )
            if contract.unique_components and any(count > 1 for count in counts):
                issues.append(
                    _finding(
                        record,
                        "The record binds a statement component more than once",
                    )
                )
            if contract.require_type and not record.explicit_type:
                issues.append(
                    _finding(
                        record,
                        "The selected record lacks an explicit rdf:Statement type",
                    )
                )
            if contract.unique_statements:
                if record.statement is None:
                    issues.append(
                        Issue(
                            "rdf.reification_unverified",
                            "An unresolved tuple cannot establish statement uniqueness",
                            kind="unverified",
                        )
                    )
                else:
                    operation.consume("states")
                    operation.consume("bytes", 128)
                    for term in record.statement:
                        bound_node(term, operation)
                    key = (
                        (
                            term_key(record.graph)
                            if record.graph is not None
                            else ("default",)
                        ),
                        *(term_key(term) for term in record.statement),
                    )
                    if key in prior:
                        issues.append(
                            _finding(
                                record,
                                "Distinct records describe the same selected statement",
                            )
                        )
                    else:
                        prior[key] = record
            checked += 1
        for node in inspection.missing:
            operation.consume("work")
            bound_node(node, operation)
            issues.append(
                Issue(
                    "rdf.reification_contract"
                    if contract.require_complete
                    else "rdf.reification_unverified",
                    "The node has no outgoing occurrence in the selected scope",
                    kind="finding" if contract.require_complete else "unverified",
                    details=Location(term=str(node)),
                )
            )
            checked += 1
        if not inspection.records and not inspection.missing:
            issues.append(
                Issue(
                    "rdf.reification_unverified",
                    "No records were available for the required application check",
                    kind="unverified",
                )
            )
        completed = inspection.report.complete
    except LimitExceeded as error:
        issues.exhausted(str(error))
    requested = len(inspection.records) + len(inspection.missing)
    return ReificationCheck(
        inspection,
        contract,
        Report(
            "rdf.reification_contract",
            checked,
            tuple(issues),
            requested=max(requested, checked, 1),
            complete=completed,
            regime="application-reification-contract",
            families=("reification.term_roles",)
            + tuple(
                name
                for name, enabled in (
                    ("reification.completeness", contract.require_complete),
                    ("reification.component_uniqueness", contract.unique_components),
                    ("reification.explicit_type", contract.require_type),
                    ("reification.statement_uniqueness", contract.unique_statements),
                )
                if enabled
            ),
            exclusions=tuple(
                name
                for name, enabled in (
                    ("reification.completeness", contract.require_complete),
                    ("reification.component_uniqueness", contract.unique_components),
                    ("reification.explicit_type", contract.require_type),
                    ("reification.statement_uniqueness", contract.unique_statements),
                )
                if not enabled
            ),
            children=(inspection.report,),
        ),
    )
