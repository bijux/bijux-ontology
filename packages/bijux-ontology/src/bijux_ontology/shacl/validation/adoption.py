"""Qualify isolated backend replies and retain their original SHACL evidence."""

from collections.abc import Sequence

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF, SH, XSD
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import (
    Issue,
    IssueKind,
    Report,
    Severity,
    combine_reports,
)
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.limits import Budget, IssueCollector, retain_report
from bijux_ontology.json.values import decode_json
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource
from bijux_ontology.shacl.evidence.findings import ShaclSource, project_findings
from bijux_ontology.shacl.evidence.results import ShaclResult
from bijux_ontology.shacl.targets.coverage import ShaclCoverageContract, read_coverage
from bijux_ontology.shacl.validation.contracts import constraint_execution

from .contracts import ShaclPlan, selection_metadata


def retain_result(result: ShaclResult, budget: Budget | None) -> ShaclResult:
    """Keep native diagnostics paid once across all supported result statuses."""
    if budget is None:
        return result
    retained = IssueCollector(budget)
    try:
        retain_report(result.report, budget, retained)
    except LimitExceeded:
        retained.exhausted("SHACL report handoff exhausted its shared budget")
        result.report = Report(
            "shacl",
            result.report.checked,
            tuple(retained),
            requested=result.report.requested,
            complete=False,
            families=result.report.families,
        )
    return result


def _function_evidence(
    value: object,
    expected: tuple[str, ...],
    *,
    complete: bool,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ResourceError("SHACL process omitted function registration evidence")
    names, calls = value.get("registered"), value.get("calls")
    if (
        not isinstance(names, list)
        or not isinstance(calls, dict)
        or any(not isinstance(name, str) for name in names)
        or len(set(names)) != len(names)
        or set(names) != set(calls)
        or not set(names) <= set(expected)
        or complete
        and set(names) != set(expected)
        or any(type(count) is not int or count < 0 for count in calls.values())
    ):
        raise ResourceError(
            "SHACL process returned invalid function execution evidence"
        )
    return {
        "registered_functions": tuple(names),
        "function_calls": tuple(sorted(calls.items())),
    }


def incomplete_result(
    plan: ShaclPlan, error: Exception, focus: tuple[URIRef | BNode | Literal, ...] = ()
) -> ShaclResult:
    """Retain an incomplete operation without inventing raw conformance."""
    kind: IssueKind = (
        "exhausted"
        if isinstance(error, LimitExceeded)
        else "unverified"
        if isinstance(error, ResourceError)
        else "input_error"
    )
    return ShaclResult(
        Report(
            "shacl",
            0,
            (Issue("shacl.execution_incomplete", str(error), kind=kind),),
            requested=1,
            complete=False,
            families=plan.families,
        ),
        **selection_metadata(plan, focus),
    )


def adopt_result(
    raw: bytes,
    plan: ShaclPlan,
    data: Graph | PreparedSource,
    dg: Graph,
    focus: tuple[URIRef | BNode | Literal, ...],
    *,
    graph_name: GraphSelection | None,
    union: bool,
    source_usages: Sequence[ShaclSource],
) -> ShaclResult:
    """Retain the same raw/backend/provenance truth for either worker lifetime."""
    selected = plan.limits
    families = plan.families
    functions = plan.function_plan if plan.has_functions else None
    function_plan = plan.function_plan
    coverage, required_constraints = plan.coverage, plan.required_constraints
    allow_empty = plan.allow_empty
    sources = tuple(source_usages)
    if any(not isinstance(source, ShaclSource) for source in sources):
        raise InputError("Expected explicit prepared SHACL source usages")
    metadata = selection_metadata(plan, focus)
    if plan.budget is not None:
        plan.budget.consume("bytes", len(raw))
        plan.budget.consume("work", len(raw))
    response = decode_json(raw, max_bytes=selected.max_total_bytes)
    if not isinstance(response, dict):
        raise ResourceError("SHACL process returned an invalid protocol object")
    status = response.get("status")
    if not isinstance(status, str) or status not in {
        "result",
        "exception",
        "exhausted",
        "unavailable",
        "failure",
        "input_error",
        "shapes_invalid",
    }:
        raise ResourceError("SHACL process returned an invalid result status")
    backend = response.get("backend", "unavailable")
    if not isinstance(backend, str):
        raise ResourceError("SHACL process returned an invalid backend identity")
    if "functions" in response or status == "result" and functions is not None:
        metadata.update(
            _function_evidence(
                response.get("functions"),
                function_plan.names,
                complete=status == "result",
            )
        )
    if status == "exception":
        raise RuntimeError(
            f"SHACL worker {response.get('type')}: {response.get('message')}"
        )
    if status != "result":
        issue_kind: IssueKind = (
            "exhausted"
            if status == "exhausted"
            else "unverified"
            if status == "unavailable"
            else "input_error"
        )
        return ShaclResult(
            Report(
                "shacl",
                0,
                (
                    Issue(
                        f"shacl.{status}",
                        str(response.get("message", "Backend incomplete")),
                        kind=issue_kind,
                    ),
                ),
                requested=1,
                complete=False,
                families=families,
            ),
            backend=backend,
            conforms=response.get("conforms")
            if type(response.get("conforms")) is bool
            else None,
            **metadata,
        )
    shapes_count = response.get("shapes_count")
    if type(shapes_count) is not int or shapes_count < 0:
        raise ResourceError("SHACL process did not identify its discovered shape scope")
    conforms, raw_report = response.get("conforms"), response.get("report")
    warnings = response.get("warnings")
    if type(conforms) is not bool or not isinstance(raw_report, str):
        raise ResourceError("SHACL process returned an invalid conformance envelope")
    if not isinstance(warnings, list) or any(
        not isinstance(warning, str) for warning in warnings
    ):
        raise ResourceError("SHACL process returned invalid validation warnings")
    metadata["shapes_count"] = shapes_count
    report_graph = prepare_source(
        raw_report, format="nt", limits=selected, budget=plan.budget
    ).graph()
    reports = tuple(report_graph.subjects(RDF.type, SH.ValidationReport))
    if len(reports) != 1:
        raise ResourceError("SHACL process did not return one RDF ValidationReport")
    stated = tuple(report_graph.objects(reports[0], SH.conforms))
    if (
        len(stated) != 1
        or not isinstance(stated[0], Literal)
        or stated[0].datatype != XSD.boolean
        or stated[0].toPython() is not conforms
    ):
        raise ResourceError("SHACL RDF conformance contradicts the result envelope")
    root_results = tuple(report_graph.objects(reports[0], SH.result))
    if conforms == bool(root_results) or any(
        (node, RDF.type, SH.ValidationResult) not in report_graph
        for node in root_results
    ):
        raise ResourceError("SHACL result nodes contradict the stated conformance")
    correlation_graph = graph_name
    correlation_union = union
    if isinstance(data, PreparedSource) and (data.graph_name is not None or data.union):
        correlation_graph, correlation_union = data.graph_name, data.union
    findings, roots, findings_complete = project_findings(
        report_graph,
        dg,
        sources=sources,
        graph_name=(
            None if correlation_graph in (None, "@default") else str(correlation_graph)
        ),
        union=correlation_union or isinstance(correlation_graph, BNode),
        limits=selected,
        budget=plan.budget,
    )
    issues = IssueCollector(plan.budget) if plan.budget is not None else []
    severity_names: dict[Node | None, Severity] = {
        SH.Info: "info",
        SH.Warning: "warning",
    }
    try:
        for finding in findings:
            severity = severity_names.get(finding.severity, "error")
            message = (
                "; ".join(map(str, finding.messages))
                or "Supplied data does not conform to the shape"
            )
            issues.append(
                Issue(
                    "shacl.nonconformance",
                    message,
                    str(finding.focus_node or ""),
                    severity=severity,
                    details=(
                        finding.locations[0]
                        if finding.location_status == "matched"
                        and len(finding.locations) == 1
                        else None
                    ),
                    evidence_json=finding.evidence_json,
                )
            )
        if not findings_complete:
            issues.append(
                Issue(
                    "shacl.finding_unverified",
                    "Result projection is incomplete; inspect the retained "
                    "raw RDF report",
                    kind="unverified",
                )
            )
        for warning in warnings:
            issues.append(
                Issue("shacl.backend_warning", str(warning), kind="unverified")
            )
        if not shapes_count and not allow_empty:
            issues.append(
                Issue(
                    "input.empty_scope",
                    "The supplied shapes graph contains no discovered SHACL shapes",
                    "shapes",
                    kind="input_error",
                )
            )
    except LimitExceeded:
        if not isinstance(issues, IssueCollector):
            raise
        issues.exhausted("SHACL result projection exhausted its shared budget")
        findings_complete = False
    report = Report(
        "shacl",
        int(shapes_count > 0),
        tuple(issues),
        requested=int(shapes_count > 0 or not allow_empty),
        allow_empty=allow_empty,
        complete=not warnings
        and findings_complete
        and (shapes_count > 0 or allow_empty),
        families=families,
    )
    evaluated_coverage = None
    constraint_report = None
    additional: list[Report] = []
    try:
        if coverage is not None or required_constraints:
            evaluated_coverage = read_coverage(
                response.get("coverage"),
                coverage or ShaclCoverageContract(),
                selected,
                budget=plan.budget,
            )
            additional.append(evaluated_coverage.report)
        if required_constraints and evaluated_coverage is not None:
            constraint_report = constraint_execution(
                required_constraints,
                evaluated_coverage,
                budget=plan.budget,
            )
            additional.append(constraint_report)
    except (InputError, ResourceError) as error:
        # The native RDF report and its paid findings have already been qualified.
        # A later required coverage failure cannot erase that completed evidence.
        additional.append(
            Report(
                "shacl-required-coverage",
                0,
                (
                    Issue(
                        "shacl.execution_incomplete",
                        str(error),
                        kind="exhausted"
                        if isinstance(error, LimitExceeded)
                        else "input_error",
                    ),
                ),
                requested=1,
                complete=False,
            )
        )
    return ShaclResult(
        combine_reports("shacl", report, *additional) if additional else report,
        conforms=conforms,
        backend=backend,
        graph=report_graph,
        findings=findings,
        root_findings=roots,
        findings_complete=findings_complete,
        coverage=evaluated_coverage,
        conformance_report=report,
        constraints=constraint_report,
        **metadata,
    )
