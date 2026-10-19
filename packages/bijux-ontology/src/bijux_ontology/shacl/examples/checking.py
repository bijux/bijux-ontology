"""Verify every constructed example against the unchanged original root/focus."""

import threading
import time
from contextlib import ExitStack
from typing import Literal as Status

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import SH

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.shacl.coverage import ShaclCoverageContract
from bijux_ontology.shacl.execution.projection import _retained_result
from bijux_ontology.shacl.execution.results import ShaclResult
from bijux_ontology.shacl.session import ShaclSession

from .construction import Candidates, State
from .profile import PreparedExamples, UnsupportedExample, prepare, reserve
from .types import ExampleGeneration, MissingValueExample, ShapeExample


class _SearchCap(ResourceError):
    """The caller's finite native-trial window ended, not a satisfiability proof."""


class _Cancellation(threading.Event):
    """Combine original deadline/cancellation without mutating the caller's event."""

    def __init__(self, operation: Operation) -> None:
        """Retain the original deadline and cancellation owner without resetting it."""
        super().__init__()
        self.operation = operation

    def is_set(self) -> bool:
        """Let the existing native-session supervisor enforce the original deadline."""
        return (
            time.monotonic() >= self.operation.deadline
            or self.operation.cancel is not None
            and self.operation.cancel.is_set()
        )


def _candidate(
    prepared: PreparedExamples, state: State, budget: Budget
) -> PreparedSource:
    """Copy all explicit selected seed facts and bounded constructed RDF edges."""
    reserve(budget, len(prepared.seed) + len(state))
    graph = Graph(bind_namespaces="none")
    for triple in (*prepared.seed, *state):
        budget.consume("work")
        budget.consume("triples")
        for node in triple:
            bound_node(node, budget)
        graph.add(triple)
    return prepare_rdf(graph, budget=budget)


def _scope(
    shapes: PreparedSource,
    seed: PreparedSource,
    prepared: PreparedExamples,
    budget: Budget,
) -> None:
    """Reject public origin/fidelity/scope drift rather than rebind cached semantics."""
    if not prepared.shapes_metadata.same_scope(
        SnapshotMetadata.capture(shapes, budget)
    ) or not prepared.seed_metadata.same_scope(SnapshotMetadata.capture(seed, budget)):
        raise UnsupportedExample("Original example source scope metadata changed")


def _qualified(result: ShaclResult, root: URIRef | BNode, focus: URIRef) -> bool:
    """Require actual complete explicit-pair evaluation, including target coverage."""
    expected = str(root) if isinstance(root, URIRef) else root
    selected = result.shapes[0] if len(result.shapes) == 1 else None
    exact = selected is expected if isinstance(root, BNode) else selected == expected
    return (
        exact
        and result.focus_nodes == (str(focus),)
        and result.mode == "pairs"
        and type(result.conforms) is bool
        and result.report.complete
        and result.report.outcome in ("agreement", "disagreement")
        and result.findings_complete
        and result.coverage is not None
        and result.coverage.report.verified
        and any(item.focus_node == focus for item in result.coverage.targets)
    )


def generate_examples(
    shapes: PreparedSource,
    root: URIRef | BNode,
    focus: URIRef,
    *,
    seed: PreparedSource,
    values: tuple[URIRef | Literal, ...],
    negative: MissingValueExample | None = None,
    max_trials: int = 128,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30.0,
    cancel: threading.Event | None = None,
) -> ExampleGeneration:
    """Search explicit caller values for actual supported acyclic SHACL examples.

    The seed must contain the IRI focus and remains intact. No value, category,
    class assertion, target edge or presence marker is invented. Direct paths,
    counts, fixed/enumerated terms, node kinds, datatype/class checks, bounded
    value/length facets, nested references and AND/OR branches are supported.
    Every trial uses the full original shapes with the same explicit root/focus
    pair, so removing an authored target cannot create a vacuous witness.

    A missing-value request removes generated direct-path values only. Its
    original root must fail the named MinCount at the actual focus, and every
    named preserved sibling is independently checked. Raw negative results stay
    negative. Finite trial/pool failure never proves general unsatisfiability.
    """
    if type(max_trials) is not int or max_trials <= 0:
        raise InputError("Example search requires a positive finite max_trials")
    current = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(current)
    trials: list[ShaclResult] = []
    prepared = None
    positive = None
    counterexample = None
    status: Status[
        "found", "search_exhausted", "unsupported", "input_error", "exhausted"
    ] = "search_exhausted"
    selected_reports: list[Report] = []
    preparation_reports: list[Report] = []
    try:
        prepared = prepare(
            shapes,
            root,
            focus,
            seed,
            values,
            negative,
            current,
            issues,
            preparation_reports,
        )
        selected_reports.extend(
            (prepared.inspection.profile.structural_report, prepared.analysis.report)
        )
        guard = _Cancellation(current)
        with ExitStack() as stack:
            sessions: dict[URIRef | BNode, ShaclSession] = {}

            def session(selected: URIRef | BNode) -> ShaclSession:
                """Reuse native preparation for each unchanged shape binding."""
                if selected not in sessions:
                    reserve(current)
                    coverage = ShaclCoverageContract(
                        minimum_focus=1,
                        focus_nodes=(str(focus),),
                        pairs=((str(selected), str(focus)),)
                        if isinstance(selected, URIRef)
                        else (),
                    )
                    sessions[selected] = stack.enter_context(
                        ShaclSession(
                            shapes,
                            use_shapes=(selected,),
                            mode="pairs",
                            preflight=True,
                            coverage=coverage,
                            limits=current.limits,
                            budget=current,
                            timeout=max(0.000001, current.deadline - time.monotonic()),
                        )
                    )
                return sessions[selected]

            def run(data: PreparedSource, selected: URIRef | BNode) -> ShaclResult:
                """Bound raw evidence before retaining a trial or claiming a witness."""
                if len(trials) >= max_trials:
                    raise _SearchCap("The explicit native-trial search window ended")
                _scope(shapes, seed, prepared, current)
                reserve(current)
                current.consume("resources")
                actual = session(selected).validate(
                    data, focus_nodes=(focus,), cancel=guard
                )
                actual = _retained_result(actual, current)
                trials.append(actual)
                if not _qualified(actual, selected, focus):
                    if actual.report.outcome == "exhausted":
                        raise LimitExceeded(
                            "Native example validation exceeded its cumulative limits"
                        )
                    raise UnsupportedExample(
                        "Native validation did not qualify the original shape/focus"
                    )
                _scope(shapes, seed, prepared, current)
                return actual

            builder = Candidates(prepared, current)
            for state in builder.expand(root, focus):
                current.consume("work")
                data = _candidate(prepared, state, current)
                result = run(data, root)
                if result.conforms is not True or not result.report.verified:
                    continue
                positive = ShapeExample(data, result)
                if negative is None:
                    status = "found"
                    break
                assert prepared.negative_path is not None
                reserve(current, len(state))
                reduced = tuple(
                    triple
                    for triple in state
                    if not (triple[0] == focus and triple[1] == prepared.negative_path)
                )
                if reduced == state:
                    continue
                candidate = _candidate(prepared, reduced, current)
                failed = run(candidate, root)
                if failed.conforms is not False or not any(
                    item.focus_node == focus
                    and item.source_shape == negative.shape
                    and item.constraint_component == SH.MinCountConstraintComponent
                    for item in failed.findings
                ):
                    continue
                kept = []
                for selected in negative.preserve:
                    checked = run(candidate, selected)
                    reserve(current)
                    kept.append(checked)
                    if checked.conforms is not True or not checked.report.verified:
                        break
                if len(kept) != len(negative.preserve) or any(
                    item.conforms is not True or not item.report.verified
                    for item in kept
                ):
                    continue
                counterexample = ShapeExample(candidate, failed, tuple(kept))
                status = "found"
                break
        if status != "found":
            issues.append(
                Issue(
                    "shacl.example_search_exhausted",
                    "No witness was found within the explicit pool/search",
                    kind="unverified",
                )
            )
    except _SearchCap as error:
        try:
            issues.append(
                Issue("shacl.example_search_exhausted", str(error), kind="unverified")
            )
        except LimitExceeded as exhausted:
            status = "exhausted"
            issues.exhausted(str(exhausted))
    except (LimitExceeded, RecursionError) as error:
        status = "exhausted"
        issues.exhausted(str(error))
    except InputError as error:
        status = "unsupported"
        try:
            issues.append(
                Issue(
                    "shacl.example_unsupported"
                    if isinstance(error, ResourceError)
                    else "shacl.example_input_invalid",
                    str(error),
                    kind="unverified"
                    if isinstance(error, ResourceError)
                    else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            status = "exhausted"
            issues.exhausted(str(exhausted))
    if status != "found":
        for receipt in preparation_reports:
            issues.retain_completed(receipt)
        for trial in trials:
            issues.retain_completed(trial.report)
    if positive is not None:
        selected_reports.append(positive.validation.report)
    if counterexample is not None:
        selected_reports.append(counterexample.validation.report)
        selected_reports.extend(item.report for item in counterexample.preserved)
    report = Report(
        "shacl-example-construction",
        int(positive is not None) + int(counterexample is not None),
        tuple(issues),
        requested=1 + int(negative is not None),
        complete=status == "found",
        children=tuple(selected_reports),
        regime="finite-pool-original-pair-validation",
    )
    if report.outcome == "input_error":
        status = "input_error"
    elif report.outcome == "exhausted":
        status = "exhausted"
    return ExampleGeneration(
        status,
        shapes,
        root,
        focus,
        seed,
        positive,
        counterexample,
        tuple(trials),
        None if prepared is None else prepared.inspection,
        None if prepared is None else prepared.analysis,
        report,
        None if prepared is None else prepared.shapes_metadata,
        None if prepared is None else prepared.seed_metadata,
    )
