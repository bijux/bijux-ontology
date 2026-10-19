"""Owned maintained Jena validation returning the ordinary SHACL evidence type."""

from collections.abc import Mapping, Sequence
from threading import Event

from rdflib import Graph

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.json.paths import bound_index
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.sources.types import Payload, RdfSource
from bijux_ontology.shacl.bindings import TermSelection, transport_roles
from bijux_ontology.shacl.capabilities import ShaclConstraintRequirement
from bijux_ontology.shacl.evidence.findings import ShaclSource
from bijux_ontology.shacl.evidence.results import ShaclResult
from bijux_ontology.shacl.execution.planning import (
    _options,
    _prepare_data,
    _role_graphs,
    _ShapePlan,
)
from bijux_ontology.shacl.execution.projection import _project_result
from bijux_ontology.shacl.execution.session import _payload, _prepay_graph
from bijux_ontology.shacl.targets.coverage import ShaclCoverageContract

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
    plan: _ShapePlan | None = None
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
        original_graph, original_union = graph_name, union
        if isinstance(data, PreparedSource) and (
            data.graph_name is not None or data.union
        ):
            original_graph, original_union = data.graph_name, data.union
        _prepay_graph(
            data._read(graph_name, union=union, budget=operation)
            if isinstance(data, PreparedSource)
            else data,
            operation,
        )
        dg, focus = _prepare_data(
            data,
            plan,
            focus_nodes,
            graph_name=graph_name,
            union=union,
        )
        supplied_sources = capture(source_usages, operation)
        operation.consume("bytes", 512 + 256 * len(supplied_sources))
        owned_sources: list[ShaclSource] = []
        for source in supplied_sources:
            if type(source) is not ShaclSource:
                raise InputError("Expected ordinary prepared SHACL source usages")
            prepared, paths, label = source.prepared, source.paths, source.source
            bound_text(label, operation)
            owned_paths: list[tuple[str | int, ...]] = []
            for path in capture(paths, operation):
                members: list[str | int] = []
                for member in capture(path, operation):
                    if type(member) is str:
                        bound_text(member, operation)
                    elif type(member) is int:
                        bound_index(member, operation)
                    else:
                        raise InputError("Source paths require string/integer members")
                    members.append(member)
                owned_paths.append(tuple(members))
            owned_sources.append(ShaclSource(prepared, tuple(owned_paths), label))
        selected_sources = tuple(owned_sources)
        if plan.features:
            issues.extend(plan.features)
        else:
            operation.consume("bytes", 1024)
            roles = transport_roles(
                {"data": dg, **_role_graphs(plan)},
                focus,
                plan.roots,
                operation.limits,
                budget=operation,
            )
            options = _options(plan, focus)
            options.update(
                protocol=1,
                operation="shacl",
                trusted_sparql=trusted_sparql,
                required_constraints=[
                    {
                        "shape": item.shape,
                        "parameter": item.parameter,
                        "component": item.component,
                    }
                    for item in plan.required_constraints
                ],
            )
            payload = _payload(roles, options, operation)
            raw = invoke_jena(backend, payload, operation, heap_mb)
            result = _project_result(
                raw,
                plan,
                dg,
                dg,
                focus,
                graph_name=original_graph,
                union=original_union,
                source_usages=selected_sources,
            )
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
