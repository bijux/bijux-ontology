"""Native bounded explanations preserve independently established logical truth."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

from rdflib import URIRef

from bijux_ontology._json import object_value
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report

from ...backend.owlapi.execution import invoke_worker, qualify_capability
from ...backend.owlapi.protocol import result_counter, result_records, result_text
from ...backend.owlapi.receipts import qualify_reasoning_result, reasoning_header
from ...backend.types import OwlProfileBackend
from ...documents.sources.capture import capture_document, capture_imports
from ...documents.sources.native import prepare_owl_document, stage_native_documents
from ...documents.types import OwlDocument
from ..consistency.checking import check_consistency
from ..consistency.types import ConsistencyCheck
from ..entailment.checking import check_entailments
from ..entailment.types import EntailmentCheck
from .encoding import capture_question
from .types import (
    AxiomSupport,
    ExplanationCheck,
    ExplanationQuestion,
    Justification,
    SourceAxiom,
)


def _observation(
    backend: OwlProfileBackend,
    source: OwlDocument,
    question: ExplanationQuestion,
    imports: Mapping[str, OwlDocument] | None,
    operation: Operation,
    timeout: float,
    heap_mb: int,
) -> ConsistencyCheck | EntailmentCheck:
    """Establish the exact selected observation through existing native operations."""
    if question.kind == "entailment":
        assert question.axiom is not None
        return check_entailments(
            backend,
            source,
            (question.axiom,),
            imports=imports,
            budget=operation,
            timeout=timeout,
            heap_mb=heap_mb,
        )
    classes = () if question.class_iri is None else (URIRef(question.class_iri),)
    return check_consistency(
        backend,
        source,
        named_classes=classes,
        imports=imports,
        budget=operation,
        timeout=timeout,
        heap_mb=heap_mb,
    )


def _applicable(
    value: ConsistencyCheck | EntailmentCheck, question: ExplanationQuestion
) -> bool | None:
    """Opposite proved answers differ from unavailable required explanation work."""
    if question.kind == "entailment":
        assert isinstance(value, EntailmentCheck)
        if value.consistent is not True or len(value.answers) != 1:
            return None
        answer = value.answers[0]
        return answer.raw_entailed if answer.report.complete else None
    assert isinstance(value, ConsistencyCheck)
    if question.kind == "inconsistency":
        return None if value.consistent is None else not value.consistent
    if value.consistent is not True or len(value.classes) != 1:
        return None
    answer_class = value.classes[0]
    return not answer_class.satisfiable if answer_class.report.complete else None


def _rendering(raw: Mapping[str, object], operation: Operation) -> tuple[str, str]:
    """Verify the pinned rendering hash after bounding every UTF-8 allocation."""
    text = result_text(raw.get("rendering"), operation)
    digest = result_text(raw.get("sha256"), operation)
    assert text is not None and digest is not None
    operation.consume("bytes", 4 * len(text))
    operation.consume("work", len(text) + 1)
    encoded = text.encode("utf-8")
    if hashlib.sha256(encoded).hexdigest() != digest:
        raise ResourceError("Explanation structural rendering differs from its hash")
    return text, digest


def _justification(
    data: Mapping[str, object],
    documents: tuple[OwlDocument, ...],
    directory: Path,
    maximum: int,
    operation: Operation,
) -> Justification:
    """Adopt actual source structures and checked independently replayed proof bytes."""
    if (
        data.get("reproduced") is not True
        or data.get("minimality") != "neither"
        or data.get("enumeration_complete") is not False
    ):
        raise ResourceError(
            "Native explanation lacks its exact replay and search boundary"
        )
    records = result_records(data.get("support"), operation)
    if len(records) > maximum:
        raise LimitExceeded("Explanation exceeds the selected axiom allowance")
    operation.consume("states", 3)
    operation.consume("bytes", 512)
    axioms = []
    seen = set()
    for raw in records:
        item = object_value(raw, location="explanation support")
        text, digest = _rendering(item, operation)
        if digest in seen:
            raise ResourceError("Explanation repeats one support axiom")
        seen.add(digest)
        origins = []
        origin_keys = set()
        for entry in result_records(item.get("origins"), operation):
            original = object_value(entry, location="explanation source structure")
            index = result_counter(
                original.get("document"), operation, len(documents) - 1
            )
            rendering, fingerprint = _rendering(original, operation)
            key = (index, fingerprint)
            if key in origin_keys:
                raise ResourceError("Explanation repeats one source structure")
            operation.consume("states", 2)
            operation.consume("bytes", 128)
            origin_keys.add(key)
            origins.append(SourceAxiom(documents[index], rendering, fingerprint))
        if not origins:
            raise ResourceError(
                "Support axiom lacks established original-source correlation"
            )
        operation.consume("states")
        operation.consume("bytes", 128)
        axioms.append(AxiomSupport(text, digest, tuple(origins)))
    declarations = []
    for value in result_records(data.get("declarations"), operation):
        declaration = result_text(value, operation)
        assert declaration is not None
        declarations.append(declaration)
    size = result_counter(
        data.get("proof_bytes"), operation, operation.limits.max_bytes
    )
    operation.consume("bytes", size + 1)
    operation.consume("work", size + 1)
    path = directory / "justification.owl"
    if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
        raise ResourceError("Explanation lacks its exact owned proof-byte receipt")
    with path.open("rb") as reader:
        content = reader.read(size + 1)
    if len(content) != size:
        raise ResourceError("Explanation proof bytes changed during adoption")
    source = prepare_owl_document(
        content,
        format="functional",
        document_iri="urn:bijux:ontology:justification",
        budget=operation,
    )
    return Justification(
        source,
        tuple(axioms),
        tuple(declarations),
        True,
        "neither",
        False,
        Report(
            "owl.justification",
            1,
            requested=1,
            regime="owlexplanation-5.0.1-native-replay",
        ),
    )


def explain_reasoning(
    backend: OwlProfileBackend,
    source: OwlDocument,
    question: ExplanationQuestion,
    *,
    imports: Mapping[str, OwlDocument] | None = None,
    max_axioms: int = 64,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> ExplanationCheck:
    """Return one bounded native justification without changing underlying truth.

    This native-document profile uses maintained OWLAPI/HermiT and
    owlexplanation 5.0.1. Source correlation identifies annotated structural
    axioms in supplied modules, never physical source coordinates. Proof bytes
    are independently reread and reasoned over by the native worker. Generated
    signature declarations are explicit replay scaffolding. Neither subset
    minimality, smallest size, uniqueness nor complete enumeration is claimed.
    Missing or bounded explanations retain the original proved observation.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    retained = IssueCollector(operation)
    captured = None
    underlying = None
    applicable = None
    justification = None
    generator = capability = None
    children: list[Report] = []
    finished = False
    try:
        operation.consume("states", 8)
        operation.consume("bytes", 1024)
        captured, wire = capture_question(question, operation)
        if type(max_axioms) is not int or max_axioms < 1:
            raise InputError("max_axioms must be a positive ordinary integer")
        original = capture_document(source, operation)
        supplied = capture_imports(imports, operation)
        underlying = _observation(
            backend, original, captured, supplied, operation, timeout, heap_mb
        )
        children.append(underlying.report)
        retain_report(underlying.report, operation, retained)
        applicable = _applicable(underlying, captured)
        if applicable is None:
            raise ResourceError(
                "The selected underlying logical observation is unqualified"
            )
        if applicable:
            backend.artifacts.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(
                prefix="logical-explanation-", dir=backend.artifacts
            ) as job:
                directory = Path(job)
                documents, request = stage_native_documents(
                    original, supplied, directory, operation
                )
                maximum = min(
                    operation.remaining("states"), operation.limits.max_states
                )
                if not maximum or not operation.remaining("issues"):
                    raise LimitExceeded(
                        "Explanation has no remaining native inventory allowance"
                    )
                request.update(
                    operation="explanations",
                    input_kind="native",
                    question=wire,
                    max_axioms=min(max_axioms, maximum),
                    max_nodes=maximum,
                    max_bytes=operation.limits.max_bytes,
                    max_issues=operation.remaining("issues"),
                    max_resources=operation.limits.max_resources,
                    query_depth=max(1, min(64, operation.limits.max_depth // 4)),
                    task_timeout_ms=max(
                        1,
                        min(
                            2**31 - 1,
                            int((operation.deadline - time.monotonic()) * 1000),
                        ),
                    ),
                )
                data = invoke_worker(backend, request, directory, operation, heap_mb)
                capability = qualify_capability(data, backend, operation)
                header = reasoning_header(data, operation)
                receipt = qualify_reasoning_result(
                    data,
                    documents,
                    "native",
                    operation,
                    issues,
                    header=header,
                    scope="explanations",
                )
                generator = result_text(data.get("generator_version"), operation)
                if generator != "5.0.1":
                    raise ResourceError("Unsupported native explanation generator")
                if data["status"] == "exhausted":
                    raise LimitExceeded(
                        "Native justification did not complete within its bounds"
                    )
                if (
                    not receipt.qualified
                    or receipt.raw_consistent != underlying.consistent
                    or data.get("applicable") is not True
                    or receipt.native_error is not None
                ):
                    raise ResourceError(
                        "Native explanation differs from the original "
                        "qualified observation"
                    )
                justification = _justification(
                    data, documents, directory, max_axioms, operation
                )
        finished = True
    except LimitExceeded as error:
        if underlying is not None:
            retained.retain_completed(underlying.report)
        issues.exhausted(str(error), location="owl.explanation")
    except (InputError, OSError) as error:
        try:
            unknown = isinstance(error, ResourceError | OSError)
            issues.append(
                Issue(
                    "owl.explanation_unverified"
                    if unknown
                    else "owl.explanation_input_error",
                    str(error),
                    "" if captured is None else captured.location,
                    kind="unverified" if unknown else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted), location="owl.explanation")
    explanation = Report(
        "owl.explanation",
        int(justification is not None),
        tuple(issues),
        allow_empty=applicable is False,
        requested=0 if applicable is False else 1,
        complete=finished,
        regime="owlexplanation-5.0.1-one-native-justification",
        exclusions=("The selected logical observation is proved false",)
        if applicable is False
        else (),
    )
    children.append(explanation)
    report = Report(
        "owl.explanation_with_observation",
        int(applicable is not None) + int(justification is not None),
        requested=1 if applicable is False else 2,
        complete=finished,
        children=tuple(children),
        regime="owl2-dl-hermit-and-owlexplanation-5.0.1",
    )
    return ExplanationCheck(
        captured,
        underlying,
        applicable,
        justification,
        generator,
        capability,
        explanation,
        report,
    )
