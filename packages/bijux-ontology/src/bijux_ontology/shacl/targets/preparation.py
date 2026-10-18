"""Qualify explicitly selected maintained SPARQL target definitions."""

from collections.abc import Sequence

from rdflib import BNode, Graph, Literal, URIRef, Variable
from rdflib.namespace import RDF, SH
from rdflib.term import Node

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.reports import Issue
from bijux_ontology.shacl.authoring.parameters import parameter_name
from bijux_ontology.shacl.capabilities import CapabilityPlan
from bijux_ontology.shacl.queries import analyze_query, check_function_dependencies

_PARAMETER = frozenset(
    (
        RDF.type,
        SH.path,
        SH.optional,
        SH.order,
        SH.name,
        SH.description,
        SH.group,
        SH.datatype,
        SH["class"],
        SH.nodeKind,
    )
)


def _objects(
    graph: Graph, node: Node, predicate: URIRef, budget: Budget
) -> tuple[Node, ...]:
    """Capture already-owned facts while charging each retained reference."""
    values = []
    for value in graph.objects(node, predicate):
        budget.consume("work")
        bound_node(value, budget)
        budget.consume("states")
        values.append(value)
    return tuple(values)


def _roots(graph: Graph, budget: Budget) -> tuple[Node, ...]:
    """Own the root inventory before materializing its traversal container."""
    result = []
    seen: set[Node] = set()
    for root in graph.subjects(SH.target):
        budget.consume("work")
        bound_node(root, budget)
        if root not in seen:
            budget.consume("states")
            seen.add(root)
            result.append(root)
    return tuple(result)


def _target_body(graph: Graph, node: Node, budget: Budget) -> None:
    """Keep other advanced semantics from hiding behind a qualified target."""
    supported = frozenset(
        (
            SH.select,
            SH.ask,
            SH.prefixes,
            SH.parameter,
            SH.labelTemplate,
            SH.name,
            SH.description,
            SH.order,
            SH.group,
        )
    )
    for predicate, value in graph.predicate_objects(node):
        budget.consume("work")
        bound_node(predicate, budget)
        bound_node(value, budget)
        if str(predicate).startswith(str(SH)) and predicate not in supported:
            raise InputError("Target has unsupported advanced semantics")


def _definitions(
    graph: Graph, root: Node, budget: Budget
) -> tuple[tuple[Node, Node, tuple[Node, ...]], ...]:
    """Identify one supported implementation for every actual declaration."""
    result = []
    for declaration in _objects(graph, root, SH.target, budget):
        if not isinstance(declaration, (URIRef, BNode)):
            raise InputError("SHACL target declarations must be resources")
        types = _objects(graph, declaration, RDF.type, budget)
        if any(
            (declaration, p, None) in graph
            for p in (SH.js, SH.jsFunctionName, SH.jsLibrary)
        ):
            raise InputError("JavaScript targets are unsupported")
        definition: Node
        if SH.SPARQLTarget in types:
            if any(kind not in (SH.SPARQLTarget, SH.Target) for kind in types):
                raise InputError("Target has ambiguous implementation kinds")
            definition = declaration
            parameters: tuple[Node, ...] = ()
            if (declaration, SH.parameter, None) in graph:
                raise InputError("Direct targets cannot declare target-type parameters")
        else:
            if (declaration, SH.select, None) in graph or (
                declaration,
                SH.ask,
                None,
            ) in graph:
                raise InputError("Target declaration shadows its target-type query")
            implementations = [
                kind for kind in types if (kind, RDF.type, SH.SPARQLTargetType) in graph
            ]
            if len(implementations) != 1 or any(
                kind not in (*implementations, SH.Target) for kind in types
            ):
                raise InputError("Target has no unique supported SPARQL target type")
            definition = implementations[0]
            parameters = _objects(graph, definition, SH.parameter, budget)
        _target_body(graph, definition, budget)
        selects = _objects(graph, definition, SH.select, budget)
        asks = _objects(graph, definition, SH.ask, budget)
        if len(selects) != 1 or len(asks) > 1:
            raise InputError("Target requires one SELECT and at most one auxiliary ASK")
        names = set()
        for parameter in parameters:
            for predicate, value in graph.predicate_objects(parameter):
                budget.consume("work")
                bound_node(predicate, budget)
                bound_node(value, budget)
                if predicate not in _PARAMETER:
                    raise InputError(
                        "Target parameter has unsupported constraint semantics"
                    )
            paths = _objects(graph, parameter, SH.path, budget)
            if len(paths) != 1 or not isinstance(paths[0], URIRef):
                raise InputError("Target parameter requires one predicate path")
            name = parameter_name(paths[0])
            if name in names:
                raise InputError("Target parameter names must be unique")
            names.add(name)
            values = _objects(graph, declaration, paths[0], budget)
            if len(values) > 1 or any(isinstance(value, BNode) for value in values):
                raise InputError("Target parameters cannot be multivalued or blank")
        budget.consume("states")
        result.append((declaration, definition, parameters))
    return tuple(result)


def prepare_targets(
    graph: Graph,
    roots: Sequence[Node],
    *,
    enabled: bool,
    limits: Limits,
    enabled_functions: frozenset[URIRef] = frozenset(),
    budget: Budget | None = None,
) -> CapabilityPlan:
    """Return selected target capabilities without executing a data query.

    Core parameter guards run in the isolated execution helper. Missing required
    bindings and unsupported richer parameter constraints cannot imply success.
    """
    if type(enabled) is not bool:
        raise InputError("trusted_targets must be a boolean")
    if not enabled:
        return CapabilityPlan(frozenset())
    try:
        from pyshacl.errors import (
            ConstraintLoadError,
            ReportableRuntimeError,
            ShapeLoadError,
        )
        from pyshacl.shape import Shape
        from pyshacl.shapes_graph import ShapesGraph
        from pyshacl.target import SPARQLTargetType
    except ImportError:
        return CapabilityPlan(
            frozenset(),
            (
                Issue(
                    "shacl.target_unavailable",
                    "Install bijux-ontology[shacl] to inspect trusted targets",
                    kind="unverified",
                ),
            ),
        )
    budget = budget if budget is not None else Budget(limits)
    issues = IssueCollector(budget)
    definitions: set[Node] = set()
    normalized: list[tuple[Node, URIRef, Literal]] = []
    try:
        supplied = ShapesGraph(graph)
        selected = roots or _roots(graph, budget)
        for root in selected:
            budget.consume("work")
            if not isinstance(root, (URIRef, BNode)):
                raise InputError("Target roots must be resources")
            shape = Shape(supplied, root)
            if shape.deactivated:
                continue
            for declaration, definition, parameters in _definitions(
                graph, root, budget
            ):
                definitions.update((declaration, definition, *parameters))
                # Public binding enforces the maintained mandatory/optional name
                # semantics; it does not execute the body or register ambient types.
                if definition != declaration:
                    SPARQLTargetType(definition, supplied).bind(shape, declaration)  # type: ignore[no-untyped-call]
                for predicate in (SH.select, SH.ask):
                    for text in _objects(graph, definition, predicate, budget):
                        analysis = analyze_query(shape, definition, text, budget)
                        if predicate == SH.select and (
                            analysis.form != "SelectQuery"
                            or analysis.projection != (Variable("this"),)
                        ):
                            raise InputError("Target SELECT must project exactly ?this")
                        if predicate == SH.ask and analysis.form != "AskQuery":
                            raise InputError(
                                "Target auxiliary ASK requires an ASK query"
                            )
                        normalized.append(
                            (definition, predicate, Literal(analysis.text))
                        )
                check_function_dependencies(
                    shape, (definition,), budget, issues, enabled_functions
                )
    except LimitExceeded as error:
        issues.exhausted(str(error), location="shapes")
    except (
        InputError,
        ConstraintLoadError,
        ReportableRuntimeError,
        ShapeLoadError,
        NotImplementedError,
    ) as error:
        try:
            issues.append(
                Issue(
                    "shacl.target_unavailable", str(error), "shapes", kind="unverified"
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted), location="shapes")
    return CapabilityPlan(frozenset(definitions), tuple(issues), tuple(normalized))
