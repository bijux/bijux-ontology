"""Optional supplied-shapes SHACL validation with isolated backend execution."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from math import isfinite
from typing import Any, cast

from pyparsing import ParseBaseException
from rdflib import BNode, Dataset, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SH, XSD
from rdflib.plugins.sparql.parser import parseQuery
from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.term import Node

from bijux_ontology._json import decode_json
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.loading import _charge_graph
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource, copy_graph
from bijux_ontology.rdf.sources import Payload, RdfSource
from bijux_ontology.reports import Issue, IssueKind, Report, Severity, combine_reports
from bijux_ontology.shacl.bindings import TermSelection, resolve_terms, transport_roles
from bijux_ontology.shacl.capabilities import (
    ShaclConstraintRequirement,
    constraint_execution,
    prepare_capabilities,
)
from bijux_ontology.shacl.coverage import (
    ShaclCoverage,
    ShaclCoverageContract,
    read_coverage,
)
from bijux_ontology.shacl.findings import ShaclFinding as ShaclFinding
from bijux_ontology.shacl.findings import ShaclSource, project_findings
from bijux_ontology.shacl.functions import (
    FunctionPlan,
    ShaclFunctionCatalog,
    prepare_functions,
)
from bijux_ontology.shacl.paths import decode_path
from bijux_ontology.shacl.transport import execute_backend
from bijux_ontology.vocabulary.loading import load_vocabulary

_UNSUPPORTED = frozenset(
    (
        SH.rule,
        SH.js,
        SH.jsFunctionName,
        SH.jsLibrary,
        SH.jsLibraryURL,
        SH.expression,
        SH.target,
    )
)


class ShaclResult:
    """Raw conformance and owned standards-shaped evidence beside generic policy.

    Function counters record invocation attempts, including SPARQL errors;
    they do not assert that every invocation returned a bound RDF term.
    """

    def __init__(
        self,
        report: Report,
        *,
        conforms: bool | None = None,
        backend: str = "unavailable",
        graph: Graph | None = None,
        findings: tuple[ShaclFinding, ...] = (),
        root_findings: tuple[ShaclFinding, ...] = (),
        findings_complete: bool = True,
        registered_functions: tuple[str, ...] = (),
        function_calls: tuple[tuple[str, int], ...] = (),
        mode: str = "targets",
        focus_nodes: tuple[TermSelection, ...] = (),
        shapes: tuple[TermSelection, ...] = (),
        preflight: bool = False,
        shapes_count: int | None = None,
        coverage: ShaclCoverage | None = None,
        conformance_report: Report | None = None,
        constraints: Report | None = None,
    ) -> None:
        self.report = report
        self.conforms = conforms
        self.backend = backend
        self.findings = findings
        self.root_findings = root_findings
        self.findings_complete = findings_complete
        self.registered_functions = registered_functions
        self.function_calls = function_calls
        self.mode = mode
        self.focus_nodes = focus_nodes
        self.shapes = shapes
        self.preflight = preflight
        self.shapes_count = shapes_count
        self.coverage = coverage
        self.conformance_report = conformance_report or report
        self.constraints = constraints
        self._graph = Graph()
        if graph is not None:
            self._graph += graph

    def report_graph(self) -> Graph:
        """Export a mutable copy with RDF result identities preserved."""
        graph = Graph()
        graph += self._graph
        return graph


def _query_forms(value: object) -> set[str]:
    if isinstance(value, CompValue):
        return {value.name} | set().union(
            *(_query_forms(item) for item in value.values())
        )
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        return set().union(*(_query_forms(item) for item in value))
    return set()


def _features(
    shapes: Graph, *, trusted_sparql: bool, nodes: frozenset[Node] | None = None
) -> tuple[Issue, ...]:
    issues = []
    for predicate in sorted(_UNSUPPORTED, key=str):
        if any(nodes is None or node in nodes for node in shapes.subjects(predicate)):
            issues.append(
                Issue(
                    "shacl.feature_unverified",
                    "Requested SHACL extension is outside the supported Core boundary",
                    str(predicate),
                    kind="unverified",
                )
            )
    for predicate in (SH.select, SH.ask):
        for subject, query in shapes.subject_objects(predicate):
            if nodes is not None and subject not in nodes:
                continue
            if not trusted_sparql:
                issues.append(
                    Issue(
                        "shacl.sparql_unverified",
                        "SHACL-SPARQL requires explicit trusted_sparql=True",
                        str(subject),
                        kind="unverified",
                    )
                )
                continue
            if not isinstance(query, Literal):
                raise InputError(
                    "SHACL query text must be a literal", location=str(subject)
                )
            try:
                forms = _query_forms(parseQuery(str(query)))
            except ParseBaseException as error:
                # Parser failures describe authored query input; query execution
                # remains in the child process and is never attempted here.
                raise InputError(
                    "SHACL query could not be parsed", location=str(subject)
                ) from error
            if forms.intersection({"ServiceGraphPattern", "DatasetClause"}):
                raise InputError(
                    "SHACL queries cannot use SERVICE or dataset retrieval",
                    location=str(subject),
                )
    return tuple(issues)


def _role(
    source: Graph | PreparedSource,
    *,
    graph_name: GraphSelection | None,
    union: bool,
    limits: Limits,
    imports: Mapping[str, RdfSource | Payload] | None = None,
    resolve_imports: bool = False,
) -> Graph:
    if isinstance(source, PreparedSource):
        graph = source._read(graph_name, union=union)
        _charge_graph(graph, Budget(limits), limits.max_bytes)
        if resolve_imports:
            graph = copy_graph(
                graph, max_triples=limits.max_triples, standardize_apart=False
            )
    elif graph_name is None and not union and not isinstance(source, Dataset):
        _charge_graph(source, Budget(limits), limits.max_bytes)
        graph = copy_graph(
            source, max_triples=limits.max_triples, standardize_apart=False
        )
    else:
        graph = prepare_source(source, limits=limits).graph(graph_name, union=union)
    if resolve_imports and next(graph.objects(None, OWL.imports), None) is not None:
        closure = load_vocabulary(graph, imports=imports, limits=limits)
        for imported in closure.imports:
            graph += imported
    return graph


def _select_roots(
    shapes: Graph, selected: tuple[URIRef | BNode | Literal, ...], mode: str
) -> Graph:
    """Select supported Core targets without disabling referenced nested shapes.

    The backend's use_shapes filter omits nested PropertyShapes when only their
    parent is selected. Altering only owned Core target declarations preserves
    those constraints. Pair mode deliberately replaces root targeting.
    """
    if not selected:
        return shapes
    graph = copy_graph(shapes, standardize_apart=False)
    roots = set(selected)
    # Match the maintained backend's finite class-target discovery, including
    # its built-in OWL class declaration, before narrowing root obligations.
    class_types = {RDFS.Class, OWL.Class} | set(
        shapes.subjects(RDFS.subClassOf, RDFS.Class)
    )
    for kind in class_types:
        for shape in tuple(graph.subjects(RDF.type, kind)):
            graph.remove((shape, RDF.type, kind))
            graph.add((shape, SH.targetClass, shape))
    for predicate in (
        SH.targetClass,
        SH.targetNode,
        SH.targetSubjectsOf,
        SH.targetObjectsOf,
    ):
        for shape in tuple(graph.subjects(predicate)):
            if mode == "pairs" or shape not in roots:
                graph.remove((shape, predicate, None))
    return graph


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


@dataclass(frozen=True)
class _ShapePlan:
    """One owned authored/effective shape and fixed target/options contract."""

    shapes: Graph
    ontology: Graph | None
    roots: tuple[URIRef | BNode | Literal, ...]
    function_plan: FunctionPlan
    has_functions: bool
    features: tuple[Issue, ...]
    families: tuple[str, ...]
    limits: Limits
    mode: str
    preflight: bool
    allow_empty: bool
    coverage: ShaclCoverageContract | None
    required_constraints: tuple[ShaclConstraintRequirement, ...]


def _prepare_plan(
    shapes: Graph | PreparedSource,
    *,
    ontology: Graph | PreparedSource | None,
    imports: Mapping[str, RdfSource | Payload] | None,
    trusted_sparql: bool,
    use_shapes: Sequence[TermSelection],
    mode: str,
    preflight: bool,
    allow_empty: bool,
    coverage: ShaclCoverageContract | None,
    required_constraints: Sequence[ShaclConstraintRequirement],
    functions: ShaclFunctionCatalog | None,
    limits: Limits,
) -> _ShapePlan:
    """Own and inspect every fixed semantic input exactly once per plan."""
    if functions is not None and not isinstance(functions, ShaclFunctionCatalog):
        raise InputError("functions requires an explicit ShaclFunctionCatalog")
    selected = limits
    families = (
        "shacl-core",
        *(("shacl-sparql",) if trusted_sparql else ()),
        *(("meta-shacl",) if preflight else ()),
        *(("shacl-functions",) if functions is not None else ()),
    )
    if any(
        type(value) is not bool for value in (trusted_sparql, preflight, allow_empty)
    ):
        raise InputError("trusted_sparql, preflight and allow_empty must be booleans")
    if coverage is not None and not isinstance(coverage, ShaclCoverageContract):
        raise InputError("coverage requires a ShaclCoverageContract")
    if isinstance(cast(object, required_constraints), (str, bytes)) or any(
        not isinstance(item, ShaclConstraintRequirement)
        for item in required_constraints
    ):
        raise InputError("required_constraints requires typed constraint requirements")
    required_constraints = tuple(dict.fromkeys(required_constraints))
    if mode not in ("targets", "pairs"):
        raise InputError("SHACL mode must be targets or pairs")
    if mode == "pairs" and not use_shapes:
        raise InputError("Pairs mode requires focus_nodes and use_shapes")
    sg = _role(
        shapes,
        graph_name=None,
        union=False,
        limits=selected,
        imports=imports,
        resolve_imports=True,
    )
    og = (
        None
        if ontology is None
        else _role(
            ontology,
            graph_name=None,
            union=False,
            limits=selected,
            imports=imports,
            resolve_imports=True,
        )
    )
    selected_shapes = resolve_terms(
        use_shapes, shapes, sg, shapes=True, limits=selected
    )
    for node in selected_shapes:
        if (node, None, None) not in sg and (None, None, node) not in sg:
            raise InputError("Selected shape is absent", location=str(node))
    function_plan = prepare_functions(functions, selected)
    capabilities = prepare_capabilities(
        sg,
        selected_shapes,
        required_constraints,
        trusted_sparql=trusted_sparql,
        limits=selected,
        enabled_functions=frozenset(URIRef(name) for name in function_plan.names),
    )
    for query_node, predicate, text in capabilities.queries:
        sg.set((query_node, predicate, text))
    features = (
        _features(sg, trusted_sparql=trusted_sparql, nodes=capabilities.nodes)
        + capabilities.issues
        + function_plan.issues
    )
    path_budget = Budget(selected)
    for shape, path_node in sg.subject_objects(SH.path):
        if capabilities.nodes is not None and shape not in capabilities.nodes:
            continue
        path = decode_path(sg, path_node, budget=path_budget)
        if path.limitation is not None:
            features += (
                Issue(
                    "shacl.path_unverified",
                    path.limitation,
                    str(shape),
                    kind="unverified",
                ),
            )
    if selected_shapes and any(
        (kind, RDFS.subClassOf, RDFS.Class) in sg
        for kind in (SH.NodeShape, SH.PropertyShape)
    ):
        features += (
            Issue(
                "shacl.implicit_target_unverified",
                "Shape kinds used as implicit target classes are unsupported "
                "during root selection",
                kind="unverified",
            ),
        )
    return _ShapePlan(
        sg,
        og,
        selected_shapes,
        function_plan,
        functions is not None,
        features,
        families,
        selected,
        mode,
        preflight,
        allow_empty,
        coverage,
        required_constraints,
    )


def _metadata(
    plan: _ShapePlan, focus: tuple[URIRef | BNode | Literal, ...]
) -> dict[str, Any]:
    """Keep caller-owned RDF term objects as the public selection witnesses."""
    return {
        "mode": plan.mode,
        "focus_nodes": tuple(
            str(node) if isinstance(node, URIRef) else node for node in focus
        ),
        "shapes": tuple(
            str(node) if isinstance(node, URIRef) else node for node in plan.roots
        ),
        "preflight": plan.preflight,
    }


def _prepare_data(
    data: Graph | PreparedSource,
    plan: _ShapePlan,
    focus_nodes: Sequence[TermSelection],
    *,
    graph_name: GraphSelection | None,
    union: bool,
) -> tuple[Graph, tuple[URIRef | BNode | Literal, ...]]:
    """Resolve one independent dataset and its exact owned focus terms."""
    if plan.mode == "pairs" and not focus_nodes:
        raise InputError("Pairs mode requires focus_nodes and use_shapes")
    if plan.mode == "targets" and focus_nodes and plan.roots:
        raise InputError(
            "Combined node/shape selection bypasses targets; choose mode='pairs'"
        )
    if not isinstance(data, (Graph, PreparedSource)):
        raise InputError("SHACL data requires a graph or prepared source")
    dg = _role(data, graph_name=graph_name, union=union, limits=plan.limits)
    focus = resolve_terms(focus_nodes, data, dg, limits=plan.limits)
    if plan.mode == "targets" and any(not isinstance(node, URIRef) for node in focus):
        raise InputError("Literal and blank focus terms require explicit pairs mode")
    return dg, focus


def _role_graphs(plan: _ShapePlan) -> dict[str, Graph]:
    """Keep authored shape identity separate from executable root targets."""
    graphs = {
        "shapes": _select_roots(plan.shapes, plan.roots, plan.mode),
        "authored-shapes": plan.shapes,
    }
    if plan.ontology is not None:
        graphs["ontology"] = plan.ontology
    if plan.has_functions:
        graphs["functions"] = plan.function_plan.graph
    return graphs


def _options(
    plan: _ShapePlan, focus: tuple[URIRef | BNode | Literal, ...]
) -> dict[str, object]:
    """Encode only the fixed supported backend options and per-job focus filter."""
    return {
        "has_ontology": plan.ontology is not None,
        "limits": asdict(plan.limits),
        "max_depth": plan.limits.max_depth,
        "focus_nodes": tuple(str(node) for node in focus)
        if plan.mode == "targets"
        else (),
        "selected_shapes": bool(plan.roots),
        "pair_mode": plan.mode == "pairs",
        "binding_count": len(focus) + len(plan.roots),
        "preflight": plan.preflight,
        "coverage": plan.coverage is not None or bool(plan.required_constraints),
        "functions": plan.function_plan.names,
    }


def _incomplete(
    plan: _ShapePlan, error: Exception, focus: tuple[URIRef | BNode | Literal, ...] = ()
) -> ShaclResult:
    """Retain an incomplete operation without inventing raw conformance."""
    kind: IssueKind = "exhausted" if isinstance(error, LimitExceeded) else "input_error"
    return ShaclResult(
        Report(
            "shacl",
            0,
            (Issue("shacl.execution_incomplete", str(error), kind=kind),),
            requested=1,
            complete=False,
            families=plan.families,
        ),
        **_metadata(plan, focus),
    )


def _project_result(
    raw: bytes,
    plan: _ShapePlan,
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
    metadata = _metadata(plan, focus)
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
        issue_kind: IssueKind = "exhausted" if status == "exhausted" else "input_error"
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
    report_graph = prepare_source(raw_report, format="nt", limits=selected).graph()
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
    )
    issues = []
    severity_names: dict[Node | None, Severity] = {
        SH.Info: "info",
        SH.Warning: "warning",
    }
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
                "Result projection is incomplete; inspect the retained raw RDF report",
                kind="unverified",
            )
        )
    for warning in warnings:
        issues.append(Issue("shacl.backend_warning", str(warning), kind="unverified"))
    if not shapes_count and not allow_empty:
        issues.append(
            Issue(
                "input.empty_scope",
                "The supplied shapes graph contains no discovered SHACL shapes",
                "shapes",
                kind="input_error",
            )
        )
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
    evaluated_coverage = (
        read_coverage(
            response.get("coverage"), coverage or ShaclCoverageContract(), selected
        )
        if coverage is not None or required_constraints
        else None
    )
    constraint_report = (
        constraint_execution(required_constraints, evaluated_coverage)
        if required_constraints and evaluated_coverage is not None
        else None
    )
    additional = tuple(
        item
        for item in (
            evaluated_coverage.report if evaluated_coverage is not None else None,
            constraint_report,
        )
        if item is not None
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


def validate_shapes(
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
    functions: ShaclFunctionCatalog | None = None,
    source_usages: Sequence[ShaclSource] = (),
    limits: Limits | None = None,
    timeout: float | None = 30.0,
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
    """
    if timeout is not None and (
        type(timeout) not in (int, float) or not isfinite(timeout) or timeout <= 0
    ):
        raise InputError("timeout must be a positive finite number or None")
    plan = _prepare_plan(
        shapes,
        ontology=ontology,
        imports=imports,
        trusted_sparql=trusted_sparql,
        use_shapes=use_shapes,
        mode=mode,
        preflight=preflight,
        allow_empty=allow_empty,
        coverage=coverage,
        required_constraints=required_constraints,
        functions=functions,
        limits=limits if limits is not None else Limits(),
    )
    dg, focus = _prepare_data(
        data, plan, focus_nodes, graph_name=graph_name, union=union
    )
    if plan.features:
        return ShaclResult(
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
    import json

    roles = transport_roles(
        {"data": dg, **_role_graphs(plan)}, focus, plan.roots, plan.limits
    )
    payload = json.dumps(
        {"graphs": roles.serialize(format="trig"), **_options(plan, focus)}
    ).encode()
    if len(payload) > plan.limits.max_total_bytes:
        raise LimitExceeded("SHACL request exceeds max_total_bytes")
    try:
        raw = execute_backend(payload, cap=plan.limits.max_total_bytes, timeout=timeout)
    except (LimitExceeded, ResourceError) as error:
        return _incomplete(plan, error, focus)
    return _project_result(
        raw,
        plan,
        data,
        dg,
        focus,
        graph_name=graph_name,
        union=union,
        source_usages=source_usages,
    )
