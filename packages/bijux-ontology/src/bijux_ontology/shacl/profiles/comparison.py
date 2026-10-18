"""Actual selected profile behavior, kept separate from raw SHACL conformance."""

import time
from dataclasses import replace

from rdflib import BNode, Graph, URIRef
from rdflib.namespace import SH

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, IssueCollector, retain_report
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.patches.correspondence import Operation, compare_witnesses
from bijux_ontology.rdf.patches.planning import capture_source
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, bound_text
from bijux_ontology.reports import Issue, Report
from bijux_ontology.shacl.checking import ShaclResult
from bijux_ontology.shacl.coverage import ShaclCoverageContract
from bijux_ontology.shacl.session import ShaclSession

from .resources import _owned
from .types import ProfileCase, ProfileComparison, ShapeProfile, ShapeTerm, Triple


def _remaining(operation: Operation) -> float:
    """Check the global deadline before creating each owned backend request."""
    operation.consume("work")
    remaining = operation.deadline - time.monotonic()
    if remaining <= 0:
        raise LimitExceeded("Shape profile exceeded its deadline")
    return remaining


def _data(source: PreparedSource, budget: Budget) -> PreparedSource:
    """Freeze exactly one explicit graph before both sides consume a case."""
    if source.union or source.literal_fidelity not in (
        "source_terms",
        "supplied_terms",
    ):
        raise InputError("Profile cases require an exact selected physical graph")
    graph = source._read(budget=budget)
    triples: list[Triple] = []
    for triple in graph:
        budget.consume("work")
        budget.consume("states")
        for node in triple:
            bound_node(node, budget)
        triples.append(triple)
    return _owned(source, graph, triples, budget)


def _portable_roles(
    profile: ShapeProfile, data: PreparedSource, budget: Budget
) -> bool:
    """Reject exact anonymous identity shared across separately serialized roles.

    This tests owned term objects, never coincident blank labels. Anonymous shape
    structure within one role remains portable through ordinary RDF isomorphism.
    """
    seen: set[int] = set()
    portable = True
    for source in (profile.source, profile.ontology, data):
        if source is None:
            continue
        current: set[int] = set()
        for triple in source._read(budget=budget):
            budget.consume("work")
            for node in triple:
                bound_node(node, budget)
                if isinstance(node, BNode):
                    budget.consume("states")
                    identity = id(node)
                    if identity in seen:
                        portable = False
                    current.add(identity)
        budget.consume("states", len(current))
        seen.update(current)
    return portable


def _run(
    shapes: PreparedSource,
    profile: ShapeProfile,
    data: PreparedSource,
    budget: Operation,
) -> ShaclResult:
    """Use the existing cancellable isolated worker with this request's deadline."""
    budget.consume("resources")
    with ShaclSession(
        shapes,
        ontology=profile.ontology,
        use_shapes=profile.roots,
        preflight=True,
        coverage=ShaclCoverageContract(),
        limits=budget.limits,
        timeout=_remaining(budget),
    ) as session:
        result = session._validate(
            data,
            focus_nodes=(),
            graph_name=None,
            union=False,
            source_usages=(),
            scope="dataset",
            budget=budget,
            cancel=budget.cancel,
        )
    retained = IssueCollector(budget)
    try:
        retain_report(result.report, budget, retained)
        budget.consume("states", len(result.findings))
    except LimitExceeded as error:
        retained.exhausted(str(error))
        result.report = Report(
            "shacl",
            result.report.checked,
            tuple(retained),
            requested=result.report.requested,
            complete=False,
            regime=result.report.regime,
        )
        result.conformance_report = result.report
        keep = sum(issue.code == "shacl.nonconformance" for issue in retained)
        result.findings = result.findings[:keep]
        result.root_findings = ()
        result.findings_complete = False
        result.coverage = None
        result.constraints = None
    return result


def _semantics(result: ShaclResult, budget: Budget) -> tuple[Graph, bool]:
    """Compare standard RDF result semantics, excluding generated message prose.

    Blank source shapes/paths carry maintained cloned descriptions and compare
    jointly through RDF isomorphism. Anonymous data endpoints lack an established
    cross-result association here and therefore remain explicitly unknown.
    """
    graph = result.report_graph()
    established = True
    for finding in result.findings:
        budget.consume("work")
        if isinstance(finding.focus_node, BNode) or isinstance(finding.value, BNode):
            established = False
        for node in (finding.source_shape, finding.path):
            if isinstance(node, BNode) and (node, None, None) not in graph:
                established = False
    for triple in graph:
        budget.consume("work")
        budget.consume("triples")
        for node in triple:
            bound_node(node, budget)
    graph.remove((None, SH.resultMessage, None))
    return graph, established


def _coverage(
    result: ShaclResult, roots: tuple[ShapeTerm, ...], budget: Budget
) -> set[ShapeTerm]:
    """Qualify complete root inventory without joining anonymous labels."""
    if result.coverage is None or not result.coverage.report.complete:
        return set()
    actual = set()
    for target in result.coverage.targets:
        budget.consume("work")
        budget.consume("states")
        actual.add(target.shape)
    covered: set[ShapeTerm] = {
        root for root in roots if isinstance(root, URIRef) and root in actual
    }
    if len(actual) == len(roots) and all(
        root in actual for root in roots if isinstance(root, URIRef)
    ):
        covered.update(roots)
    return covered


def compare_profile(
    authored: PreparedSource,
    profile: ShapeProfile,
    cases: tuple[ProfileCase, ...],
    *,
    budget: Operation,
) -> ShapeProfile:
    """Qualify actual supplied cases without asserting universal preservation."""
    issues = IssueCollector(budget)
    comparisons: list[ProfileComparison] = []
    covered: list[set[ShapeTerm]] = [set(), set()]
    try:
        if not profile.structural_report.verified or profile.source is None:
            issues.append(
                Issue(
                    "shacl.profile_behavior_unverified",
                    "Behavior requires a complete supported "
                    "authored dependency closure",
                    kind="unverified",
                )
            )
        else:
            for case in cases:
                budget.consume("work")
                bound_text(case.name, budget)
                before = after = None
                local = IssueCollector(budget)
                comparison: Report | None = None
                try:
                    data = _data(case.data, budget)
                    before = _run(authored, profile, data, budget)
                    after = _run(profile.source, profile, data, budget)
                    if not _portable_roles(profile, data, budget):
                        local.append(
                            Issue(
                                "shacl.profile_behavior_unverified",
                                "Separate portable roles cannot retain shared "
                                "anonymous term identity",
                                kind="unverified",
                            )
                        )
                    for side, result in enumerate((before, after)):
                        if (
                            result.conforms is not None
                            and result.conforms != case.expected_conforms
                        ):
                            local.append(
                                Issue(
                                    "shacl.profile_behavior_changed",
                                    "Actual selected conformance differs "
                                    "from the declared case",
                                    expected=str(case.expected_conforms),
                                    actual=str(result.conforms),
                                )
                            )
                        covered[side].update(_coverage(result, profile.roots, budget))
                    if (
                        not before.report.complete
                        or not after.report.complete
                        or before.conforms is None
                        or after.conforms is None
                        or not before.findings_complete
                        or not after.findings_complete
                    ):
                        local.append(
                            Issue(
                                "shacl.profile_behavior_unverified",
                                "Both maintained validations must complete "
                                "with actual conformance",
                                kind="unverified",
                            )
                        )
                    else:
                        left, left_established = _semantics(before, budget)
                        right, right_established = _semantics(after, budget)
                        if not left_established or not right_established:
                            local.append(
                                Issue(
                                    "shacl.profile_behavior_unverified",
                                    "Anonymous data/result correspondence "
                                    "is not established",
                                    kind="unverified",
                                )
                            )
                        else:
                            left_source = prepare_rdf(left, budget=budget)
                            right_source = prepare_rdf(right, budget=budget)
                            equal, _ = compare_witnesses(
                                capture_source(left_source, "before:", budget).witness,
                                capture_source(right_source, "after:", budget).witness,
                                budget,
                            )
                            comparison = Report(
                                "shacl-profile-results",
                                1,
                                ()
                                if equal
                                else (
                                    Issue(
                                        "shacl.profile_behavior_changed",
                                        "Selected standard RDF result semantics differ",
                                    ),
                                ),
                                regime="standard-shacl-result-isomorphism",
                            )
                            local.extend(comparison.issues)
                except LimitExceeded as error:
                    local.exhausted(str(error))
                except (InputError, ResourceError) as error:
                    try:
                        local.append(
                            Issue(
                                "shacl.profile_behavior_unverified",
                                str(error),
                                kind="unverified",
                            )
                        )
                    except LimitExceeded as exhausted:
                        local.exhausted(str(exhausted))
                report = Report(
                    "shacl-profile-case",
                    1,
                    tuple(local),
                    children=(comparison,) if comparison is not None else (),
                    regime="selected-shacl-result-semantics",
                )
                budget.consume("states")
                comparisons.append(ProfileComparison(case.name, before, after, report))
                issues.extend(report.issues)
                if report.outcome == "exhausted":
                    break
            for coverage_set in covered:
                if set(profile.roots) - coverage_set:
                    issues.append(
                        Issue(
                            "shacl.profile_behavior_unverified",
                            "Requested roots lack completed selected target coverage",
                            kind="unverified",
                        )
                    )
    except LimitExceeded as error:
        issues.exhausted(str(error))
    report = Report(
        "shacl-profile",
        len(comparisons),
        tuple(issues),
        requested=max(1, len(cases)),
        children=(profile.structural_report, *(item.report for item in comparisons)),
        regime="finite-profile-supplied-case-agreement",
    )
    return replace(profile, comparisons=tuple(comparisons), report=report)
