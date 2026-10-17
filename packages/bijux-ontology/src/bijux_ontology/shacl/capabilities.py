"""Required constraint binding and selected SHACL execution capabilities."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import SH
from rdflib.term import Node

from bijux_ontology.errors import LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.snapshots import copy_graph
from bijux_ontology.rdf.structure import read_rdf_list
from bijux_ontology.reports import Issue, Report
from bijux_ontology.shacl.coverage import ShaclCoverage
from bijux_ontology.shacl.queries import check_function_dependencies

if TYPE_CHECKING:
    from pyshacl.shape import Shape
    from pyshacl.shapes_graph import ShapesGraph


@dataclass(frozen=True)
class ShaclConstraintRequirement:
    """Require a bound supported parameter, optionally from one exact component.

    Execution proof currently covers independently targeted root shapes,
    including PropertyShapes. Nested-only or conditional invocation is unknown.
    """

    shape: str
    parameter: str
    component: str | None = None

    def __post_init__(self) -> None:
        absolute_iri(self.shape)
        absolute_iri(self.parameter)
        if self.component is not None:
            absolute_iri(self.component)


def _selected(
    graph: Graph, discovered: dict[Node, "Shape"], roots: Sequence[str], budget: Budget
) -> dict[Node, "Shape"]:
    """Retain selected roots and all statically referenced constraint shapes."""
    pending: list[Node] = [URIRef(iri) for iri in roots] if roots else list(discovered)
    result = {}
    while pending:
        node = pending.pop()
        if node in result or node not in discovered:
            continue
        budget.consume("states")
        result[node] = discovered[node]
        for predicate in (SH.property, SH.node, SH["not"], SH.qualifiedValueShape):
            pending.extend(graph.objects(node, predicate))
        for predicate in (SH["and"], SH["or"], SH.xone):
            for head in graph.objects(node, predicate):
                pending.extend(
                    read_rdf_list(graph, head, max_items=budget.limits.max_work)
                )
    return result


def _validators(component: object, shape: "Shape") -> set[Node]:
    """Select the same validator role as the maintained custom-component engine."""
    from pyshacl.constraints.constraint_component import CustomConstraintComponent

    if not isinstance(component, CustomConstraintComponent):
        return set()
    if shape.is_property_shape and component.property_validators:
        return set(component.property_validators)
    if not shape.is_property_shape and component.node_validators:
        return set(component.node_validators)
    return set(component.validators)


def _required_parameter(
    requirement: ShaclConstraintRequirement, shape: "Shape", graph: "ShapesGraph"
) -> str | None:
    """Check binding and selected implementation without treating metadata as rules."""
    from pyshacl.constraints import CONSTRAINT_PARAMETERS_MAP
    from pyshacl.constraints.sparql.sparql_based_constraint_components import (
        SPARQLConstraintComponent,
    )

    parameter = URIRef(requirement.parameter)
    values = tuple(graph.graph.objects(shape.node, parameter))
    if not values:
        return "Required parameter is unbound on the selected shape"
    if parameter == SH.sparql and any(
        (node, SH.deactivated, Literal(True)) in graph.graph for node in values
    ):
        return "A required SPARQL constraint is deactivated"
    builtin = CONSTRAINT_PARAMETERS_MAP.get(parameter)
    if builtin is not None:
        if requirement.component not in (None, str(builtin.shacl_constraint_component)):
            return "Required parameter does not belong to the requested component"
        builtin(shape)
        return None
    components = [
        component
        for component in shape.find_custom_constraints()  # type: ignore[no-untyped-call]
        if any(item.path() == parameter for item in component.parameters)
        and requirement.component in (None, str(component.node))
    ]
    if len(components) != 1:
        return "Required parameter has no unique bound supported component"
    component = components[0]
    if not isinstance(component, SPARQLConstraintComponent):
        return "Required component has no supported validator implementation"
    validators = _validators(component, shape)
    if len(validators) != 1:
        return (
            "Required component has unavailable or shadowed validator implementations"
        )
    component.make_validator_for_shape(shape)
    return None


@dataclass(frozen=True)
class CapabilityPlan:
    """Selected executable definitions and incomplete capability diagnostics."""

    nodes: frozenset[Node] | None
    issues: tuple[Issue, ...] = ()


def prepare_capabilities(
    graph: Graph,
    roots: Sequence[str],
    requirements: Sequence[ShaclConstraintRequirement],
    *,
    trusted_sparql: bool,
    limits: Limits,
) -> CapabilityPlan:
    """Resolve selected queries and explicitly required parameter bindings."""
    if not trusted_sparql and not requirements and not roots:
        return CapabilityPlan(None)
    try:
        from pyshacl.errors import (
            ConstraintLoadError,
            ConstraintLoadWarning,
            ShapeLoadError,
        )
        from pyshacl.shapes_graph import ShapesGraph
    except ImportError:
        return CapabilityPlan(
            None,
            (
                Issue(
                    "shacl.capability_unavailable",
                    "Install bijux-ontology[shacl] to inspect execution capabilities",
                    kind="unverified",
                ),
            ),
        )
    budget = Budget(limits)
    issues = IssueCollector(budget)
    definitions: set[Node] = set()
    location = "shapes"
    try:
        supplied = ShapesGraph(copy_graph(graph, standardize_apart=False))
        selected = _selected(
            supplied.graph,
            {shape.node: shape for shape in supplied.shapes},
            roots,
            budget,
        )
        definitions.update(selected)
        for requirement in requirements:
            location = requirement.shape
            budget.consume("work")
            shape = selected.get(URIRef(requirement.shape))
            reason = (
                _required_parameter(requirement, shape, supplied)
                if shape is not None
                else "Required shape is outside the selected authored constraint scope"
            )
            if reason:
                issues.append(
                    Issue(
                        "shacl.constraint_unavailable",
                        reason,
                        requirement.shape,
                        kind="unverified",
                        expected=requirement.parameter,
                        actual=requirement.component,
                    )
                )
        for shape in selected.values():
            location = str(shape.node)
            budget.consume("work")
            if shape.deactivated:
                continue
            nodes = {
                node
                for node in graph.objects(shape.node, SH.sparql)
                if (node, SH.deactivated, Literal(True)) not in graph
            }
            for component in shape.find_custom_constraints():  # type: ignore[no-untyped-call]
                nodes.update(_validators(component, shape))
            definitions.update(nodes)
            if trusted_sparql:
                check_function_dependencies(shape, nodes, budget, issues)
    except (
        ConstraintLoadError,
        ConstraintLoadWarning,
        ShapeLoadError,
        NotImplementedError,
    ) as error:
        issues.append(
            Issue(
                "shacl.constraint_malformed",
                str(error),
                location,
                kind="unverified",
            )
        )
    except LimitExceeded as error:
        issues.exhausted(str(error), location="shapes")
    return CapabilityPlan(frozenset(definitions), tuple(issues))


def constraint_execution(
    requirements: Sequence[ShaclConstraintRequirement], coverage: ShaclCoverage
) -> Report:
    """Count only required root parameters with completed target execution evidence."""
    executed = {str(target.shape) for target in coverage.targets}
    deactivated = {str(shape) for shape in coverage.deactivated_shapes}
    issues = []
    for requirement in requirements:
        if requirement.shape not in executed or not coverage.report.complete:
            issues.append(
                Issue(
                    "shacl.constraint_not_evaluated",
                    "Required shape is deactivated"
                    if requirement.shape in deactivated
                    else "Required parameter has no completed root execution evidence",
                    requirement.shape,
                    kind="unverified",
                    expected=requirement.parameter,
                )
            )
    return Report(
        "shacl-constraint-coverage",
        len(requirements),
        tuple(issues),
        regime="shacl-required-root-constraints",
    )
