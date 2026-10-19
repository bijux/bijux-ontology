"""Optional supplied-shapes SHACL validation with isolated backend execution."""

from collections.abc import Mapping, Sequence
from math import isfinite

from rdflib import Graph

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.sources.types import Payload, RdfSource
from bijux_ontology.shacl.bindings import TermSelection, transport_roles
from bijux_ontology.shacl.capabilities import (
    ShaclConstraintRequirement,
)
from bijux_ontology.shacl.coverage import (
    ShaclCoverageContract,
)
from bijux_ontology.shacl.execution.planning import (
    _metadata,
    _options,
    _prepare_data,
    _prepare_plan,
    _role_graphs,
)
from bijux_ontology.shacl.execution.projection import (
    _incomplete,
    _project_result,
    _retained_result,
)
from bijux_ontology.shacl.execution.results import ShaclResult as ShaclResult
from bijux_ontology.shacl.execution.transport import execute_backend
from bijux_ontology.shacl.findings import ShaclFinding as ShaclFinding
from bijux_ontology.shacl.findings import ShaclSource
from bijux_ontology.shacl.functions import (
    ShaclFunctionCatalog,
)


def validate_shapes(
    data: Graph | PreparedSource,
    shapes: Graph | PreparedSource,
    *,
    ontology: Graph | PreparedSource | None = None,
    graph_name: GraphSelection | None = None,
    union: bool = False,
    imports: Mapping[str, RdfSource | Payload] | None = None,
    trusted_sparql: bool = False,
    trusted_targets: bool = False,
    focus_nodes: Sequence[TermSelection] = (),
    use_shapes: Sequence[TermSelection] = (),
    mode: str = "targets",
    preflight: bool = False,
    allow_empty: bool = False,
    coverage: ShaclCoverageContract | None = None,
    required_constraints: Sequence[ShaclConstraintRequirement] = (),
    functions: ShaclFunctionCatalog | None = None,
    source_usages: Sequence[ShaclSource] = (),
    limits: Limits | None = None,
    timeout: float | None = 30.0,
    budget: Budget | None = None,
    _owned_worker: bool = False,
) -> ShaclResult:
    """Validate supplied graphs with inference, JS, rules and implicit retrieval off.

    ``mode='targets'`` retains normal targets, optionally filtering focus nodes
    or selected shapes. ``mode='pairs'`` deliberately applies supplied shapes to
    supplied focus nodes, bypassing targets. Root selection changes only owned
    Core targets, including supported implicit class targets; referenced nested
    shapes remain active.
    Every selected root must be a shape in the authored graph before targeting.
    Zero discovered shapes are an empty required scope by default. Explicit
    allow_empty=True makes it not applicable, never verified. Raw backend
    conformance and its RDF report remain available either way.
    ``trusted_targets=True`` separately authorizes supported SPARQL targets and
    parameterized target types. Actual target enumeration runs once inside the
    owned worker before Core validation; it does not enable constraint queries,
    rules, JavaScript or ambient function definitions.
    An optional ``coverage`` contract additionally requires evaluated root
    targets; it changes no raw conformance or targetless-shape semantics.
    Literal and owned blank focus terms require explicit ``mode="pairs"``.
    Blank terms must come from the respective PreparedSource export; equal labels
    from other snapshots do not establish identity. Anonymous authored shapes can
    be selected with their owned blank term.
    ``required_constraints`` also requires supported parameter/component bindings
    and completed root execution; unsupported function dependencies never verify.
    An explicit trusted ``functions`` catalog registers only its selected SPARQL
    dependency closure. Constraint query trust remains separate. Arguments and
    results retain RDF terms and ordinary SPARQL unbound/error behavior; no other
    advanced extension is activated. Calls and query execution share the worker
    deadline, with a separate finite function-call/dependency budget.
    Optional ``source_usages`` joins actual prepared JSON-LD selections to named
    RDF edges in this exact graph. String literals and named references support
    predicate, inverse, sequence and alternative joins; unknown blank, list or
    quantified correspondence remains explicit. All possible locations survive.
    Supporting ontology axioms are mixed
    into the backend data graph without running an inference engine. Dataset
    union or graph selection is deliberate; returned conformance covers exactly
    that scope. Backend recursion depth and process deadline are separate limits.
    Optional shared accounting covers controlled preparation and projection;
    opaque maintained-engine CPU remains bounded by the owning process deadline.
    The private owned-worker route requires an already isolated disposable worker
    whose caller enforces the timeout; it starts no nested process.
    """
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("SHACL shared accounting requires a Budget")
    if type(_owned_worker) is not bool:
        raise InputError("Owned SHACL execution flag must be boolean")
    if timeout is not None and (
        type(timeout) not in (int, float) or not isfinite(timeout) or timeout <= 0
    ):
        raise InputError("timeout must be a positive finite number or None")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("SHACL limits conflict with the shared budget")
    plan = _prepare_plan(
        shapes,
        ontology=ontology,
        imports=imports,
        trusted_sparql=trusted_sparql,
        trusted_targets=trusted_targets,
        use_shapes=use_shapes,
        mode=mode,
        preflight=preflight,
        allow_empty=allow_empty,
        coverage=coverage,
        required_constraints=required_constraints,
        functions=functions,
        limits=limits
        if limits is not None
        else (budget.limits if budget is not None else Limits()),
        budget=budget,
    )
    dg, focus = _prepare_data(
        data, plan, focus_nodes, graph_name=graph_name, union=union
    )
    if plan.features:
        result = ShaclResult(
            Report(
                "shacl",
                0,
                plan.features,
                requested=1,
                complete=False,
                families=plan.families,
            ),
            **_metadata(plan, focus),
        )
        return _retained_result(result, budget)
    import json

    roles = transport_roles(
        {"data": dg, **_role_graphs(plan)},
        focus,
        plan.roots,
        plan.limits,
        budget=budget,
    )
    if _owned_worker:
        # The caller already owns a disposable worker and its outer deadline.
        # Execute the same backend there; a nested isolated process would escape
        # the outer POSIX process group when its owner is terminated.
        from bijux_ontology.execution.protocol import json_bound
        from bijux_ontology.shacl.execution.backend import execute

        response = execute(_options(plan, focus), supplied=roles, budget=budget)
        if budget is not None:
            budget.consume("bytes", json_bound(response, budget))
        raw = json.dumps(response, ensure_ascii=True).encode()
        if len(raw) > plan.limits.max_total_bytes:
            raise LimitExceeded("SHACL response exceeds max_total_bytes")
    else:
        payload = json.dumps(
            {"graphs": roles.serialize(format="nquads"), **_options(plan, focus)}
        ).encode()
        if budget is not None:
            budget.consume("bytes", len(payload))
            budget.consume("work", len(payload))
        if len(payload) > plan.limits.max_total_bytes:
            raise LimitExceeded("SHACL request exceeds max_total_bytes")
        try:
            raw = execute_backend(
                payload, cap=plan.limits.max_total_bytes, timeout=timeout
            )
        except (LimitExceeded, ResourceError) as error:
            return _retained_result(_incomplete(plan, error, focus), budget)
    result = _project_result(
        raw,
        plan,
        data,
        dg,
        focus,
        graph_name=graph_name,
        union=union,
        source_usages=source_usages,
    )
    return _retained_result(result, budget)
