"""Owned maintained Jena validation returning the ordinary SHACL evidence type."""

from collections.abc import Mapping, Sequence
from threading import Event

from rdflib import Graph

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    retain_report,
)
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.sources.types import Payload, RdfSource
from bijux_ontology.shacl.evidence.findings import ShaclSource
from bijux_ontology.shacl.evidence.results import ShaclResult
from bijux_ontology.shacl.native.planning import NativeShaclPlan
from bijux_ontology.shacl.native.requests import (
    capture_native_sources,
    encode_native_request,
    prepare_native_request,
)
from bijux_ontology.shacl.native.results import adopt_native_result
from bijux_ontology.shacl.targets.coverage import ShaclCoverageContract
from bijux_ontology.shacl.validation.bindings import TermSelection
from bijux_ontology.shacl.validation.contracts import ShaclConstraintRequirement

from ..runtime import invoke_jena
from ..types import JenaBackend
from .preparation import prepare_plan


def validate_jena_shapes(
    backend: JenaBackend,
    data: Graph | PreparedSource,
    shapes: Graph | PreparedSource,
    *,
    ontology: Graph | PreparedSource | None = None,
    graph_name: GraphSelection | None = None,
    union: bool = False,
    imports: Mapping[str, RdfSource | Payload] | None = None,
    trusted_sparql: bool = False,
    focus_nodes: Sequence[TermSelection] = (),
    use_shapes: Sequence[TermSelection] = (),
    mode: str = "targets",
    preflight: bool = False,
    allow_empty: bool = False,
    coverage: ShaclCoverageContract | None = None,
    required_constraints: Sequence[ShaclConstraintRequirement] = (),
    source_usages: Sequence[ShaclSource] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> ShaclResult:
    """Validate supplied scope using the pinned optional Jena6 Core/SELECT engine.

    Existing snapshot selection, root/pair semantics, raw RDF reports, findings
    and required coverage are shared with the Python API. Native Jena discovers
    actual shapes/targets/constraints. Trusted SELECT constraints require parsed
    local-only queries and available native functions; missing implementations,
    ASK/custom components, advanced targets/rules/JS and meta preflight remain
    unknown. No implicit fetch, inference or backend substitution occurs.
    Conformance, target coverage and required constraint execution stay separate.
    Parity is limited to actual compared operations, never interchangeable-engine
    certification. Native execution is heap/deadline/output bounded on POSIX;
    generic imports need no Java and other platforms have no ownership claim.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    plan: NativeShaclPlan | None = None
    focus: tuple[TermSelection, ...] = ()
    result = None
    try:
        if type(union) is not bool:
            raise InputError("Dataset union selection requires an exact boolean")
        if not isinstance(data, Graph | PreparedSource):
            raise InputError("SHACL data requires a graph or prepared source")
        plan = prepare_plan(
            shapes,
            ontology,
            imports,
            use_shapes,
            mode,
            trusted_sparql,
            preflight,
            allow_empty,
            coverage,
            required_constraints,
            operation,
        )
        request = prepare_native_request(
            data,
            plan,
            focus_nodes,
            graph_name=graph_name,
            union=union,
        )
        focus = request.focus
        request = capture_native_sources(request, source_usages)
        if plan.features:
            issues.extend(plan.features)
        else:
            payload = encode_native_request(request)
            raw = invoke_jena(backend, payload, operation, heap_mb)
            result = adopt_native_result(raw, request)
            retain_report(result.report, operation, issues)
    except LimitExceeded as error:
        issues.exhausted(str(error), location="shacl.jena")
    except (InputError, OSError) as error:
        try:
            unknown = isinstance(error, ResourceError | OSError)
            issues.append(
                Issue(
                    "shacl.jena_unavailable" if unknown else "shacl.jena_input_error",
                    str(error),
                    kind="unverified" if unknown else "input_error",
                )
            )
        except LimitExceeded as stopped:
            issues.exhausted(str(stopped), location="shacl.jena")
    if result is not None:
        issues.retain_completed(result.report)
        if any(issue.kind == "exhausted" for issue in issues):
            result.report = Report(
                "shacl",
                result.report.checked,
                tuple(issues),
                requested=1,
                complete=False,
                children=(result.report,),
                families=result.report.families,
            )
        return result
    return ShaclResult(
        Report("shacl", 0, tuple(issues), requested=1, complete=False),
        mode="targets" if plan is None else plan.mode,
        focus_nodes=focus,
        shapes=() if plan is None else plan.roots,
        preflight=False if plan is None else plan.preflight,
    )
