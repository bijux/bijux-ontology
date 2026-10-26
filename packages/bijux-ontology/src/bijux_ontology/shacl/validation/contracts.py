"""Fixed owned SHACL shapes, selection options and required execution contracts."""

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from rdflib import BNode, Graph, Literal, URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import (
    Issue,
    Report,
)
from bijux_ontology.execution.inputs import capture
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.json.paths import bound_index
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.shacl.sparql.functions import (
    FunctionPlan,
    ShaclFunctionCatalog,
)
from bijux_ontology.shacl.targets.coverage import ShaclCoverage, ShaclCoverageContract


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


@dataclass(frozen=True)
class ShaclPlan:
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


def selection_metadata(
    plan: ShaclPlan, focus: tuple[URIRef | BNode | Literal, ...]
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


def request_options(
    plan: ShaclPlan, focus: tuple[URIRef | BNode | Literal, ...]
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


def capture_contract(
    coverage: ShaclCoverageContract | None,
    requirements: Sequence[ShaclConstraintRequirement],
    functions: ShaclFunctionCatalog | None,
    budget: Budget,
) -> tuple[ShaclConstraintRequirement, ...]:
    """Qualify caller contract scalars before deduplication or later IRI allocation."""
    captured = capture(requirements, budget)
    for requirement in captured:
        if not isinstance(requirement, ShaclConstraintRequirement):
            raise InputError("Expected typed SHACL constraint requirements")
        for value in (requirement.shape, requirement.parameter, requirement.component):
            if value is not None:
                bound_text(value, budget)
    if coverage is not None:
        if not isinstance(coverage, ShaclCoverageContract):
            raise InputError("coverage requires a ShaclCoverageContract")
        bound_index(coverage.minimum_focus, budget)
        for value in capture(coverage.focus_nodes, budget):
            bound_text(value, budget)
        for pair in capture(coverage.pairs, budget):
            for value in pair:
                bound_text(value, budget)
    if functions is not None:
        if not isinstance(functions, ShaclFunctionCatalog):
            raise InputError("functions requires a ShaclFunctionCatalog")
        for value in capture(functions.selected, budget):
            bound_text(value, budget)
    return captured


def constraint_execution(
    requirements: Sequence[ShaclConstraintRequirement],
    coverage: ShaclCoverage,
    *,
    budget: Budget | None = None,
) -> Report:
    """Count only required root parameters with completed target execution evidence."""
    issues = IssueCollector(budget) if budget is not None else []
    try:
        if budget is not None:
            budget.consume(
                "work",
                len(coverage.targets)
                + len(coverage.deactivated_shapes)
                + len(requirements),
            )
            budget.consume(
                "states",
                len(coverage.targets)
                + len(coverage.deactivated_shapes)
                + len(requirements),
            )
        executed = {str(target.shape) for target in coverage.targets}
        deactivated = {str(shape) for shape in coverage.deactivated_shapes}
        for requirement in requirements:
            if requirement.shape not in executed or not coverage.report.complete:
                issues.append(
                    Issue(
                        "shacl.constraint_not_evaluated",
                        "Required shape is deactivated"
                        if requirement.shape in deactivated
                        else "Required parameter has no completed root "
                        "execution evidence",
                        requirement.shape,
                        kind="unverified",
                        expected=requirement.parameter,
                    )
                )
    except LimitExceeded:
        if not isinstance(issues, IssueCollector):
            raise
        issues.exhausted("Required constraint coverage exhausted its shared budget")
    return Report(
        "shacl-constraint-coverage",
        len(requirements),
        tuple(issues),
        regime="shacl-required-root-constraints",
    )
