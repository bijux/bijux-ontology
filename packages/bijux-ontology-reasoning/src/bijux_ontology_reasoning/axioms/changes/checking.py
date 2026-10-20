"""Checked root-axiom changes over original offline native documents."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import cast

from bijux_ontology._json import object_value
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Issue, Report

from ...backend.owlapi.bundles import PROFILES, qualify_backend
from ...backend.owlapi.execution import (
    invoke_worker,
    qualify_capability,
    result_counter,
)
from ...backend.types import OwlProfile, OwlProfileBackend
from ...documents.conversion import _profiles
from ...documents.sources.native import prepare_owl_document, stage_native_documents
from ...documents.types import NativeProfile, OwlDocument
from ..decoding import ProjectionDecoder
from ..types import ProjectedAxiom
from .encoding import ConstructionEncoder
from .types import AxiomBase, AxiomCandidate, AxiomCheck, AxiomPatch


def receipt(source: OwlDocument) -> tuple[object, ...]:
    """Bind checked bytes, parser selection and both resolution identities."""
    return (
        source.content,
        source.format,
        source.document_iri,
        source.effective_url,
        source.sha256,
    )


def _owned(source: OwlDocument, budget: Budget) -> OwlDocument:
    """Capture one ordinary descriptor without trusting subclass field callbacks."""
    if type(source) is not OwlDocument:
        raise InputError("Axiom transactions require an ordinary prepared OwlDocument")
    content, syntax, identity, effective, digest = (
        source.content,
        source.format,
        source.document_iri,
        source.effective_url,
        source.sha256,
    )
    if type(content) is not bytes or type(digest) is not str:
        raise InputError("Invalid native source record")
    result = prepare_owl_document(
        content,
        format=syntax,
        document_iri=identity,
        effective_url=effective,
        budget=budget,
    )
    bound_text(digest, budget)
    if result.sha256 != digest:
        raise InputError("Prepared source bytes differ from their captured hash")
    return result


def _base(source: OwlDocument) -> AxiomBase:
    """Retain already-qualified immutable scalar preconditions."""
    return AxiomBase(
        source.sha256, source.format, source.document_iri, source.effective_url
    )


def axiom_base(
    source: OwlDocument, *, limits: Limits | None = None, budget: Budget | None = None
) -> AxiomBase:
    """Capture raw integrity together with syntax and exact resolution identities."""
    operation = Operation(limits, budget, 30, None)
    return _base(_owned(source, operation))


def _expected(expected: AxiomBase, budget: Budget) -> AxiomBase:
    """Prepay required scalars before comparing interpretation context."""
    if type(expected) is not AxiomBase:
        raise InputError("Expected a complete typed native axiom base")
    for value in (
        expected.sha256,
        expected.format,
        expected.document_iri,
        expected.effective_url,
    ):
        if type(value) is not str:
            raise InputError("Native axiom base fields require plain text")
        bound_text(value, budget)
    return expected


def _imports(
    values: Mapping[str, OwlDocument] | None, budget: Budget
) -> Mapping[str, OwlDocument] | None:
    """Capture supplied import bytes before trusted application callbacks run."""
    if values is None:
        return None
    if not isinstance(values, Mapping):
        raise InputError("Native imports need an explicit finite mapping")
    size = len(values)
    budget.consume("resources", size)
    budget.consume("states", size)
    budget.consume("bytes", 64 + size * 128)
    result: dict[str, OwlDocument] = {}
    for alias, source in values.items():
        if len(result) >= size or type(alias) is not str:
            raise InputError("Import mapping changed or has a nontext alias")
        bound_text(alias, budget)
        result[alias] = _owned(source, budget)
    if len(result) != size:
        raise InputError("Import mapping changed during capture")
    return result


def _stage(
    source: OwlDocument,
    imports: Mapping[str, OwlDocument] | None,
    directory: Path,
    operation: Operation,
) -> dict[str, object]:
    """Reuse the bounded original-native import staging boundary."""
    _, request = stage_native_documents(source, imports, directory, operation)
    allowance = min(operation.remaining("states"), operation.limits.max_states)
    issues = operation.remaining("issues")
    if not allowance or not issues:
        raise LimitExceeded(
            "Axiom operation has no remaining native inventory allowance"
        )
    request.update(
        max_bytes=operation.limits.max_bytes,
        max_nodes=allowance,
        max_issues=issues,
        max_resources=operation.limits.max_resources,
        projection_depth=max(1, min(64, operation.limits.max_depth // 4)),
    )
    return request


def _delta(
    raw: object, decoder: ProjectionDecoder, issues: IssueCollector
) -> tuple[ProjectedAxiom, ...]:
    """Retain actual changed axioms; unsupported changed families remain required."""
    record = object_value(raw, location="native axiom delta")
    result = []
    for item in decoder.records(record.get("axioms")):
        decoder.budget.consume("bytes", 64)
        result.append(decoder.axiom(item))
    for kind in decoder.records(record.get("unsupported")):
        text = decoder.text(kind)
        issues.append(
            Issue(
                "owl.axiom_change_unrepresented",
                "A changed native axiom has no complete supported typed projection",
                actual=text,
                kind="unverified",
            )
        )
    return tuple(result)


def _losses(raw: object, decoder: ProjectionDecoder, issues: IssueCollector) -> None:
    """Keep original-input and reread interpretation losses distinct from edits."""
    for item in decoder.records(raw):
        record = object_value(item, location="native change loss")
        kind, value = (
            decoder.text(record.get("kind")),
            decoder.text(record.get("value")),
        )
        issues.append(
            Issue(
                "owl.axiom_change_loss",
                "Native input or output has an unaccounted structural difference",
                expected=kind,
                actual=value,
                kind="unverified",
            )
        )


def _error(issues: IssueCollector, error: InputError | OSError, location: str) -> None:
    """Preserve ordinary input/unknown failures and paid-prefix exhaustion."""
    try:
        issues.append(
            Issue(
                "owl.axiom_change_unverified"
                if isinstance(error, ResourceError | OSError)
                else "owl.axiom_change_input_error",
                str(error),
                location,
                kind="unverified"
                if isinstance(error, ResourceError | OSError)
                else "input_error",
            )
        )
    except LimitExceeded as stopped:
        issues.exhausted(str(stopped), location=location)


def compare_axioms(
    backend: OwlProfileBackend,
    before: OwlDocument,
    after: OwlDocument,
    *,
    imports: Mapping[str, OwlDocument] | None = None,
    after_imports: Mapping[str, OwlDocument] | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> AxiomPatch:
    """Compare direct root axiom sets using maintained structural identity.

    Headers, ontology annotations and import declarations must match. Imported
    axioms are never presented as root edits. Unsupported changed axioms prevent
    a complete typed delta; unchanged unsupported axioms need no reconstruction.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    decoder = ProjectionDecoder(operation)
    additions: tuple[ProjectedAxiom, ...] = ()
    removals: tuple[ProjectedAxiom, ...] = ()
    capability = None
    complete = False
    base = None
    try:
        old, new = _owned(before, operation), _owned(after, operation)
        base = _base(old)
        captured_imports = _imports(imports, operation)
        captured_after = _imports(after_imports, operation)
        qualify_backend(backend, operation)
        backend.artifacts.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(
            prefix="axiom-comparison-", dir=backend.artifacts
        ) as job:
            directory = Path(job)
            (directory / "before").mkdir()
            (directory / "after").mkdir()
            request = _stage(old, captured_imports, directory / "before", operation)
            second = _stage(new, captured_after, directory / "after", operation)
            request = dict(
                request,
                before=request,
                after=second,
                operation="axiom_changes",
                action="compare",
            )
            data = invoke_worker(backend, request, directory, operation, heap_mb)
            capability = qualify_capability(data, backend, operation)
            _losses(data.get("changes"), decoder, issues)
            if data.get("header_compatible") is False:
                issues.append(
                    Issue(
                        "owl.axiom_header_mismatch",
                        "Axiom changes cannot represent changed ontology headers",
                        kind="unverified",
                    )
                )
            elif data.get("qualified") is not True:
                issues.append(
                    Issue(
                        "owl.axiom_change_unverified",
                        "Native structural comparison did not complete",
                        kind="unverified",
                    )
                )
            else:
                additions = _delta(data.get("additions"), decoder, issues)
                removals = _delta(data.get("removals"), decoder, issues)
                complete = not issues
            if data["status"] == "exhausted":
                raise LimitExceeded("Native axiom comparison exhausted its allowance")
    except LimitExceeded as error:
        issues.exhausted(str(error), location="owl.axiom_comparison")
    except (InputError, OSError) as error:
        _error(issues, error, "owl.axiom_comparison")
    return AxiomPatch(
        base,
        additions,
        removals,
        capability,
        Report(
            "owl.axiom_comparison",
            int(complete),
            tuple(issues),
            requested=1,
            complete=complete,
            regime="owlapi-direct-root-annotated-axiom-sets",
        ),
    )


def _checks(
    values: Sequence[AxiomCheck],
    source: OwlDocument,
    operation: Operation,
    issues: IssueCollector,
    retained: list[Report],
    *,
    required: bool,
) -> None:
    """Each trusted callback sees independent bytes and the actual shared budget."""
    selected = capture_items(values, AxiomCheck, operation)
    if required and not selected:
        issues.append(
            Issue(
                "owl.axiom_postconditions_missing",
                "Checked application acceptance requires selected after postconditions",
                kind="unverified",
            )
        )
    names = set()
    for selected_check in selected:
        bound_text(selected_check.name, operation)
        if (
            not selected_check.name
            or selected_check.name in names
            or not callable(selected_check.check)
        ):
            raise InputError("Axiom check names must be distinct and callable")
        operation.consume("states", 3)
        operation.consume("bytes", 128)
        names.add(selected_check.name)
        checked = selected_check.check(_owned(source, operation), operation)
        if not isinstance(checked, Report):
            raise InputError("Axiom checks must return ordinary Reports")
        try:
            retain_report(checked, operation, issues)
        except LimitExceeded:
            issues.retain_completed(checked)
            retained.append(checked)
            raise
        retained.append(checked)
        operation.consume("work")
        if required and not checked.checked:
            issues.append(
                Issue(
                    "owl.axiom_postcondition_unverified",
                    "An after postcondition completed no actual comparison",
                    selected_check.name,
                    kind="unverified",
                )
            )


def prepare_axiom_change(
    backend: OwlProfileBackend,
    source: OwlDocument,
    *,
    expected: AxiomBase,
    additions: Sequence[ProjectedAxiom] = (),
    removals: Sequence[ProjectedAxiom] = (),
    imports: Mapping[str, OwlDocument] | None = None,
    profiles: Sequence[OwlProfile] = (),
    before_checks: Sequence[AxiomCheck] = (),
    after_checks: Sequence[AxiomCheck] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> AxiomCandidate:
    """Apply explicit supported root edits with checked native output and contracts.

    Exact raw hash, syntax, resolution identities and removals are preconditions.
    Imports remain read-only. Syntax/profile observations never claim logical
    consistency. Caller checks may use the ordinary maintained entailment and
    consistency APIs with the same budget; baseline findings stay separate from
    acceptance. A candidate is inspectable even when its after checks fail.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    baseline = IssueCollector(operation)
    decoder = ProjectionDecoder(operation)
    added: tuple[ProjectedAxiom, ...] = ()
    removed: tuple[ProjectedAxiom, ...] = ()
    observations: tuple[NativeProfile, ...] = ()
    before: list[Report] = []
    after: list[Report] = []
    capability = None
    candidate = original = None
    finished = False
    try:
        original = _owned(source, operation)
        if _expected(expected, operation) != _base(original):
            issues.append(
                Issue(
                    "owl.axiom_base_mismatch",
                    "Original bytes or interpretation context "
                    "differ from the required base",
                )
            )
        else:
            captured_imports = _imports(imports, operation)
            encoder = ConstructionEncoder(operation)
            added, add_wire = encoder.axioms(additions)
            removed, remove_wire = encoder.axioms(removals)
            selected = capture_items(profiles, str, operation)
            if len(set(selected)) != len(selected) or any(
                value not in PROFILES for value in selected
            ):
                raise InputError("Choose distinct supported OWL profiles")
            _checks(
                before_checks, original, operation, baseline, before, required=False
            )
            qualify_backend(backend, operation)
            backend.artifacts.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(
                prefix="axiom-change-", dir=backend.artifacts
            ) as job:
                directory = Path(job)
                request = _stage(original, captured_imports, directory, operation)
                request.update(
                    operation="axiom_changes",
                    action="apply",
                    additions=add_wire,
                    removals=remove_wire,
                    profiles=selected,
                )
                data = invoke_worker(backend, request, directory, operation, heap_mb)
                capability = qualify_capability(data, backend, operation)
                _losses(data.get("changes"), decoder, issues)
                if data.get("preconditions") is False:
                    issues.append(
                        Issue(
                            "owl.axiom_precondition_failed",
                            "A removal is absent or an addition conflicts",
                        )
                    )
                elif data.get("qualified") is not True:
                    issues.append(
                        Issue(
                            "owl.axiom_change_unverified",
                            "Native construction or preservation did not complete",
                            kind="unverified",
                        )
                    )
                else:
                    added = _delta(data.get("additions"), decoder, issues)
                    removed = _delta(data.get("removals"), decoder, issues)
                    observations = _profiles(
                        data.get("after_profiles"),
                        cast(tuple[OwlProfile, ...], selected),
                        operation,
                    )
                    for observation in observations:
                        if not observation.in_profile:
                            issues.append(
                                Issue(
                                    "owl.axiom_profile_violation",
                                    "Candidate violates a selected OWL profile",
                                    actual=observation.profile,
                                )
                            )
                    size = result_counter(
                        data.get("bytes"), operation, operation.limits.max_bytes
                    )
                    operation.consume("bytes", size)
                    path = directory / "candidate.owl"
                    if (
                        path.is_symlink()
                        or not path.is_file()
                        or path.stat().st_size != size
                    ):
                        raise ResourceError(
                            "Native candidate byte receipt is unavailable"
                        )
                    content = path.read_bytes()
                    candidate = prepare_owl_document(
                        content,
                        format="functional",
                        document_iri=original.document_iri,
                        effective_url=original.effective_url,
                        budget=operation,
                    )
                    _checks(
                        after_checks, candidate, operation, issues, after, required=True
                    )
                    finished = True
                if data["status"] == "exhausted":
                    raise LimitExceeded("Native axiom change exhausted its allowance")
    except LimitExceeded as error:
        issues.exhausted(str(error), location="owl.axiom_change")
    except (InputError, OSError) as error:
        _error(issues, error, "owl.axiom_change")
    if original is None:
        original = source
    report = Report(
        "owl.axiom_change",
        int(finished),
        tuple(issues),
        requested=1,
        complete=finished,
        children=tuple(after),
        regime="checked-annotated-native-root-axiom-change",
    )
    return AxiomCandidate(
        candidate,
        original,
        added,
        removed,
        observations,
        tuple(before),
        tuple(after),
        capability,
        report,
        receipt(candidate) if candidate is not None else None,
        candidate,
    )
