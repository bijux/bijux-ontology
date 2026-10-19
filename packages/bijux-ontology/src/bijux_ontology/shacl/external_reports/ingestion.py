"""Bounded ordinary SHACL report ingestion without claiming local execution."""

import json
from collections.abc import Iterator

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF, SH, XSD
from rdflib.term import Node

from bijux_ontology._json import owned_json
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report, Severity
from bijux_ontology.execution.budgets import operation
from bijux_ontology.limits import Budget, BudgetKind, IssueCollector, Limits, bound_text
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import Payload, RdfSource
from bijux_ontology.shacl.findings import ShaclFinding, project_findings

from .types import ExternalShaclReport, ExternalShaclScope


class _ProjectionBudget(Budget):
    """Observe the existing projector's bounded partial-return exhaustion."""

    def __init__(self, shared: Budget) -> None:
        """Keep the exact caller counters while recording a swallowed limit."""
        super().__init__(shared.limits)
        self.shared = shared
        self.failed = False

    def remaining(self, kind: BudgetKind) -> int:
        """Read the same original allowance without a fresh projection grant."""
        return self.shared.remaining(kind)

    def consume(self, kind: BudgetKind, amount: int = 1) -> None:
        """Forward all projection costs before its existing partial-result catch."""
        try:
            self.shared.consume(kind, amount)
        except LimitExceeded:
            self.failed = True
            raise


def _values(
    graph: Graph, subject: Node, predicate: Node, budget: Budget
) -> Iterator[Node]:
    """Charge every selected assertion before retaining inventory references."""
    for value in graph.objects(subject, predicate):
        budget.consume("work")
        budget.consume("states")
        yield value


def _single(
    graph: Graph, node: Node, predicate: Node, budget: Budget, *, required: bool = False
) -> Node | None:
    """Reject ambiguous fields before the existing singleton projection API."""
    selected = None
    for value in _values(graph, node, predicate, budget):
        if selected is not None:
            raise InputError("External SHACL result field has multiple values")
        selected = value
    if required and selected is None:
        raise InputError("External SHACL result omits a required field")
    return selected


def _boolean(value: Node | None) -> bool:
    """Read only valid explicitly typed XSD boolean terms without coercion."""
    if (
        not isinstance(value, Literal)
        or value.datatype != XSD.boolean
        or str(value) not in ("true", "false", "1", "0")
    ):
        raise InputError("External SHACL conformance requires an XSD boolean")
    return str(value) in ("true", "1")


def _structure(graph: Graph, budget: Budget) -> tuple[Node, bool, bool]:
    """Qualify the finite direct standards report/result field inventory."""
    reports = []
    for node in graph.subjects(RDF.type, SH.ValidationReport):
        budget.consume("work")
        budget.consume("states")
        reports.append(node)
        if len(reports) > 1:
            raise InputError("Expected exactly one external SHACL ValidationReport")
    if not reports:
        raise InputError("Expected exactly one external SHACL ValidationReport")
    report = reports[0]
    conforms = _boolean(_single(graph, report, SH.conforms, budget, required=True))
    well_formed = _single(graph, report, SH.shapesGraphWellFormed, budget)
    if well_formed is not None:
        _boolean(well_formed)
    roots = list(_values(graph, report, SH.result, budget))
    if conforms == bool(roots):
        raise InputError("External SHACL results contradict raw conformance")
    budget.consume("states", 2 * len(roots))
    budget.consume("work", len(roots))
    root_nodes = frozenset(roots)
    pending = list(roots)
    complete = True
    seen: set[Node] = set()
    while pending:
        budget.consume("work")
        node = pending.pop()
        if node in seen:
            continue
        budget.consume("states")
        seen.add(node)
        if not isinstance(node, URIRef | BNode):
            raise InputError("External SHACL result identities require resources")
        validation_result = (node, RDF.type, SH.ValidationResult) in graph
        if not validation_result and node in root_nodes:
            raise InputError("External roots require explicit ValidationResult types")
        if not validation_result and (node, RDF.type, SH.AbstractResult) not in graph:
            complete = False
        _single(graph, node, SH.focusNode, budget, required=validation_result)
        for predicate in (SH.resultSeverity, SH.sourceConstraintComponent):
            value = _single(graph, node, predicate, budget, required=validation_result)
            if value is not None and not isinstance(value, URIRef):
                raise InputError("External SHACL severity and component require IRIs")
        for predicate in (SH.resultPath, SH.sourceShape):
            value = _single(graph, node, predicate, budget)
            if value is not None and not isinstance(value, URIRef | BNode):
                raise InputError(
                    "External SHACL paths and source shapes require resources"
                )
        _single(graph, node, SH.value, budget)
        for message in _values(graph, node, SH.resultMessage, budget):
            if not isinstance(message, Literal):
                raise InputError("External SHACL result messages require literals")
        pending.extend(_values(graph, node, SH.detail, budget))
    # Additional RDF is preserved. Orphan result nodes cannot silently join the
    # global existing finding projector as if they belonged to this report.
    for node in graph.subjects(RDF.type, SH.ValidationResult):
        budget.consume("work")
        if node not in seen:
            raise InputError("External report contains unassociated result nodes")
    for subject, _, _ in graph.triples((None, SH.result, None)):
        budget.consume("work")
        if subject != report:
            raise InputError("External report contains another result root")
    for subject, _, _ in graph.triples((None, SH.detail, None)):
        budget.consume("work")
        if subject not in seen:
            raise InputError("External report contains an unassociated detail edge")
    return report, conforms, complete


def _finding_issue(finding: ShaclFinding, budget: Budget) -> Issue:
    """Reserve canonical ASCII evidence before constructing a generic finding."""
    evidence, count = owned_json(finding.to_dict(), budget)
    allowance = 16 * count + 128
    if allowance > budget.limits.max_bytes:
        raise LimitExceeded("External SHACL finding evidence exceeds max_bytes")
    budget.consume("bytes", allowance)
    budget.consume("work", allowance)
    encoded = json.dumps(
        evidence, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    size = sum(len(message) for message in finding.messages) + 2 * len(finding.messages)
    if size > budget.limits.max_bytes:
        raise LimitExceeded("External SHACL finding messages exceed max_bytes")
    budget.consume("work", size)
    budget.consume("bytes", 4 * size)
    message = (
        "; ".join(map(str, finding.messages))
        or "Externally asserted SHACL nonconformance"
    )
    severity: Severity = (
        "info"
        if finding.severity == SH.Info
        else "warning"
        if finding.severity == SH.Warning
        else "error"
    )
    return Issue(
        "shacl.external_nonconformance",
        message,
        kind="finding",
        severity=severity,
        evidence_json=encoded,
    )


def ingest_external_report(
    source: RdfSource | Payload,
    *,
    scope: ExternalShaclScope | None = None,
    backend: str | None = None,
    format: str | None = None,
    base: str | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ExternalShaclReport:
    """Import one asserted report; producer claims never prove current local scope.

    The finite profile requires direct standard report/root-result types and
    singleton fields. Abstract or extension details retain their original nodes;
    Unqualified extension types stay unknown rather than becoming violations.
    Unknown extension triples are retained without interpretation. Data
    correspondence is unavailable: report-local blank focus nodes are not rebound
    to a current local dataset, and no source locations are invented.
    """
    shared = operation(limits, budget)
    issues, structural = IssueCollector(shared), IssueCollector(shared)
    owned: PreparedSource | None = None
    report_node = None
    conforms = None
    qualified_scope = None
    qualified_backend = None
    findings: tuple[ShaclFinding, ...] = ()
    roots: tuple[ShaclFinding, ...] = ()
    complete = False
    structure_complete = False
    try:
        if scope is not None and not isinstance(scope, ExternalShaclScope):
            raise InputError("External SHACL scope requires descriptive typed claims")
        for text in (
            backend,
            *((scope.data, scope.shapes, scope.execution) if scope else ()),
        ):
            if text is not None:
                if type(text) is not str or not text:
                    raise InputError(
                        "External SHACL claims require nonempty plain text"
                    )
                bound_text(text, shared)
        qualified_scope, qualified_backend = scope, backend
        owned = prepare_rdf(source, format=format, base=base, budget=shared)
        graph = owned._read(budget=shared)
        report_node, conforms, structure_complete = _structure(graph, shared)
        if not structure_complete:
            structural.append(
                Issue(
                    "shacl.external_detail_unverified",
                    "An extension detail has no directly qualified "
                    "standard result type",
                    kind="unverified",
                )
            )
        projection = _ProjectionBudget(shared)
        findings, roots, complete = project_findings(graph, Graph(), budget=projection)
        for finding in findings:
            if (finding.result_node, RDF.type, SH.ValidationResult) in graph:
                issues.append(_finding_issue(finding, shared))
        if projection.failed:
            raise LimitExceeded(
                "External finding projection exhausted its shared allowance"
            )
        if not complete:
            issues.append(
                Issue(
                    "shacl.external_projection_incomplete",
                    "External paths or detail cycles are not completely projected",
                    kind="unverified",
                )
            )
        if (
            scope is None
            or any(text is None for text in (scope.data, scope.shapes, scope.execution))
            or backend is None
        ):
            issues.append(
                Issue(
                    "shacl.external_scope_missing",
                    "External backend, input, shapes or execution claims are missing",
                    kind="unverified",
                )
            )
        issues.append(
            Issue(
                "shacl.external_proof_unverified",
                "Externally asserted results do not independently establish "
                "fresh local execution or omitted coverage",
                kind="unverified",
            )
        )
    except LimitExceeded:
        issues.exhausted(
            "External SHACL report ingestion exhausted its shared allowance"
        )
        complete = False
    except (InputError, ResourceError):
        try:
            structural.append(
                Issue(
                    "shacl.external_structure_invalid",
                    "External SHACL report input or structure is invalid",
                    kind="input_error",
                )
            )
        except LimitExceeded:
            issues.exhausted(
                "External SHACL input diagnostics exhausted the shared allowance"
            )
    if not structure_complete and not structural:
        structural.exhausted(
            "External SHACL structure qualification exhausted its allowance"
        )
    structure = Report(
        "external-shacl-structure",
        int(report_node is not None),
        tuple(structural),
        requested=1,
        complete=structure_complete,
    )
    report = Report(
        "external-shacl",
        int(report_node is not None),
        (*structural, *issues),
        requested=1,
        complete=False,
        children=(structure,),
        regime="externally-asserted",
    )
    result = object.__new__(ExternalShaclReport)
    for name, value in {
        "scope": qualified_scope,
        "backend": qualified_backend,
        "conforms": conforms,
        "report_node": report_node,
        "findings": findings,
        "root_findings": roots,
        "findings_complete": complete,
        "structural_report": structure,
        "report": report,
        "_source": owned,
    }.items():
        object.__setattr__(result, name, value)
    return result
