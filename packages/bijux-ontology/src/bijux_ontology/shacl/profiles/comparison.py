"""Actual selected profile behavior, kept separate from raw SHACL conformance."""

from dataclasses import replace

from rdflib import URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import Budget, IssueCollector, bound_text
from bijux_ontology.rdf.patches.correspondence import compare_witnesses
from bijux_ontology.rdf.patches.planning import capture_source
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.shacl.checking import ShaclResult

from .execution import freeze_data, portable_roles, result_semantics, validate_profile
from .types import ProfileCase, ProfileComparison, ShapeProfile, ShapeTerm


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
                    data = freeze_data(case.data, budget)
                    before = validate_profile(authored, profile, data, budget)
                    after = validate_profile(profile.source, profile, data, budget)
                    if not portable_roles(profile, data, budget):
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
                        left, left_established = result_semantics(before, budget)
                        right, right_established = result_semantics(after, budget)
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
