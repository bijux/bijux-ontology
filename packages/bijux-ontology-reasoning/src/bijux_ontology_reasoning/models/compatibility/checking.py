"""Actual model logical questions over one qualified original ontology closure."""

import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import cast

from bijux_ontology._json import object_value
from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import Payload, RdfSource
from bijux_ontology.reports import Issue, Report

from ...axioms.codec.decoding import ProjectionDecoder
from ...axioms.types import ClassExpression, SubClassOf
from ...backend.owlapi.bundles import qualify_backend
from ...backend.owlapi.execution import (
    invoke_worker,
    qualify_capability,
    result_counter,
    result_text,
)
from ...backend.owlapi.receipts import (
    _boolean,
    _records,
    qualify_reasoning_result,
    reasoning_header,
)
from ...backend.types import LoaderQualification, OwlProfileBackend, ProfileDocument
from ...datatypes.checking import check_datatype_branches
from ...datatypes.types import DatatypeCheck
from ...documents.sources.capture import capture_document, capture_imports
from ...documents.sources.native import stage_native_documents
from ...documents.sources.rdf import capture_rdf_closure, stage_documents
from ...documents.types import OwlDocument
from .planning import plan_models
from .types import ModelCompatibility, ModelLogicalAnswer, ModelQuestion


def _expressions(
    value: object, decoder: ProjectionDecoder
) -> tuple[ClassExpression | None, ClassExpression | None]:
    """Read the existing native typed projection; no expression parser is added."""
    if value is None:
        return None, None
    record = object_value(value, location="model native question")
    unsupported = _records(record.get("unsupported"), decoder.budget)
    for kind in unsupported:
        decoder.text(kind)
    axioms = _records(record.get("axioms"), decoder.budget)
    if unsupported:
        return None, None
    if len(axioms) != 1:
        raise ResourceError("Native model question omitted its typed expression")
    axiom = decoder.axiom(axioms[0])
    if type(axiom) is not SubClassOf or axiom.annotations:
        raise ResourceError("Native model question has the wrong structural role")
    return axiom.sub_class, axiom.super_class


def _answer(
    question: ModelQuestion,
    item: dict[str, object],
    decoder: ProjectionDecoder,
    issues: IssueCollector,
) -> ModelLogicalAnswer:
    """Qualify each distinct raw method before interpreting application acceptance."""
    budget = decoder.budget
    budget.consume("states", 3)
    budget.consume("bytes", 512)
    error = result_text(item.get("error"), budget, optional=True)
    entailed = None if item.get("entailed") is None else _boolean(item["entailed"])
    satisfiable = (
        None if item.get("satisfiable") is None else _boolean(item["satisfiable"])
    )
    required_question = question.kind == "required_subsumption"
    if (required_question and satisfiable is not None) or (
        not required_question and entailed is not None
    ):
        raise ResourceError("Native model method contradicts the requested question")
    actual, required = _expressions(item.get("query"), decoder)
    raw = entailed if required_question else satisfiable
    completed = (
        error is None
        and actual is not None
        and required is not None
        and raw is not None
    )
    local: list[Issue] = []
    if not completed:
        local.append(
            Issue(
                "models.logical_question_unverified",
                "The selected model logical question is unsupported or incomplete",
                question.location,
                actual=error,
                kind="unverified",
            )
        )
    elif raw is False:
        conditional = question.kind == "coemission_contradiction" and (
            not question.guaranteed or not question.covered
        )
        local.append(
            Issue(
                "models.logical_conditional_conflict_unverified"
                if conditional
                else "models.required_subsumption_not_entailed"
                if required_question
                else "models.logical_coemission_conflict"
                if question.kind == "coemission_contradiction"
                else "models.logical_types_unsatisfiable",
                "A possible co-emission combination is contradictory "
                "without a declared promise"
                if conditional
                else "The required subsumption is not entailed"
                if required_question
                else "Declared co-emission types cannot hold simultaneously"
                if question.kind == "coemission_contradiction"
                else "Declared simultaneous types are unsatisfiable",
                question.location,
                kind="unverified" if conditional else "finding",
            )
        )
    if not question.covered:
        local.append(
            Issue(
                "models.logical_scenario_unverified",
                "The selected finite state lacks explicit scenario coverage",
                question.location,
                kind="unverified",
            )
        )
    for issue in local:
        issues.append(issue)
    return ModelLogicalAnswer(
        question,
        actual,
        required if required_question else None,
        entailed,
        satisfiable,
        error,
        Report(
            "models.logical_question",
            int(completed),
            tuple(local),
            requested=1,
            complete=completed,
            regime="owl2-dl-hermit-1.4.5.519",
        ),
    )


def check_model_compatibility(
    backend: OwlProfileBackend,
    source: OwlDocument | PreparedSource,
    models: Sequence[ModelDeclaration],
    *,
    imports: Mapping[str, OwlDocument | RdfSource | Payload] | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> ModelCompatibility:
    """Discharge a documented automatic finite class/domain/range model subset.

    Required endpoint subsumption, possible class satisfiability and prohibited
    co-emission contradictions stay separate. Native class reasoning can resolve
    anonymous intersections/existentials; it cannot prove a source field was
    emitted. Normal serialization, output counts and independent model contracts
    remain with ordinary model/output checking. Global literal ranges delegate to
    the separate datatype operation. Lists, resource bridges, unsupported contexts
    and uncovered finite scenarios remain required unknown at original locations.
    RDF imports are captured once through the maintained owned closure boundary;
    repeated class and datatype reasoning cannot reread caller streams or graphs.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    decoder = ProjectionDecoder(operation)
    declarations: tuple[ModelDeclaration, ...] = ()
    questions: tuple[ModelQuestion, ...] = ()
    answers: list[ModelLogicalAnswer | None] = []
    datatypes: list[DatatypeCheck] = []
    children: list[Report] = []
    originals: tuple[OwlDocument | ProfileDocument, ...] = ()
    loaders: tuple[LoaderQualification, ...] = ()
    consistent = raw_consistent = in_profile = None
    native_error = None
    reasoner = capability = None
    consistency_report = Report("owl.consistency", requested=1, complete=False)
    finished = False
    datatype_count = 0
    datatype_source = source
    datatype_imports = imports
    rdf_sources: tuple[PreparedSource, ...] = ()
    rdf_metadata: tuple[SnapshotMetadata, ...] = ()
    try:
        plan = plan_models(models, operation, issues)
        declarations, questions = plan.models, plan.questions
        datatype_count = len(plan.datatypes)
        operation.consume("bytes", 128 + 64 * (len(questions) + datatype_count))
        answers = [None] * len(questions)
        if not questions:
            if issues:
                raise ResourceError("No selected model declaration can be qualified")
            raise InputError("No supported model logical questions were supplied")
        if isinstance(source, PreparedSource):
            initial_scope = SnapshotMetadata.capture(source, operation)
            closure = capture_rdf_closure(
                source,
                cast(Mapping[str, RdfSource | Payload] | None, imports),
                operation,
                reports=children,
            )
            rdf_sources = closure.originals
            operation.consume("bytes", 64 + 16 * len(rdf_sources))
            rdf_metadata = tuple(
                SnapshotMetadata.capture(item, operation) for item in rdf_sources
            )
            if not rdf_metadata or not initial_scope.same_scope(rdf_metadata[0]):
                raise ResourceError("Logical RDF source scope changed during capture")
            datatype_source, datatype_imports = closure.source, closure.imports
        qualify_backend(backend, operation)
        backend.artifacts.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(
            prefix="model-compatibility-", dir=backend.artifacts
        ) as job:
            directory = Path(job)
            if isinstance(source, OwlDocument):
                captured = capture_document(source, operation)
                captured_imports = capture_imports(
                    cast(Mapping[str, OwlDocument] | None, imports), operation
                )
                originals, request = stage_native_documents(
                    captured, captured_imports, directory, operation
                )
                datatype_source, datatype_imports = captured, captured_imports
                kind = "native"
            elif isinstance(source, PreparedSource):
                originals, request, _ = stage_documents(
                    cast(PreparedSource, datatype_source),
                    cast(Mapping[str, RdfSource | Payload] | None, datatype_imports),
                    directory,
                    operation,
                    issues,
                    children,
                )
                kind = "rdf"
            else:
                raise InputError(
                    "Model compatibility requires an owned native or RDF source"
                )
            maximum = min(operation.remaining("issues"), operation.limits.max_states)
            if not maximum or not operation.remaining("states"):
                raise LimitExceeded("Model compatibility has no result allowance")
            request.update(
                operation="model_compatibility",
                input_kind=kind,
                queries=plan.wire,
                max_nodes=operation.remaining("states"),
                max_issues=maximum,
                max_bytes=operation.limits.max_bytes,
                max_resources=operation.limits.max_resources,
                query_depth=max(1, min(64, operation.limits.max_depth // 4)),
                task_timeout_ms=max(
                    1,
                    min(2**31 - 1, int((operation.deadline - time.monotonic()) * 1000)),
                ),
            )
            data = invoke_worker(backend, request, directory, operation, heap_mb)
            capability = qualify_capability(data, backend, operation)
            children.append(capability.report)
            header = reasoning_header(data, operation)
            raw_consistent, in_profile, native_error = (
                header.raw_consistent,
                header.in_profile,
                header.native_error,
            )
            receipt = qualify_reasoning_result(
                data, originals, kind, operation, issues, header=header, scope="models"
            )
            loaders, reasoner = receipt.loaders, receipt.reasoner
            if not receipt.qualified:
                if data["status"] == "exhausted":
                    raise LimitExceeded(
                        "Model native input qualification did not complete"
                    )
                raise ResourceError("The selected DL model scope is unqualified")
            consistent = raw_consistent
            if consistent is not None:
                operation.consume("bytes", 256)
                finding = (
                    None
                    if consistent
                    else Issue(
                        "owl.inconsistent", "The selected ontology is inconsistent"
                    )
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
            if consistent is not True:
                issues.append(
                    Issue(
                        "models.logical_consistency_unverified",
                        "Model logical acceptance requires "
                        "a consistent selected ontology",
                        kind="unverified",
                    )
                )
            else:
                seen: set[int] = set()
                for raw in _records(data.get("answers"), operation):
                    item = object_value(raw, location="native model logical answer")
                    index = result_counter(
                        item.get("index"), operation, len(questions) - 1
                    )
                    if index in seen:
                        raise ResourceError(
                            "Native model answer has duplicate ownership"
                        )
                    operation.consume("states")
                    seen.add(index)
                    answer = _answer(questions[index], item, decoder, issues)
                    answers[index] = answer
                    children.append(answer.report)
                if len(seen) != len(questions):
                    if data["status"] == "exhausted":
                        raise LimitExceeded(
                            "Native model questions exhausted their allowance"
                        )
                    raise ResourceError(
                        "Native model operation omitted required questions"
                    )
                if data["status"] == "exhausted":
                    raise LimitExceeded(
                        "Native model operation exhausted its allowance"
                    )
                if any(
                    not metadata.same_scope(SnapshotMetadata.capture(item, operation))
                    for item, metadata in zip(rdf_sources, rdf_metadata, strict=True)
                ):
                    raise ResourceError(
                        "Logical RDF source scope changed during class reasoning"
                    )
                if plan.datatypes:
                    operation.consume("bytes", 128)
                    datatype = check_datatype_branches(
                        backend,
                        datatype_source,
                        plan.datatypes,
                        imports=datatype_imports,
                        budget=operation,
                        timeout=timeout,
                        heap_mb=heap_mb,
                        cancel=cancel,
                    )
                    try:
                        retain_report(datatype.report, operation, issues)
                    except LimitExceeded:
                        issues.retain_completed(datatype.report)
                        datatypes.append(datatype)
                        children.append(datatype.report)
                        raise
                    datatypes.append(datatype)
                    children.append(datatype.report)
                if any(
                    not metadata.same_scope(SnapshotMetadata.capture(item, operation))
                    for item, metadata in zip(rdf_sources, rdf_metadata, strict=True)
                ):
                    raise ResourceError(
                        "Logical RDF source scope changed during datatype reasoning"
                    )
                finished = True
    except LimitExceeded as error:
        issues.exhausted(str(error), location="models.logical_compatibility")
    except (InputError, OSError) as error:
        try:
            issues.append(
                Issue(
                    "models.logical_unverified"
                    if isinstance(error, ResourceError | OSError)
                    else "models.logical_input_error",
                    str(error),
                    kind="unverified"
                    if isinstance(error, ResourceError | OSError)
                    else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted), location="models.logical_compatibility")
    for child in children:
        issues.retain_completed(child)
    pending = (
        Issue(
            "models.logical_not_executed",
            "The required logical question was not completed",
            kind="unverified",
        ),
    )
    completed = tuple(
        answer
        if answer is not None
        else ModelLogicalAnswer(
            question,
            None,
            None,
            None,
            None,
            native_error,
            Report("models.logical_question", 0, pending, requested=1, complete=False),
        )
        for question, answer in zip(questions, answers, strict=True)
    )
    report = Report(
        "models.logical_compatibility",
        int(consistent is not None)
        + sum(
            answer.raw_entailed is not None or answer.raw_satisfiable is not None
            for answer in completed
        )
        + sum(
            answer.outcome != "unknown"
            for datatype in datatypes
            for answer in datatype.answers
        ),
        tuple(issues),
        requested=1 + len(questions) + datatype_count,
        complete=finished,
        children=tuple(children),
        regime="selected-model-logical-contracts",
    )
    return ModelCompatibility(
        declarations,
        completed,
        tuple(datatypes),
        consistent,
        raw_consistent,
        in_profile,
        native_error,
        originals,
        rdf_sources,
        loaders,
        reasoner,
        capability,
        consistency_report,
        report,
    )
