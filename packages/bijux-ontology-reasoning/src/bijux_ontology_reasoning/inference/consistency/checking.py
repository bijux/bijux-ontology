"""Owned native DL diagnosis with exact input and required query coverage."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import cast

from rdflib import URIRef

from bijux_ontology._json import object_value
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import Payload, RdfSource

from ...backend.owlapi.bundles import qualify_backend
from ...backend.owlapi.execution import invoke_worker, qualify_capability
from ...backend.owlapi.protocol import result_boolean as _boolean
from ...backend.owlapi.protocol import result_records as _records
from ...backend.owlapi.protocol import result_text
from ...backend.owlapi.receipts import qualify_reasoning_result, reasoning_header
from ...backend.types import LoaderQualification, OwlProfileBackend, ProfileDocument
from ...documents.sources.native import stage_native_documents
from ...documents.sources.rdf import stage_documents
from ...documents.types import OwlDocument
from .types import ClassSatisfiability, ConsistencyCheck


def _classes(
    selection: Sequence[URIRef] | None, budget: Budget
) -> tuple[URIRef, ...] | None:
    """Capture exact named requests before import maps or other caller callbacks."""
    if selection is None:
        return None
    if not isinstance(selection, Sequence) or isinstance(selection, str | bytes):
        raise InputError("Named classes require a finite URIRef sequence or None")
    size = len(selection)
    budget.consume("states", size)
    budget.consume("bytes", 64 + 32 * size)
    seen: set[URIRef] = set()
    selected = []
    for index in range(size):
        budget.consume("work")
        value = selection[index]
        if type(value) is not URIRef:
            raise InputError("Named class selections require URIRef terms")
        bound_text(value, budget)
        absolute_iri(value)
        if value in seen:
            raise InputError("Named class requests must be distinct")
        seen.add(value)
        selected.append(value)
    return tuple(selected)


def check_consistency(
    backend: OwlProfileBackend,
    source: OwlDocument | PreparedSource,
    *,
    named_classes: Sequence[URIRef] | None = None,
    imports: Mapping[str, OwlDocument | RdfSource | Payload] | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> ConsistencyCheck:
    """Ask actual HermiT consistency and separate finite named-class questions.

    None selects the actual named-class signature of the required import closure.
    Explicit classes must occur in that signature. Exact original native input or
    qualified RDF loader receipts, the complete selected closure and OWL 2 DL
    membership precede reasoning. Unsupported work never selects a weaker engine.
    JVM work has an owned heap/deadline; host result retention shares Budget.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children: list[Report] = []
    documents: tuple[OwlDocument | ProfileDocument, ...] = ()
    loaders: tuple[LoaderQualification, ...] = ()
    selected = None
    answers: list[ClassSatisfiability] = []
    capability = reasoner = None
    consistent = raw_consistent = in_profile = None
    native_error = None
    complete = False
    consistency_report = Report("owl.consistency", requested=1, complete=False)
    try:
        selected = _classes(named_classes, operation)
        qualify_backend(backend, operation)
        backend.artifacts.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="diagnosis-", dir=backend.artifacts) as job:
            directory = Path(job)
            if isinstance(source, OwlDocument):
                documents, request = stage_native_documents(
                    source,
                    cast(Mapping[str, OwlDocument] | None, imports),
                    directory,
                    operation,
                )
                kind = "native"
            elif isinstance(source, PreparedSource):
                documents, request, _ = stage_documents(
                    source,
                    cast(Mapping[str, RdfSource | Payload] | None, imports),
                    directory,
                    operation,
                    issues,
                    children,
                )
                kind = "rdf"
            else:
                raise InputError("Diagnosis requires an owned RDF or native document")
            maximum = min(operation.remaining("issues"), operation.limits.max_states)
            if not maximum or not operation.remaining("states"):
                raise LimitExceeded("DL diagnosis has no remaining result allowance")
            request.update(
                operation="diagnosis",
                input_kind=kind,
                classes=selected,
                max_bytes=operation.limits.max_bytes,
                max_issues=maximum,
                max_resources=operation.limits.max_resources,
                max_nodes=operation.remaining("states"),
                task_timeout_ms=max(
                    1,
                    min(2**31 - 1, int((operation.deadline - time.monotonic()) * 1000)),
                ),
            )
            data = invoke_worker(backend, request, directory, operation, heap_mb)
            operation.consume("states", 4)
            capability = qualify_capability(data, backend, operation)
            children.append(capability.report)
            header = reasoning_header(data, operation)
            raw_consistent, in_profile, native_error = (
                header.raw_consistent,
                header.in_profile,
                header.native_error,
            )
            receipt = qualify_reasoning_result(
                data,
                documents,
                kind,
                operation,
                issues,
                header=header,
                scope="diagnosis",
            )
            raw_consistent, in_profile, native_error = (
                receipt.raw_consistent,
                receipt.in_profile,
                receipt.native_error,
            )
            loaders, reasoner = receipt.loaders, receipt.reasoner
            if not receipt.qualified:
                if data["status"] == "exhausted":
                    raise LimitExceeded(
                        "DL native input qualification did not complete"
                    )
                raise ResourceError("The selected DL reasoning scope is unqualified")
            actual_selection = []
            for raw in _records(data.get("selected"), operation):
                iri = result_text(raw, operation)
                assert iri is not None
                absolute_iri(iri)
                actual_selection.append(URIRef(iri))
            if len(set(actual_selection)) != len(actual_selection) or (
                selected is not None and tuple(actual_selection) != selected
            ):
                raise ResourceError(
                    "DL selected classes do not cover the exact request"
                )
            selected = tuple(actual_selection)
            operation.consume("states", 2 + len(selected))
            operation.consume("bytes", 256 + 64 * len(selected))
            if raw_consistent is not None:
                consistent = raw_consistent
                finding = (
                    Issue("owl.inconsistent", "The selected ontology is inconsistent")
                    if not consistent
                    else None
                )
                if finding is not None:
                    issues.append(finding)
                consistency_report = Report(
                    "owl.consistency",
                    1,
                    () if finding is None else (finding,),
                    requested=1,
                )
                children.append(consistency_report)
            seen = set()
            for raw in _records(data.get("classes"), operation):
                item = object_value(raw, location="DL class satisfiability")
                text = result_text(item.get("iri"), operation)
                assert text is not None
                iri = URIRef(text)
                satisfiable = _boolean(item.get("satisfiable"))
                if consistent is not True or iri not in selected or iri in seen:
                    raise ResourceError(
                        "DL class answer lacks exact qualified coverage"
                    )
                operation.consume("states", 2)
                operation.consume("bytes", 128)
                seen.add(iri)
                finding = (
                    Issue(
                        "owl.unsatisfiable_class",
                        "The selected named class is unsatisfiable",
                        str(iri),
                    )
                    if not satisfiable
                    else None
                )
                if finding is not None:
                    issues.append(finding)
                report = Report(
                    "owl.class_satisfiability",
                    1,
                    () if finding is None else (finding,),
                    requested=1,
                )
                answers.append(ClassSatisfiability(iri, satisfiable, report))
                children.append(report)
            if data["status"] == "exhausted":
                raise LimitExceeded("DL native diagnosis did not complete")
            if consistent is None or (consistent and seen != set(selected)):
                raise ResourceError("DL diagnosis omitted required completed answers")
            complete = True
    except LimitExceeded as error:
        issues.exhausted(str(error), location="owl.diagnosis")
    except (InputError, OSError):
        try:
            issues.append(
                Issue(
                    "owl.diagnosis_unverified",
                    "DL diagnosis or its exact selected interpretation is unavailable",
                    kind="unverified",
                )
            )
        except LimitExceeded as error:
            issues.exhausted(str(error), location="owl.diagnosis")
    report = Report(
        "owl.diagnosis",
        int(consistent is not None) + len(answers),
        tuple(issues),
        requested=1 + (len(selected) if selected is not None else 0),
        complete=complete,
        children=tuple(children),
        regime="owl2-dl-hermit-1.4.5.519",
    )
    return ConsistencyCheck(
        consistent,
        raw_consistent,
        in_profile,
        native_error,
        tuple(answers),
        selected,
        documents,
        loaders,
        reasoner,
        capability,
        consistency_report,
        report,
    )
