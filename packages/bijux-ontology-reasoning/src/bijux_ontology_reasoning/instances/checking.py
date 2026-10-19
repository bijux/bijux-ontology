"""Joint direct-semantics consistency with separate original record correlation."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from threading import Event

from rdflib import BNode

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.rdf.patches.correspondence import Operation
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import Payload, RdfSource
from bijux_ontology.reports import Issue, Location, Report

from ..backend.rdf_sources import capture_rdf_closure
from ..backend.types import OwlProfileBackend
from ..diagnosis import check_consistency
from .preparation import capture_bindings, joint_source, participation, selected_scope
from .types import EmittedConsistency, EmittedRecord


def check_emitted_consistency(
    backend: OwlProfileBackend,
    ontology: PreparedSource,
    emitted: PreparedSource,
    bindings: Sequence[RdfModelUsage],
    *,
    imports: Mapping[str, RdfSource | Payload] | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> EmittedConsistency:
    """Diagnose all selected emitted RDF with one selected ontology closure.

    Both inputs require an explicit physical PreparedSource scope. Native OWL
    document conversion is outside this profile. Original record bindings anchor
    source-node participation; they do not filter the submitted graph or identify
    a uniquely guilty record/field. Actual OWLAPI interpretation, exact imports,
    OWL 2 DL and strict HermiT capabilities remain required. Missing explicit
    existential values and distinct names retain OWL's ordinary open-world
    semantics. Submission conformance is checked separately by check_rdf_emission.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children: list[Report] = []
    requests: tuple[RdfModelUsage, ...] = ()
    records: list[EmittedRecord | None] = []
    ontology_scope = emitted_scope = None
    original_imports: tuple[PreparedSource, ...] = ()
    derived = None
    diagnosis = None
    completed = False
    try:
        operation.consume("states", 8)
        operation.consume("bytes", 1024)
        requests = capture_bindings(bindings, operation)
        operation.consume("work", len(requests) * 3)
        records = [None] * len(requests)
        ontology_scope, schema = selected_scope(ontology, operation)
        emitted_scope, data = selected_scope(emitted, operation)
        counts, blanks = participation(data, operation)
        for index, binding in enumerate(requests):
            operation.consume("work")
            subject = binding.subject
            owned = not isinstance(subject, BNode) or blanks.get(id(subject)) is subject
            count = (
                counts.get(id(subject) if isinstance(subject, BNode) else subject, 0)
                if owned
                else None
            )
            issue = None
            if count is None or count == 0:
                issue = Issue(
                    "owl.emitted_record_unverified",
                    "Record node has no established participation "
                    "in this emitted snapshot"
                    if owned
                    else "Blank record node does not belong to this emitted snapshot",
                    binding.location or binding.model,
                    kind="unverified",
                )
                issues.append(issue)
            record_report = Report(
                "owl.emitted_record",
                int(count is not None and count > 0),
                () if issue is None else (issue,),
                requested=1,
                complete=issue is None,
            )
            records[index] = EmittedRecord(binding, count, record_report)
        # The factory preserves actual export reports in this buffer even if a
        # later snapshot fails. Shared receipt ownership prevents a second issue
        # charge when the enclosing operation adopts them.
        closure = capture_rdf_closure(
            ontology,
            imports,
            operation,
            additional=(emitted,),
            reports=children,
        )
        original_imports = closure.originals[1:]
        for child in closure.reports:
            retain_report(child, operation, issues)
        derived = joint_source(schema, data, ontology, operation)
        # Reserve result/report references before native execution: publishing a
        # returned paid global inconsistency must not require fresh capacity.
        operation.consume("states", 2)
        operation.consume("bytes", 128)
        diagnosis = check_consistency(
            backend,
            derived,
            named_classes=(),
            imports=closure.imports,
            budget=operation,
            timeout=max(0.000001, operation.deadline - time.monotonic()),
            heap_mb=heap_mb,
            cancel=operation.cancel,
        )
        children.append(diagnosis.report)
        try:
            retain_report(diagnosis.report, operation, issues)
        except LimitExceeded:
            issues.retain_completed(diagnosis.report)
            raise
        completed = diagnosis.report.complete and diagnosis.consistent is not None
    except LimitExceeded as failure:
        for child in children:
            issues.retain_completed(child)
        issues.exhausted(str(failure), location="owl.emitted")
    except (InputError, OSError) as failure:
        for child in children:
            issues.retain_completed(child)
        try:
            unavailable = isinstance(failure, ResourceError | OSError)
            issues.append(
                Issue(
                    "owl.emitted_unverified"
                    if unavailable
                    else "owl.emitted_input_error",
                    str(failure) or "Emitted RDF diagnosis is unavailable",
                    failure.location if isinstance(failure, InputError) else "",
                    kind="unverified" if unavailable else "input_error",
                    details=Location.from_error(failure),
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted), location="owl.emitted")
    pending = tuple(issue for issue in issues if issue.kind != "finding")
    unknown = Report("owl.emitted_record", 0, pending, requested=1, complete=False)
    retained = tuple(
        record if record is not None else EmittedRecord(requests[index], None, unknown)
        for index, record in enumerate(records)
    )
    attribution = Report(
        "owl.emitted_record_participation",
        sum(record.report.complete for record in retained),
        pending if not retained else (),
        requested=len(requests),
        complete=bool(requests) and all(record.report.complete for record in retained),
        children=tuple(record.report for record in retained),
    )
    report = Report(
        "owl.emitted_consistency",
        int(diagnosis is not None and diagnosis.consistent is not None)
        + attribution.checked,
        tuple(issues),
        requested=1 + len(requests),
        complete=completed and attribution.complete,
        children=(attribution, *children),
        regime="owl2-dl-hermit-1.4.5.519",
    )
    return EmittedConsistency(
        ontology_scope,
        emitted_scope,
        original_imports,
        derived,
        retained,
        diagnosis,
        attribution,
        report,
    )
