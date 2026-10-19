"""Actual finite validation comparisons over unchanged explicit node-shape pairs."""

import time
from pathlib import Path
from threading import Event
from typing import cast

from rdflib import URIRef

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
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.shacl.checking import ShaclResult
from bijux_ontology.shacl.coverage import ShaclCoverageContract
from bijux_ontology.shacl.profiles.execution import freeze_data
from bijux_ontology.shacl.session import ShaclSession
from bijux_ontology.shex.checking import validate_shex
from bijux_ontology.shex.types import ShexResult

from .compilation import _map
from .types import InterchangeCheck, InterchangeObligation, ShapeTranslation


def _pair_completed(
    result: ShaclResult, shape: URIRef, focus: URIRef, operation: Operation
) -> bool:
    """Require actual complete evaluation of exactly this unchanged root pair."""
    if (
        not result.report.complete
        or not result.findings_complete
        or type(result.conforms) is not bool
        or result.coverage is None
        or not result.coverage.report.complete
        or len(result.coverage.targets) != 1
    ):
        return False
    target = result.coverage.targets[0]
    operation.consume("work")
    for node in (target.shape, target.focus_node):
        bound_node(node, operation)
    return target.shape == shape and target.focus_node == focus


def check_interchange(
    translation: ShapeTranslation,
    data: PreparedSource,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
    artifacts: Path | None = None,
) -> InterchangeCheck:
    """Compare both maintained validators on each fixed compiled pair and graph.

    Agreement includes both validators rejecting a record. Their actual negative
    findings remain in the raw fields; agreement never certifies the data as
    valid. Missing engines, incomplete coverage and unsupported scalars remain
    unknown. Each pair is compared independently, so mixed pass/fail records
    cannot hide behind an aggregate false conformance answer.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    obligations: list[InterchangeObligation] = []
    shex: ShexResult | None = None
    metadata = None
    checked = 0
    children: list[Report] = []
    requested = 1
    try:
        if (
            type(translation) is not ShapeTranslation
            or type(data) is not PreparedSource
        ):
            raise InputError(
                "Interchange checking requires a compiled translation and prepared data"
            )
        retain_report(translation.report, operation, issues)
        children.append(translation.report)
        if (
            not translation.report.verified
            or translation.shapes is None
            or translation.schema is None
            or translation.preservation is None
        ):
            raise ResourceError(
                "An incomplete compilation cannot establish language agreement"
            )
        bindings, root = _map(translation.bindings, operation)
        requested = len(bindings)
        operation.consume("states", 8 + 4 * requested)
        operation.consume("bytes", 1024 + 256 * requested)
        if (
            translation.shape_metadata is None
            or not translation.shape_metadata.same_scope(
                SnapshotMetadata.capture(translation.shapes, operation)
            )
        ):
            raise ResourceError("Compiled shape scope changed after qualification")
        metadata = SnapshotMetadata.capture(data, operation)
        frozen = freeze_data(data, operation)
        imports = {}
        for resource in translation.schema_imports:
            operation.consume("states")
            bound_text(resource.document_iri, operation)
            bound_text(resource.content, operation)
            imports[resource.document_iri] = resource
        bound_text(translation.schema.content, operation)
        shex = validate_shex(
            frozen,
            translation.schema,
            bindings=bindings,
            imports=imports,
            budget=operation,
            timeout=max(0.000001, operation.deadline - time.monotonic()),
            cancel=cancel,
            artifacts=artifacts,
        )
        # Raw nonconformance belongs to the data result. Required execution
        # completion still participates in comparison acceptance independently.
        completion = IssueCollector(operation)
        for issue in shex.report.issues:
            if issue.kind != "finding":
                completion.append(issue)
        native_coverage = Report(
            "shape-interchange.shex-coverage",
            int(shex.report.complete),
            tuple(completion),
            requested=1,
            complete=shex.report.complete,
        )
        retain_report(native_coverage, operation, issues)
        children.append(native_coverage)
        if len(shex.obligations) != len(bindings):
            raise ResourceError("Native ShEx omitted the unchanged shape map")
        with ShaclSession(
            translation.shapes,
            use_shapes=(root,),
            mode="pairs",
            preflight=True,
            coverage=ShaclCoverageContract(),
            budget=operation,
            timeout=max(0.000001, operation.deadline - time.monotonic()),
        ) as session:
            for binding, native in zip(bindings, shex.obligations, strict=True):
                operation.consume("states", 8)
                operation.consume("bytes", 512)
                local = IssueCollector(operation)
                actual = None
                equal = None
                complete = False
                try:
                    if native.binding != binding:
                        raise ResourceError(
                            "Native ShEx changed a required binding identity"
                        )
                    actual = session.validate(
                        frozen,
                        focus_nodes=(cast(URIRef, binding.focus),),
                        cancel=cancel,
                    )
                    if (
                        not _pair_completed(
                            actual, root, cast(URIRef, binding.focus), operation
                        )
                        or native.status == "unknown"
                        or native.native_json is None
                    ):
                        raise ResourceError(
                            "Both exact pair evaluations must actually complete"
                        )
                    equal = actual.conforms == (native.status == "matched")
                    if not equal:
                        local.append(
                            Issue(
                                "shape-interchange.behavior_changed",
                                "Maintained validators disagree for this exact pair",
                                str(binding.focus),
                                expected=str(actual.conforms),
                                actual=native.status,
                            )
                        )
                    complete = True
                    checked += 1
                except LimitExceeded as error:
                    local.exhausted(str(error), location=str(binding.focus))
                except InputError as error:
                    try:
                        local.append(
                            Issue(
                                "shape-interchange.unverified",
                                str(error),
                                str(binding.focus),
                                kind="unverified",
                            )
                        )
                    except LimitExceeded as exhausted:
                        local.exhausted(str(exhausted), location=str(binding.focus))
                report = Report(
                    "shape-interchange.pair",
                    int(complete),
                    tuple(local),
                    requested=1,
                    complete=complete,
                    regime="finite-native-conformance-comparison",
                )
                # Reference capacity was reserved before either pair outcome.
                obligations.append(
                    InterchangeObligation(binding, actual, native.status, equal, report)
                )
                children.append(report)
                try:
                    retain_report(report, operation, issues)
                except LimitExceeded as error:
                    issues.retain_completed(report)
                    issues.exhausted(str(error))
                    break
                if any(issue.kind == "exhausted" for issue in report.issues):
                    break
        if not metadata.same_scope(SnapshotMetadata.capture(data, operation)):
            raise ResourceError("Selected RDF data scope changed during comparison")
    except LimitExceeded as error:
        for child in children:
            issues.retain_completed(child)
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(
                Issue(
                    "shape-interchange.unverified"
                    if isinstance(error, ResourceError)
                    else "input.invalid",
                    str(error),
                    kind="unverified"
                    if isinstance(error, ResourceError)
                    else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return InterchangeCheck(
        translation,
        metadata,
        shex,
        tuple(obligations),
        Report(
            "shape-interchange.compare",
            checked,
            tuple(issues),
            requested=requested,
            complete=checked == requested,
            children=tuple(children),
            regime="finite-native-conformance-comparison",
        ),
    )
