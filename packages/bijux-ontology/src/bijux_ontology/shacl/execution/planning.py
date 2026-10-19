"""Own fixed SHACL contracts and prepare selected data for either worker lifetime."""

from collections.abc import Iterable, Mapping, Sequence
from typing import cast

from pyparsing import ParseBaseException
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDFS, SH
from rdflib.plugins.sparql.parser import parseQuery
from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.sources.snapshots import (
    PreparedSource,
)
from bijux_ontology.rdf.sources.types import Payload, RdfSource
from bijux_ontology.shacl.execution.capabilities import prepare_capabilities
from bijux_ontology.shacl.paths import decode_path
from bijux_ontology.shacl.sparql.functions import (
    ShaclFunctionCatalog,
    prepare_functions,
)
from bijux_ontology.shacl.targets.coverage import ShaclCoverageContract
from bijux_ontology.shacl.targets.preparation import prepare_targets
from bijux_ontology.shacl.validation.bindings import TermSelection, resolve_terms
from bijux_ontology.shacl.validation.contracts import (
    ShaclConstraintRequirement,
    ShaclPlan,
)
from bijux_ontology.shacl.validation.roles import prepare_role, select_roots

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
) -> ShaclPlan:
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
    sg = prepare_role(
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
        else prepare_role(
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
        select_roots(sg, selected_shapes, mode),
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
    return ShaclPlan(
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
