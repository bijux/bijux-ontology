"""Paired selected SHACL execution and finite representative release impact."""

import threading
from collections.abc import Sequence
from typing import Literal

from rdflib import Graph, URIRef
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.patches.correspondence import compare_witnesses
from bijux_ontology.rdf.patches.planning import capture_source
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, term_key
from bijux_ontology.shacl.execution.results import ShaclResult
from bijux_ontology.shacl.profiles.execution import freeze_data as _data
from bijux_ontology.shacl.profiles.execution import portable_roles as _portable_roles
from bijux_ontology.shacl.profiles.execution import result_semantics as _semantics
from bijux_ontology.shacl.profiles.execution import validate_profile as _run
from bijux_ontology.shacl.profiles.extraction import extract_profile
from bijux_ontology.shacl.profiles.resources import _capture
from bijux_ontology.shacl.profiles.types import ShapeProfile

from .identity import ShapeKey, finding_groups, shape_key, shape_maps
from .types import (
    ShapeCorrespondence,
    ShapeFindingChange,
    ShapeSample,
    ShapeSampleAssessment,
    ShapeTargetChange,
    ShapeUpgradeAssessment,
)


def _unknown(issues: IssueCollector, message: str) -> None:
    """Keep unsupported attribution or required coverage separate from conformance."""
    issues.append(Issue("shacl.upgrade_unverified", message, kind="unverified"))


def _equal(left: PreparedSource, right: PreparedSource, budget: Operation) -> bool:
    """Delegate joint anonymous topology to the existing bounded RDF worker."""
    equal, _ = compare_witnesses(
        capture_source(left, "before:", budget).witness,
        capture_source(right, "after:", budget).witness,
        budget,
    )
    return equal


def _targets(
    result: ShaclResult,
    profile: ShapeProfile,
    mapping: dict[URIRef, ShapeKey],
    budget: Budget,
) -> dict[tuple[ShapeKey, tuple[object, ...]], Node]:
    """Bind only named root/focus evaluations from completed worker coverage."""
    if result.coverage is None or not result.coverage.report.verified:
        raise InputError("Completed target coverage is unavailable")
    budget.consume("states", len(profile.roots))
    roots = set(profile.roots)
    selected: dict[tuple[ShapeKey, tuple[object, ...]], Node] = {}
    for target in result.coverage.targets:
        budget.consume("work")
        for node in (target.shape, target.focus_node):
            bound_node(node, budget)
        if not isinstance(target.shape, URIRef) or not isinstance(
            target.focus_node, URIRef
        ):
            raise InputError("Target identity has no named cross-result binding")
        if target.shape not in roots:
            raise InputError("Target evaluation is outside the selected roots")
        budget.consume("states")
        selected[(shape_key(target.shape, mapping), term_key(target.focus_node))] = (
            target.focus_node
        )
    return selected


def _changes(
    before: ShaclResult,
    after: ShaclResult,
    graphs: tuple[Graph, Graph],
    maps: tuple[dict[URIRef, ShapeKey], dict[URIRef, ShapeKey]],
    *,
    shapes_equal: bool,
    lost_targets: bool,
    budget: Operation,
    issues: IssueCollector,
    output: list[ShapeFindingChange],
) -> None:
    """Compare contract groups while retaining values, severity and multiplicity."""
    left, left_unknown = finding_groups(before.findings, graphs[0], maps[0], budget)
    right, right_unknown = finding_groups(after.findings, graphs[1], maps[1], budget)
    raw_after = {}
    for finding, issue in zip(
        after.findings,
        (
            item
            for item in after.conformance_report.issues
            if item.code == "shacl.nonconformance"
        ),
        strict=False,
    ):
        budget.consume("work")
        budget.consume("states")
        raw_after[id(finding)] = issue
    budget.consume("states", len(left) + len(right))
    for key in left.keys() | right.keys():
        budget.consume("work")
        old, new = left.get(key, []), right.get(key, [])
        common = min(len(old), len(new))
        if common:
            budget.consume("states")
            output.append(
                ShapeFindingChange(
                    "persistent", key, tuple(old[:common]), tuple(new[:common])
                )
            )
        if len(old) > common:
            budget.consume("states")
            output.append(
                ShapeFindingChange(
                    "uncertain" if lost_targets else "resolved",
                    key,
                    tuple(old[common:]),
                    (),
                )
            )
        if len(new) > common:
            budget.consume("states")
            output.append(
                ShapeFindingChange("introduced", key, (), tuple(new[common:]))
            )
            for finding in new[common:]:
                # Transfer the already-qualified actual failure; a second issue
                # slot must not erase it when the remaining allowance is zero.
                actual_issue = raw_after.get(id(finding))
                if actual_issue is None:
                    raise InputError("Introduced finding lacks qualified raw evidence")
                issues.append(actual_issue)
    if left_unknown or right_unknown:
        same = False
        if shapes_equal and not lost_targets:
            lg, le = _semantics(before, budget)
            rg, re = _semantics(after, budget)
            if le and re:
                from bijux_ontology.rdf.loading import prepare_rdf

                same = _equal(
                    prepare_rdf(lg, budget=budget),
                    prepare_rdf(rg, budget=budget),
                    budget,
                )
        budget.consume("states")
        output.append(
            ShapeFindingChange(
                "persistent" if same else "uncertain",
                ("joint-anonymous-results",) if same else None,
                left_unknown,
                right_unknown,
            )
        )
        if not same:
            _unknown(
                issues, "Edited anonymous finding correspondence is not established"
            )


def _sample(
    sample: ShapeSample,
    profiles: tuple[ShapeProfile, ShapeProfile],
    graphs: tuple[Graph, Graph],
    maps: tuple[dict[URIRef, ShapeKey], dict[URIRef, ShapeKey]],
    shapes_equal: bool,
    budget: Operation,
    completed: tuple[set[ShapeKey], set[ShapeKey]],
) -> ShapeSampleAssessment:
    """Execute both actual profiles on one frozen graph with authored targets."""
    issues = IssueCollector(budget)
    before = after = None
    changes: list[ShapeFindingChange] = []
    targets: list[ShapeTargetChange] = []
    name = "sample"
    try:
        bound_text(sample.name, budget)
        name = sample.name
        data = _data(sample.data, budget)
        results: list[ShaclResult] = []
        for profile in profiles:
            if profile.source is None:
                raise InputError("Selected shape profile has no qualified source")
            if not _portable_roles(profile, data, budget):
                raise InputError("Anonymous identity shared across separate roles")
            result = _run(profile.source, profile, data, budget)
            results.append(result)
            if len(results) == 1:
                before = result
            else:
                after = result
        assert before is not None and after is not None
        if any(
            not result.report.complete
            or result.conforms is None
            or not result.findings_complete
            for result in results
        ):
            _unknown(
                issues,
                "Both maintained executions and result projections must complete",
            )
        else:
            previous = _targets(before, profiles[0], maps[0], budget)
            current = _targets(after, profiles[1], maps[1], budget)
            lost = False
            budget.consume("states", len(previous) + len(current))
            for key in previous.keys() | current.keys():
                budget.consume("work")
                budget.consume("states")
                status: Literal["persistent", "removed", "introduced"] = (
                    "persistent"
                    if key in previous and key in current
                    else "removed"
                    if key in previous
                    else "introduced"
                )
                focus = previous[key] if key in previous else current[key]
                targets.append(ShapeTargetChange(status, key[0], focus))
                if status == "removed":
                    lost = True
                    issues.append(
                        Issue(
                            "shacl.upgrade_target_removed",
                            "A prior selected root no longer targets this record",
                        )
                    )
            for side, inventory in enumerate((previous, current)):
                budget.consume("states", len(inventory))
                completed[side].update(key[0] for key in inventory)
            _changes(
                before,
                after,
                graphs,
                maps,
                shapes_equal=shapes_equal,
                lost_targets=lost,
                budget=budget,
                issues=issues,
                output=changes,
            )
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except (InputError, ResourceError):
        try:
            _unknown(
                issues,
                "Selected SHACL execution or finding correspondence is unavailable",
            )
        except LimitExceeded as error:
            issues.exhausted(str(error))
    report = Report(
        "shape-upgrade-sample",
        int(before is not None) + int(after is not None),
        tuple(issues),
        requested=2,
        regime="representative-shacl-release-impact",
    )
    return ShapeSampleAssessment(
        name, before, after, tuple(changes), tuple(targets), report
    )


def assess_shape_upgrade(
    before: ShapeProfile,
    after: ShapeProfile,
    samples: Sequence[ShapeSample],
    *,
    correspondence: Sequence[ShapeCorrespondence] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30.0,
    cancel: threading.Event | None = None,
) -> ShapeUpgradeAssessment:
    """Assess finite selected releases, retaining raw failures separately.

    Named findings use exact focus/path/component and named shape correspondence.
    Unchanged anonymous structures require joint shape/result isomorphism; edited
    anonymous attribution and anonymous data focus correspondence stay unknown.
    Completed target loss is a regression even when raw conformance improves.
    This does not prove universal refinement or data-release compatibility.
    """
    if not isinstance(before, ShapeProfile) or not isinstance(after, ShapeProfile):
        raise InputError("Shape upgrades require paired ShapeProfiles")
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children: list[Report] = []
    assessed: list[ShapeSampleAssessment] = []
    requested = 1
    try:
        owned_samples = _capture(samples, operation)
        requested = max(1, len(owned_samples))
        pairs = _capture(correspondence, operation)
        profiles: list[ShapeProfile] = []
        for profile in (before, after):
            retain_report(profile.structural_report, operation, issues)
            operation.consume("states")
            children.append(profile.structural_report)
            if profile.source is None or not profile.structural_report.verified:
                raise InputError("A required profile structure is incomplete")
            qualified = extract_profile(
                profile.source,
                profile.roots,
                ontology=profile.ontology,
                budget=operation,
                timeout=timeout,
                cancel=cancel,
            )
            retain_report(qualified.structural_report, operation, issues)
            operation.consume("states")
            children.append(qualified.structural_report)
            if qualified.source is None or not qualified.structural_report.verified:
                raise InputError("Actual selected profile structure is incomplete")
            profiles.append(qualified)
        first, second = profiles
        assert first.source is not None and second.source is not None
        graphs = (
            first.source._read(budget=operation),
            second.source._read(budget=operation),
        )
        maps = shape_maps(pairs, graphs, operation)
        shapes_equal = _equal(first.source, second.source, operation)
        completed: tuple[set[ShapeKey], set[ShapeKey]] = (set(), set())
        names: set[str] = set()
        for item in owned_samples:
            if not isinstance(item, ShapeSample):
                raise InputError("Expected typed ShapeSample records")
            bound_text(item.name, operation)
            if item.name in names:
                raise InputError("Shape sample names must be distinct")
            operation.consume("states")
            names.add(item.name)
            operation.consume("states", 2)
            operation.consume("bytes", 128)
            result = _sample(
                item, (first, second), graphs, maps, shapes_equal, operation, completed
            )
            assessed.append(result)
            retain_report(result.report, operation, issues)
            children.append(result.report)
            if result.report.outcome == "exhausted":
                break
        for side, profile in enumerate(profiles):
            for root in profile.roots:
                bound_node(root, operation)
                if (
                    not isinstance(root, URIRef)
                    or shape_key(root, maps[side]) not in completed[side]
                ):
                    _unknown(
                        issues,
                        "A selected root has no completed representative targets",
                    )
        if not owned_samples:
            _unknown(issues, "No actual representative data samples were supplied")
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except (InputError, ResourceError):
        try:
            _unknown(
                issues,
                "Shape profile, sample or correspondence qualification is incomplete",
            )
        except LimitExceeded as error:
            issues.exhausted(str(error))
    return ShapeUpgradeAssessment(
        tuple(assessed),
        Report(
            "shape-upgrade",
            len(assessed),
            tuple(issues),
            requested=requested,
            children=tuple(children),
            regime="representative-shacl-release-impact",
        ),
    )
