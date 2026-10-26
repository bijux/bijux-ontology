"""Signed reference policy and bounded SCC interpretation over qualified facts."""

from collections.abc import Iterator
from typing import Literal as Kind

from rdflib.namespace import SH
from rdflib.term import Node

from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import Budget, IssueCollector
from bijux_ontology.rdf.structure import strong_components
from bijux_ontology.shacl.parameters.reading import ParameterReader, count_value
from bijux_ontology.shacl.profiles.types import ShapeDependency

from .local import ShapeState
from .types import ShapeComponent, SignedDependency


def signed_dependency(
    dependency: ShapeDependency,
    states: dict[Node, ShapeState],
    parameters: ParameterReader,
    issues: IssueCollector,
) -> SignedDependency:
    """Classify only justified reference polarity and retained active scope."""
    polarity: Kind["positive", "negative", "nonmonotone"] = "positive"
    if dependency.parameter == SH["not"]:
        polarity = "negative"
    elif dependency.parameter == SH.xone:
        polarity = "nonmonotone"
    elif (
        dependency.parameter == SH.qualifiedValueShape
        and states[dependency.shape].active
        and states[dependency.shape].valid
        and states[dependency.dependency].active
        and states[dependency.dependency].valid
    ):
        maximum_term = parameters.one(dependency.shape, SH.qualifiedMaxCount)
        maximum = (
            None
            if maximum_term is None
            else count_value(maximum_term, parameters.budget)
        )
        disjoint = parameters.boolean(dependency.shape, SH.qualifiedValueShapesDisjoint)
        if maximum is not None:
            polarity = "nonmonotone"
        if disjoint:
            polarity = "nonmonotone"
            issues.append(
                Issue(
                    "shacl.analysis_qualification_unverified",
                    "Qualified sibling exclusions require additional "
                    "semantic dependencies",
                    str(dependency.shape),
                    kind="unverified",
                )
            )
    source, target = (
        states[dependency.shape],
        states[dependency.dependency],
    )
    return SignedDependency(
        dependency,
        polarity,
        source.active and target.active and source.valid and target.valid,
    )


def shape_components(
    states: dict[Node, ShapeState],
    dependencies: list[SignedDependency],
    budget: Budget,
) -> Iterator[ShapeComponent]:
    """Compute active signed SCCs iteratively without assigning recursive truth."""
    nodes = states
    budget.consume("states", 2 * len(nodes))
    forward: dict[Node, list[Node]] = {node: [] for node in nodes}
    backward: dict[Node, list[Node]] = {node: [] for node in nodes}
    for edge in dependencies:
        budget.consume("work")
        if edge.active:
            budget.consume("states", 2)
            forward[edge.dependency.shape].append(edge.dependency.dependency)
            backward[edge.dependency.dependency].append(edge.dependency.shape)
    assigned: dict[Node, int] = {}
    groups: list[list[Node]] = []
    for group in strong_components(
        nodes, forward, backward, budget=budget, max_depth=budget.limits.max_depth
    ):
        budget.consume("states", len(group))
        budget.consume("work", len(group))
        budget.reserve_storage(64 * len(group))
        number = len(groups)
        groups.append(group)
        assigned.update((node, number) for node in group)
    budget.consume("states", len(groups))
    internal: list[list[SignedDependency]] = [[] for _ in groups]
    for edge in dependencies:
        budget.consume("work")
        if (
            edge.active
            and assigned[edge.dependency.shape] == assigned[edge.dependency.dependency]
        ):
            budget.consume("states")
            internal[assigned[edge.dependency.shape]].append(edge)
    for group, edges in zip(groups, internal, strict=True):
        budget.consume("work", len(edges))
        budget.consume("states", 1 + len(group) + len(edges))
        recursive = len(group) > 1 or any(
            e.dependency.shape == e.dependency.dependency for e in edges
        )
        negative = any(e.polarity != "positive" for e in edges)
        boundary: Kind[
            "acyclic", "processor_defined", "negative_recursion_unsupported"
        ] = (
            "negative_recursion_unsupported"
            if recursive and negative
            else "processor_defined"
            if recursive
            else "acyclic"
        )
        component = ShapeComponent(tuple(group), tuple(edges), recursive, boundary)
        yield component
