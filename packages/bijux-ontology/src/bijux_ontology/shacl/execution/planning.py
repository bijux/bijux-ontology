"""Own fixed SHACL contracts and prepare selected data for either worker lifetime."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, cast

from pyparsing import ParseBaseException
from rdflib import BNode, Dataset, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SH
from rdflib.plugins.sparql.parser import parseQuery
from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.sources.loading import _charge_graph
from bijux_ontology.rdf.sources.snapshots import (
    GraphSelection,
    PreparedSource,
    copy_graph,
)
from bijux_ontology.rdf.sources.types import Payload, RdfSource
from bijux_ontology.shacl.bindings import TermSelection, resolve_terms
from bijux_ontology.shacl.capabilities import (
    ShaclConstraintRequirement,
    prepare_capabilities,
)
from bijux_ontology.shacl.coverage import (
    ShaclCoverageContract,
)
from bijux_ontology.shacl.functions import (
    FunctionPlan,
    ShaclFunctionCatalog,
    prepare_functions,
)
from bijux_ontology.shacl.paths import decode_path
from bijux_ontology.shacl.targets.preparation import prepare_targets
from bijux_ontology.vocabulary.sources.loading import load_vocabulary

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


def _query_forms(value: object) -> set[str]:
    if isinstance(value, CompValue):
        return {value.name} | set().union(
            *(_query_forms(item) for item in value.values())
        )
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        return set().union(*(_query_forms(item) for item in value))
    return set()


def _features(
    shapes: Graph,
    *,
    trusted_sparql: bool,
    trusted_targets: bool,
    nodes: frozenset[Node] | None = None,
) -> tuple[Issue, ...]:
    issues = []
    for predicate in sorted(_UNSUPPORTED, key=str):
        if predicate == SH.target and trusted_targets:
            continue
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
    budget: Budget | None = None,
) -> Graph:
    if isinstance(source, PreparedSource):
        graph = source._read(graph_name, union=union, budget=budget)
        _charge_graph(
            graph, budget if budget is not None else Budget(limits), limits.max_bytes
        )
        if resolve_imports:
            graph = copy_graph(
                graph, max_triples=limits.max_triples, standardize_apart=False
            )
    elif graph_name is None and not union and not isinstance(source, Dataset):
        _charge_graph(
            source, budget if budget is not None else Budget(limits), limits.max_bytes
        )
        graph = copy_graph(
            source, max_triples=limits.max_triples, standardize_apart=False
        )
    else:
        graph = prepare_source(source, limits=limits, budget=budget)._read(
            graph_name, union=union, budget=budget
        )
    if resolve_imports and next(graph.objects(None, OWL.imports), None) is not None:
        closure = load_vocabulary(graph, imports=imports, limits=limits, budget=budget)
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
        SH.target,
    ):
        for shape in tuple(graph.subjects(predicate)):
            if mode == "pairs" or shape not in roots:
                graph.remove((shape, predicate, None))
    return graph


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
    trusted_targets: bool
    budget: Budget | None = None


def _prepare_plan(
    shapes: Graph | PreparedSource,
    *,
    ontology: Graph | PreparedSource | None,
    imports: Mapping[str, RdfSource | Payload] | None,
    trusted_sparql: bool,
    trusted_targets: bool,
    use_shapes: Sequence[TermSelection],
    mode: str,
    preflight: bool,
    allow_empty: bool,
    coverage: ShaclCoverageContract | None,
    required_constraints: Sequence[ShaclConstraintRequirement],
    functions: ShaclFunctionCatalog | None,
    limits: Limits,
    budget: Budget | None = None,
) -> _ShapePlan:
    """Own and inspect every fixed semantic input exactly once per plan."""
    if functions is not None and not isinstance(functions, ShaclFunctionCatalog):
        raise InputError("functions requires an explicit ShaclFunctionCatalog")
    selected = limits
    families = (
        "shacl-core",
        *(("shacl-sparql",) if trusted_sparql else ()),
        *(("shacl-targets",) if trusted_targets else ()),
        *(("meta-shacl",) if preflight else ()),
        *(("shacl-functions",) if functions is not None else ()),
    )
    if any(
        type(value) is not bool
        for value in (trusted_sparql, trusted_targets, preflight, allow_empty)
    ):
        raise InputError(
            "SHACL trust, preflight and allow_empty options must be booleans"
        )
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
        budget=budget,
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
            budget=budget,
        )
    )
    if budget is not None:
        budget.consume("work", len(sg) * (len(_UNSUPPORTED) + 4))
        budget.consume("states", len(sg))
    selected_shapes = resolve_terms(
        use_shapes, shapes, sg, shapes=True, limits=selected, budget=budget
    )
    for node in selected_shapes:
        if (node, None, None) not in sg and (None, None, node) not in sg:
            raise InputError("Selected shape is absent", location=str(node))
    function_plan = prepare_functions(functions, selected, budget=budget)
    capabilities = prepare_capabilities(
        sg,
        selected_shapes,
        required_constraints,
        trusted_sparql=trusted_sparql,
        limits=selected,
        enabled_functions=frozenset(URIRef(name) for name in function_plan.names),
        inspect_targets=trusted_targets,
        budget=budget,
    )
    targets = prepare_targets(
        _select_roots(sg, selected_shapes, mode),
        selected_shapes,
        enabled=trusted_targets,
        limits=selected,
        budget=budget,
        enabled_functions=frozenset(URIRef(name) for name in function_plan.names),
    )
    for query_node, predicate, text in (*capabilities.queries, *targets.queries):
        sg.set((query_node, predicate, text))
    # Missing backend discovery cannot classify target bodies as constraints.
    # Capability diagnostics already prevent execution in that case.
    features = (
        (
            ()
            if trusted_targets and capabilities.nodes is None
            else _features(
                sg,
                trusted_sparql=trusted_sparql,
                trusted_targets=trusted_targets,
                nodes=capabilities.nodes,
            )
        )
        + capabilities.issues
        + targets.issues
        + function_plan.issues
    )
    path_budget = budget if budget is not None else Budget(selected)
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
        trusted_targets,
        budget,
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
    dg = _role(
        data, graph_name=graph_name, union=union, limits=plan.limits, budget=plan.budget
    )
    focus = resolve_terms(focus_nodes, data, dg, limits=plan.limits, budget=plan.budget)
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
        "trusted_targets": plan.trusted_targets,
    }
