"""Compose finite local, recursion and necessary-root analysis with exact coverage."""

from rdflib import BNode, Graph
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    retain_report,
)
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.shacl.parameters.reading import ParameterReader
from bijux_ontology.shacl.profiles.references import ReferenceReader
from bijux_ontology.shacl.profiles.types import ShapeProfile

from .local import LocalConstraints
from .propagation import root_conflicts
from .recursion import shape_components, signed_dependency
from .types import RootConflict, ShapeAnalysis, ShapeComponent, SignedDependency


def analyze_shape_profile(
    profile: ShapeProfile,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ShapeAnalysis:
    """Analyze finite local constraints and active dependencies without loading data.

    Structural extraction is the required preparation; its supplied-case behavior
    report is independent of these corpus-free checks. Local contradiction
    records are conditional on invoking that shape. Only necessary conflicts on
    explicitly selected roots become finding diagnostics. No conflict found is
    never a general satisfiability certificate. Recursion remains explicitly
    processor-defined, including positive cycles.
    """
    if not isinstance(profile, ShapeProfile):
        raise InputError("Shape analysis requires a dependency-aware ShapeProfile")
    if (
        limits is not None
        and not isinstance(limits, Limits)
        or budget is not None
        and not isinstance(budget, Budget)
    ):
        raise InputError("Shape analysis requires typed limits and budget")
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("Shape analysis limits conflict with the shared budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    parameters = ParameterReader(Graph(), operation)
    local = LocalConstraints(parameters, issues)
    dependencies: list[SignedDependency] = []
    components: list[ShapeComponent] = []
    conflicts: list[RootConflict] = []
    roots: tuple[Node, ...] = ()
    complete = False
    stages = 0
    try:
        if type(profile.roots) is not tuple or type(profile.dependencies) is not tuple:
            raise InputError("Shape profile inventories require immutable tuples")
        if not isinstance(profile.structural_report, Report) or (
            profile.source is not None
            and not isinstance(profile.source, PreparedSource)
        ):
            raise InputError(
                "Shape profiles require typed source and structural report"
            )
        operation.consume("states", len(profile.roots) + len(profile.dependencies))
        retain_report(profile.structural_report, operation, issues)
        if (
            not profile.structural_report.verified
            or profile.source is None
            or not profile.roots
        ):
            issues.append(
                Issue(
                    "shacl.analysis_profile_unverified",
                    "Analysis requires completed selected structural extraction",
                    kind="unverified",
                )
            )
        else:
            parameters.graph = profile.source._read(budget=operation)
            # Qualify the existing selected RDF scalars before maintained list
            # traversal or dictionary lookups can copy their lexical metadata.
            owned_blanks: set[int] = set()
            for triple in parameters.graph:
                operation.consume("work")
                operation.consume("triples")
                for term in triple:
                    bound_node(term, operation)
                    if isinstance(term, BNode) and id(term) not in owned_blanks:
                        operation.consume("states")
                        owned_blanks.add(id(term))
            nodes = ReferenceReader(parameters, owned_blanks).qualify(
                profile.roots, profile.dependencies
            )
            roots = profile.roots
            for selected_node in nodes:
                operation.consume("work")
                local.scan(selected_node)
            for dependency in profile.dependencies:
                operation.consume("work")
                operation.consume("states")
                try:
                    dependencies.append(
                        signed_dependency(dependency, local.states, parameters, issues)
                    )
                except InputError as error:
                    if isinstance(error, LimitExceeded):
                        raise
                    issues.append(
                        Issue(
                            "shacl.analysis_metadata_unverified",
                            str(error),
                            str(dependency.shape),
                            kind="unverified",
                        )
                    )
            for component in shape_components(local.states, dependencies, operation):
                components.append(component)
                if component.recursive:
                    issues.append(
                        Issue(
                            "shacl.analysis_recursion_unverified",
                            "Recursive shape validation has no assigned semantics "
                            "in this analysis",
                            str(component.shapes[0]),
                            actual=component.boundary,
                            kind="unverified",
                        )
                    )
            stages += 1
            for conflict in root_conflicts(
                roots, local.states, local.conflicts, dependencies, parameters
            ):
                conflicts.append(conflict)
                issues.append(
                    Issue(
                        "shacl.static_incompatibility",
                        "Supported necessary shape constraints cannot hold together "
                        "for an invocation",
                        str(conflict.root),
                    )
                )
            stages += 1
            complete = not any(
                issue.kind in ("input_error", "unverified", "exhausted")
                for issue in issues
            )
    except LimitExceeded as error:
        issues.exhausted(str(error), location="shacl.analysis")
    except InputError as error:
        try:
            issues.append(
                Issue(
                    "shacl.analysis_metadata_unverified", str(error), kind="unverified"
                )
            )
        except LimitExceeded as limit:
            issues.exhausted(str(limit), location="shacl.analysis")
    checked = local.completed + stages
    return ShapeAnalysis(
        tuple(local.conflicts),
        tuple(conflicts),
        tuple(dependencies),
        tuple(components),
        Report(
            "shacl.analysis",
            checked,
            tuple(issues),
            requested=max(checked, len(local.states) + 2, 1),
            complete=complete,
            regime="finite-static-analysis",
        ),
        "impossible" if conflicts else "not_determined",
        tuple(node for node, state in local.states.items() if not state.active),
    )
