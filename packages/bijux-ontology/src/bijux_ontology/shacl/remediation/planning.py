"""Generate separate checked patches without guessing application content."""

import time
from collections.abc import Sequence

from rdflib import URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.migrations.candidates import MigrationCheck, finish_migration
from bijux_ontology.rdf.patches.applying import _materialize, apply_rdf_patch
from bijux_ontology.rdf.patches.planning import capture_source, plan_rdf_patch
from bijux_ontology.rdf.patches.types import PatchUnavailable
from bijux_ontology.rdf.sources.metadata import SnapshotMetadata
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.shacl.checking import validate_shapes
from bijux_ontology.shacl.evidence.findings import ShaclFinding
from bijux_ontology.shacl.execution.capabilities import ShaclConstraintRequirement
from bijux_ontology.shacl.targets.coverage import ShaclCoverageContract

from .types import RemediationAlternative, RemediationResult, RemediationStrategy


def propose_remediations(
    data: PreparedSource,
    shapes: PreparedSource,
    strategies: Sequence[RemediationStrategy],
    *,
    coverage: ShaclCoverageContract | None = None,
    required_constraints: Sequence[ShaclConstraintRequirement] = (),
    checks: Sequence[MigrationCheck] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30.0,
) -> RemediationResult:
    """Recheck all authored shapes and original root obligations for every proposal.

    No selected-constraint shortcut is used. Removing an original target cannot
    evade its required check. The initial profile requires named root obligations
    and named finding shape/focus identities. Anonymous edit correspondence uses
    the existing exact patch planner and remains unknown when not established.
    Callbacks are ordinary trusted application code: unexpected errors propagate and
    their CPU cannot be forcibly interrupted. Native checks share one deadline.
    A valid alternative still requires explicit application/export acceptance.
    """
    operation = Operation(limits, budget, timeout, None)
    issues = IssueCollector(operation)
    alternatives: list[RemediationAlternative] = []
    unresolved: list[ShaclFinding] = []
    baseline = None
    completed = 0
    requested = 1
    finished = False

    def unknown(message: str) -> None:
        """Keep missing strategy or scope qualification distinct from repair success."""
        issues.append(Issue("shacl.remediation_unverified", message, kind="unverified"))

    try:
        SnapshotMetadata.capture(data, operation)
        SnapshotMetadata.capture(shapes, operation)
        source = capture_source(data, "source:", operation)
        shape_capture = capture_source(shapes, "shapes:", operation)
        owned_shapes = _materialize(shape_capture.witness, shapes, operation)
        supplied = capture_items(strategies, RemediationStrategy, operation)
        extra = capture_items(checks, MigrationCheck, operation)
        required = capture_items(
            required_constraints, ShaclConstraintRequirement, operation
        )
        names: set[str] = set()
        for strategy in supplied:
            bound_text(strategy.name, operation)
            if (
                not strategy.name
                or strategy.name in names
                or not callable(strategy.propose)
            ):
                raise InputError(
                    "Strategies require distinct names and callable proposals"
                )
            operation.consume("states")
            names.add(strategy.name)
            for node in (strategy.source_shape, strategy.constraint_component):
                if not isinstance(node, URIRef):
                    raise InputError(
                        "Strategies require exact named constraint identities"
                    )
                bound_node(node, operation)
        initial_coverage = coverage or ShaclCoverageContract(minimum_focus=1)
        baseline = validate_shapes(
            _materialize(source.witness, data, operation),
            owned_shapes,
            coverage=initial_coverage,
            required_constraints=required,
            preflight=True,
            budget=operation,
            timeout=operation.deadline - time.monotonic(),
        )
        if (
            baseline.conforms is None
            or not baseline.findings_complete
            or baseline.coverage is None
            or not baseline.coverage.report.verified
        ):
            retain_report(baseline.report, operation, issues)
            unknown("Complete native baseline and target evidence are required")
        else:
            pairs: list[tuple[str, str]] = []
            for target in baseline.coverage.targets:
                operation.consume("work")
                operation.consume("states")
                if not isinstance(target.shape, URIRef) or not isinstance(
                    target.focus_node, URIRef
                ):
                    raise ResourceError(
                        "Remediation requires named original root obligations"
                    )
                pairs.append((str(target.shape), str(target.focus_node)))
            post_coverage = ShaclCoverageContract(
                minimum_focus=max(1, initial_coverage.minimum_focus),
                focus_nodes=initial_coverage.focus_nodes,
                pairs=(*initial_coverage.pairs, *pairs),
            )
            findings = baseline.findings
            requested = max(1, len(supplied))

            def postcondition(candidate: PreparedSource) -> Report:
                """Check every shape against candidate data and original targets."""
                return validate_shapes(
                    candidate,
                    owned_shapes,
                    coverage=post_coverage,
                    required_constraints=required,
                    preflight=True,
                    budget=operation,
                    timeout=operation.deadline - time.monotonic(),
                ).report

            resolved = False
            for strategy in supplied:
                matched: list[ShaclFinding] = []
                for finding in findings:
                    operation.consume("work")
                    if (
                        finding.source_shape == strategy.source_shape
                        and finding.constraint_component
                        == strategy.constraint_component
                    ):
                        if not isinstance(finding.focus_node, URIRef):
                            unknown(
                                "Anonymous finding focus lacks supported "
                                "repair correspondence"
                            )
                            continue
                        operation.consume("states")
                        matched.append(finding)
                if not matched:
                    completed += 1
                    continue
                proposed = strategy.propose(
                    _materialize(source.witness, data, operation), tuple(matched)
                )
                operation.consume("work", 0)
                if proposed is None:
                    completed += 1
                    continue
                SnapshotMetadata.capture(proposed, operation)
                patch = plan_rdf_patch(
                    data,
                    proposed,
                    budget=operation,
                    timeout=operation.deadline - time.monotonic(),
                )
                retain_report(patch.report, operation, issues)
                if patch.patch is not None and patch.report.verified:
                    transformed = apply_rdf_patch(
                        data,
                        patch.patch,
                        budget=operation,
                        timeout=operation.deadline - time.monotonic(),
                    )
                    operation.consume("states")
                    operation.consume("bytes", 64)
                    candidate = finish_migration(
                        data,
                        transformed,
                        patch.patch,
                        before_checks=(),
                        after_checks=(
                            MigrationCheck("required-shacl", postcondition),
                            *extra,
                        ),
                        budget=operation,
                    )
                    alternatives.append(
                        RemediationAlternative(strategy.name, candidate)
                    )
                    if candidate.report.verified:
                        resolved = True
                completed += 1
            for finding in findings:
                operation.consume("work")
                if not resolved:
                    operation.consume("states")
                    operation.consume("bytes", 8)
                    unresolved.append(finding)
            if unresolved or not supplied:
                unknown("Required findings have no completely checked alternative")
            finished = True
    except LimitExceeded as error:
        issues.exhausted(str(error), location="shacl.remediation")
    except (ResourceError, PatchUnavailable) as error:
        try:
            unknown(str(error))
        except LimitExceeded:
            issues.exhausted("Stopped retaining unsupported remediation scope")
    except InputError as error:
        try:
            issues.append(Issue("input.invalid", str(error), kind="input_error"))
        except LimitExceeded:
            issues.exhausted("Stopped retaining remediation input failure")
    return RemediationResult(
        baseline,
        tuple(alternatives),
        tuple(unresolved),
        Report(
            "shacl.remediation",
            completed,
            tuple(issues),
            requested=requested,
            complete=finished,
            children=tuple(a.candidate.report for a in alternatives),
            regime="application-owned-alternatives-complete-required-recheck",
        ),
    )
