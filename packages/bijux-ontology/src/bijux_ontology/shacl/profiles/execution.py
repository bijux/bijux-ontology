"""Owned selected-profile execution and result semantics shared by consumers."""

import time

from rdflib import BNode, Graph
from rdflib.namespace import SH

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, IssueCollector, retain_report
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, independent_blank_scopes
from bijux_ontology.reports import Report
from bijux_ontology.shacl.checking import ShaclResult
from bijux_ontology.shacl.coverage import ShaclCoverageContract
from bijux_ontology.shacl.session import ShaclSession

from .resources import _owned
from .types import ShapeProfile, Triple


def remaining_deadline(operation: Operation) -> float:
    """Check the global deadline before creating each owned backend request."""
    operation.consume("work")
    remaining = operation.deadline - time.monotonic()
    if remaining <= 0:
        raise LimitExceeded("Shape profile exceeded its deadline")
    return remaining


def freeze_data(source: PreparedSource, budget: Budget) -> PreparedSource:
    """Freeze exactly one explicit graph before both sides consume a case."""
    if source.union or source.literal_fidelity not in (
        "source_terms",
        "supplied_terms",
    ):
        raise InputError("Profile cases require an exact selected physical graph")
    graph = source._read(budget=budget)
    triples: list[Triple] = []
    for triple in graph:
        budget.consume("work")
        budget.consume("states")
        for node in triple:
            bound_node(node, budget)
        triples.append(triple)
    return _owned(source, graph, triples, budget)


def portable_roles(profile: ShapeProfile, data: PreparedSource, budget: Budget) -> bool:
    """Reject exact anonymous identity shared across separately serialized roles.

    This tests owned term objects, never coincident blank labels. Anonymous shape
    structure within one role remains portable through ordinary RDF isomorphism.
    """
    return independent_blank_scopes(
        (
            source._read(budget=budget)
            for source in (profile.source, profile.ontology, data)
            if source is not None
        ),
        budget,
    )


def validate_profile(
    shapes: PreparedSource,
    profile: ShapeProfile,
    data: PreparedSource,
    budget: Operation,
) -> ShaclResult:
    """Use the existing cancellable isolated worker with this request's deadline."""
    budget.consume("resources")
    with ShaclSession(
        shapes,
        ontology=profile.ontology,
        use_shapes=profile.roots,
        preflight=True,
        coverage=ShaclCoverageContract(),
        limits=budget.limits,
        timeout=remaining_deadline(budget),
    ) as session:
        result = session._validate(
            data,
            focus_nodes=(),
            graph_name=None,
            union=False,
            source_usages=(),
            scope="dataset",
            budget=budget,
            cancel=budget.cancel,
        )
    retained = IssueCollector(budget)
    try:
        retain_report(result.report, budget, retained)
        budget.consume("states", len(result.findings))
    except LimitExceeded as error:
        retained.exhausted(str(error))
        result.report = Report(
            "shacl",
            result.report.checked,
            tuple(retained),
            requested=result.report.requested,
            complete=False,
            regime=result.report.regime,
        )
        result.conformance_report = result.report
        keep = sum(issue.code == "shacl.nonconformance" for issue in retained)
        result.findings = result.findings[:keep]
        result.root_findings = ()
        result.findings_complete = False
        result.coverage = None
        result.constraints = None
    return result


def result_semantics(result: ShaclResult, budget: Budget) -> tuple[Graph, bool]:
    """Compare standard RDF result semantics, excluding generated message prose.

    Blank source shapes/paths carry maintained cloned descriptions and compare
    jointly through RDF isomorphism. Anonymous data endpoints lack an established
    cross-result association here and therefore remain explicitly unknown.
    """
    graph = result.report_graph()
    established = True
    for finding in result.findings:
        budget.consume("work")
        if isinstance(finding.focus_node, BNode) or isinstance(finding.value, BNode):
            established = False
        for node in (finding.source_shape, finding.path):
            if isinstance(node, BNode) and (node, None, None) not in graph:
                established = False
    for triple in graph:
        budget.consume("work")
        budget.consume("triples")
        for node in triple:
            bound_node(node, budget)
    graph.remove((None, SH.resultMessage, None))
    return graph, established
