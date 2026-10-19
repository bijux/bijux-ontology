"""Actual syntactic locality extraction and independent selected consumer rechecks."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import Literal

from bijux_ontology._json import object_value
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report

from ...axioms.codec.decoding import ProjectionDecoder
from ...axioms.types import NamedEntity, ProjectedAxiom
from ...backend.owlapi.bundles import qualify_backend
from ...backend.owlapi.execution import invoke_worker, qualify_capability
from ...backend.owlapi.protocol import result_counter
from ...backend.types import OwlProfileBackend
from ...documents.sources.native import prepare_owl_document, stage_native_documents
from ...documents.types import OwlDocument
from ...inference.entailment.checking import check_entailments
from .preparation import prepare_locality
from .types import LocalityAxiom, LocalityMethod, LocalityModule


def _axioms(
    raw: object, originals: tuple[OwlDocument, ...], decoder: ProjectionDecoder
) -> tuple[LocalityAxiom, ...]:
    """Adopt exact annotated structural axioms with finite original owner indexes."""
    result = []
    for row in decoder.records(raw):
        decoder.budget.consume("bytes", 256)
        record = object_value(row, location="locality axiom")
        projection = object_value(record.get("projection"), location="locality axiom")
        values = decoder.records(projection.get("axioms"))
        if decoder.records(projection.get("unsupported")) or len(values) != 1:
            raise ResourceError("A retained module axiom has no complete typed view")
        raw_owners = decoder.records(record.get("documents"))
        decoder.budget.consume("bytes", 128 + 128 * len(raw_owners))
        decoder.budget.consume("states", len(raw_owners))
        owners = tuple(
            result_counter(index, decoder.budget, len(originals) - 1)
            for index in raw_owners
        )
        if not owners or tuple(sorted(set(owners))) != owners:
            raise ResourceError("Module axiom original owners are incomplete")
        result.append(LocalityAxiom(decoder.axiom(values[0]), owners))
    return tuple(result)


def extract_locality_module(
    backend: OwlProfileBackend,
    source: OwlDocument,
    signature: Sequence[NamedEntity],
    *,
    method: LocalityMethod,
    module_iri: str,
    consumer_entailments: Sequence[ProjectedAxiom],
    import_policy: Literal["flatten"],
    annotations: Literal["retain"],
    imports: Mapping[str, OwlDocument] | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> LocalityModule:
    """Extract one explicit maintained BOT/TOP/STAR module from native originals.

    The selected actual closure is flattened; native relevant entity/axiom and
    root ontology annotations are retained. Chosen entailments must use only
    selected signature roles or builtins and are checked on both original and
    standalone module. Missing entities, incomplete typed source coverage or
    incomplete extraction cannot certify a module. This is distinct from RDF
    neighborhoods/MIREOT and makes no universal conservativity claim.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    decoder = ProjectionDecoder(operation)
    originals: tuple[OwlDocument, ...] = ()
    selected: tuple[NamedEntity, ...] = ()
    selected_method: LocalityMethod | None = None
    axioms: tuple[LocalityAxiom, ...] = ()
    before = after = None
    candidate = capability = None
    reports: list[Report] = []
    complete = False
    try:
        plan = prepare_locality(
            source,
            signature,
            consumer_entailments,
            method,
            module_iri,
            imports,
            import_policy,
            annotations,
            operation,
        )
        selected = plan.signature
        selected_method = plan.method
        operation.consume("bytes", 128)
        before = check_entailments(
            backend,
            plan.source,
            plan.obligations,
            imports=plan.imports,
            budget=operation,
            timeout=timeout,
            heap_mb=heap_mb,
            cancel=cancel,
        )
        reports.append(before.report)
        retain_report(before.report, operation, issues)
        if not before.report.verified:
            raise ResourceError("Chosen original consumer entailments are not proved")
        qualify_backend(backend, operation)
        backend.artifacts.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(
            prefix="locality-module-", dir=backend.artifacts
        ) as job:
            directory = Path(job)
            originals, request = stage_native_documents(
                plan.source, plan.imports, directory, operation
            )
            maximum = operation.remaining("states")
            allowance = operation.remaining("issues")
            if not maximum or not allowance:
                raise LimitExceeded(
                    "Locality extraction has no native result allowance"
                )
            request.update(
                max_bytes=operation.limits.max_bytes,
                max_nodes=maximum,
                max_issues=allowance,
                max_resources=operation.limits.max_resources,
                projection_depth=max(1, min(64, operation.limits.max_depth // 4)),
            )
            request.update(
                operation="locality",
                method=plan.method,
                module_iri=plan.module_iri,
                signature=plan.signature_wire,
                consumer_entailments=plan.obligation_wire,
            )
            data = invoke_worker(backend, request, directory, operation, heap_mb)
            capability = qualify_capability(data, backend, operation)
            if decoder.text(data.get("method")) != plan.method:
                raise ResourceError(
                    "Native extraction used an unselected locality method"
                )
            for raw in decoder.records(data.get("source_unsupported")):
                issues.append(
                    Issue(
                        "owl.locality_source_unrepresented",
                        "An original axiom lies outside "
                        "the complete typed source profile",
                        actual=decoder.text(raw),
                        kind="unverified",
                    )
                )
            for raw in decoder.records(data.get("failures")):
                issues.append(
                    Issue(
                        "owl.locality_selection_failed",
                        decoder.text(raw),
                    )
                )
            if data.get("qualified") is not True:
                if data["status"] == "exhausted":
                    raise LimitExceeded("Native locality extraction did not complete")
                raise ResourceError("Native locality extraction is unqualified")
            axioms = _axioms(data.get("axioms"), originals, decoder)
            size = result_counter(
                data.get("bytes"), operation, operation.limits.max_bytes
            )
            operation.consume("bytes", size)
            path = directory / "candidate.owl"
            if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
                raise ResourceError("Checked module byte receipt is unavailable")
            candidate = prepare_owl_document(
                path.read_bytes(),
                format="functional",
                document_iri=plan.module_iri,
                effective_url=plan.module_iri,
                budget=operation,
            )
            operation.consume("bytes", 128)
            after = check_entailments(
                backend,
                candidate,
                plan.obligations,
                budget=operation,
                timeout=timeout,
                heap_mb=heap_mb,
                cancel=cancel,
            )
            reports.append(after.report)
            retain_report(after.report, operation, issues)
            if not after.report.verified:
                raise ResourceError(
                    "The standalone module did not prove selected entailments"
                )
            complete = True
    except LimitExceeded as error:
        issues.exhausted(str(error), location="owl.locality")
    except (InputError, OSError) as error:
        try:
            issues.append(
                Issue(
                    "owl.locality_unverified"
                    if isinstance(error, ResourceError | OSError)
                    else "owl.locality_input_error",
                    str(error),
                    kind="unverified"
                    if isinstance(error, ResourceError | OSError)
                    else "input_error",
                )
            )
        except LimitExceeded as stopped:
            issues.exhausted(str(stopped), location="owl.locality")
    for report in reports:
        issues.retain_completed(report)
    return LocalityModule(
        candidate,
        originals,
        selected,
        selected_method,
        axioms,
        before,
        after,
        capability,
        Report(
            "owl.locality",
            int(complete),
            tuple(issues),
            requested=1,
            complete=complete,
            children=tuple(reports),
            regime="owlapi-syntactic-locality-selected-consumer-proofs",
        ),
    )
