"""Source patches checked against explicit base, meaning and form obligations."""

from collections.abc import Sequence
from typing import cast

from rdflib import BNode, Variable
from rdflib import Literal as RdfLiteral

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Location, Report
from bijux_ontology.documents.export import RDFExport
from bijux_ontology.documents.forms.checking import check_document_form
from bijux_ontology.documents.forms.types import DocumentFormCheck, FormRequirement
from bijux_ontology.execution.budgets import operation
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.json.paths import at_path, bound_path, bounded_location
from bijux_ontology.jsonld.checking import PreparedUsages, prepare_usages
from bijux_ontology.jsonld.comparison import compare_observed_usages
from bijux_ontology.jsonld.conversion import DirectionPolicy
from bijux_ontology.jsonld.expectations import ContextExpectation
from bijux_ontology.jsonld.observations import ObservedUsage
from bijux_ontology.jsonld.resources import ContextResources, DocumentLoader
from bijux_ontology.rdf.identifiers import absolute_iri

from .operations import (
    _Precondition,
    _Unsupported,
    apply_operations,
    declarations,
    loader_resources,
    prepare_operations,
    serialized,
    snapshot,
)
from .types import DocumentPatch, RetainedUsage


def _parent(usage: ObservedUsage, budget: Budget) -> str | None:
    """Establish one actual named parent, without comparing local blank labels."""
    budget.consume("work", len(usage.occurrences))
    depth = min((item.depth for item in usage.occurrences), default=None)
    parents: set[str] = set()
    for item in usage.occurrences:
        budget.consume("work")
        if item.depth != depth:
            continue
        if item.subject is None or item.subject.startswith("_:"):
            return None
        bound_text(item.subject, budget)
        absolute_iri(item.subject)
        budget.consume("states")
        parents.add(item.subject)
    return next(iter(parents)) if len(parents) == 1 else None


def _retained(
    original: object,
    edited: object,
    before: PreparedUsages,
    after: PreparedUsages,
    selected: tuple[RetainedUsage, ...],
    budget: Budget,
) -> Report:
    """Require raw values and actual selected relational meanings to be retained."""
    issues = IssueCollector(budget)
    pairs: list[tuple[str, ObservedUsage, ObservedUsage]] = []
    checked = 0
    compared = None
    try:
        for index, item in enumerate(selected):
            bound_path(item.before_path, budget)
            bound_path(item.after_path, budget)
            where = bounded_location(item.after_path, budget)
            try:
                left_value = at_path(original, item.before_path)
                right_value = at_path(edited, item.after_path)
            except (KeyError, IndexError):
                issues.append(
                    Issue(
                        "documents.patch_retained_missing",
                        "A required retained source occurrence is absent",
                        where,
                    )
                )
                checked += 1
                continue
            if serialized(left_value, budget) != serialized(right_value, budget):
                issues.append(
                    Issue(
                        "documents.patch_retained_changed",
                        "A retained source value changed",
                        where,
                    )
                )
            # Reserve returned observations/pair slots before invoking observation.
            budget.consume("states", 3)
            budget.consume("bytes", 192)
            left, right = (
                before.observe(item.before_path),
                after.observe(item.after_path),
            )
            positional = any(
                type(part) is int for part in (*item.before_path, *item.after_path)
            )
            if positional or item.before_path != item.after_path:
                parent = _parent(left, budget)
                if parent is None or _parent(right, budget) != parent:
                    issues.append(
                        Issue(
                            "documents.patch_rebinding_unverified",
                            "Retained rebinding requires the same named parent",
                            where,
                            kind="unverified",
                        )
                    )
            pairs.append((f"retained:{index}", left, right))
            checked += 1
        compared = compare_observed_usages(pairs, budget=budget)
        checked += 1
        retain_report(compared, budget, issues)
    except (LimitExceeded, MemoryError, RecursionError):
        issues.exhausted("Retained source-patch checks exceeded their allowance")
    return Report(
        "documents.patch_retained",
        checked,
        tuple(issues),
        requested=len(selected) + 1,
        complete=checked == len(selected) + 1
        and compared is not None
        and compared.complete,
        children=() if compared is None else (compared,),
    )


def apply_document_patch(
    document: object,
    operations: Sequence[object],
    *,
    expected_base: object,
    before_requirements: Sequence[FormRequirement],
    after_requirements: Sequence[FormRequirement],
    after_expectations: Sequence[ContextExpectation],
    retained: Sequence[RetainedUsage],
    before_expectations: Sequence[ContextExpectation] = (),
    base: str,
    document_loader: DocumentLoader | None = None,
    rdf_direction: DirectionPolicy | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> DocumentPatch:
    """Apply explicit add/remove/replace/test operations with checked source intent.

    The exact owned expected_base is mandatory. Object-name order is immaterial;
    array order and JSON numeric source kinds remain significant. Native RFC test
    semantics are separate: 1 and 1.0 compare equal, while true and 1 do not.
    Unsupported move/copy and unproven anonymous selector rebinding stay unknown.

    Nonempty before/after form, changed-meaning and retained-usage obligations are
    required. Retention combines exact raw values with actual expanded mappings,
    identities, containers and topology; no assertion of whole-document semantic
    preservation is made when the caller intentionally changes other fields.
    Explicit retained array paths require named-parent evidence on both sides.
    All caller declarations and input trees are captured before loader callbacks.
    Raw source-form reports retain their already converted RDF snapshots. Only
    verified aggregate evidence publishes exact immutable candidate JSON bytes.
    No destination is written; export_document_patch uses ordinary checked writes.
    """
    inherited = (
        document_loader.budget
        if isinstance(document_loader, ContextResources)
        else None
    )
    selected = operation(limits, budget if budget is not None else inherited)
    issues = IssueCollector(selected)
    before_form: DocumentFormCheck | None = None
    after_form: DocumentFormCheck | None = None
    retained_report = None
    children: list[Report] = []
    output = None
    checked = edits_checked = 0
    complete = False
    requested = 4
    try:
        if isinstance(cast(object, base), (BNode, RdfLiteral, Variable)):
            raise InputError("Source patch base requires named IRI text")
        bound_text(base, selected)
        absolute_iri(base)
        base = str(base)
        if rdf_direction not in (None, "i18n-datatype", "compound-literal"):
            raise InputError("Select a supported explicit RDF direction policy")
        original, original_json = snapshot(document, selected)
        _, expected_json = snapshot(expected_base, selected)
        edits = prepare_operations(operations, selected)
        before_forms = declarations(before_requirements, FormRequirement, selected)
        after_forms = declarations(after_requirements, FormRequirement, selected)
        before_expected = declarations(
            before_expectations, ContextExpectation, selected
        )
        after_expected = declarations(after_expectations, ContextExpectation, selected)
        preserved = declarations(retained, RetainedUsage, selected)
        if not before_forms or not after_forms or not after_expected or not preserved:
            raise _Unsupported(
                "Source patches require nonempty form, changed and retained obligations"
            )
        if original_json != expected_json:
            raise _Precondition("The supplied document differs from expected_base")
        edited, _ = snapshot(original, selected)
        for changed in apply_operations(edited, edits, selected):
            edited = changed
            edits_checked += 1
        candidate = serialized(edited, selected)
        checked = 1
        # Raw callback results and their enclosing reports own slots before work.
        selected.consume("states", 8)
        selected.consume("bytes", 512)
        loader = loader_resources(document_loader, selected)
        before = prepare_usages(
            original, base=base, document_loader=loader, budget=selected
        )
        before_form = check_document_form(
            before,
            before_forms,
            expectations=before_expected,
            rdf_direction=rdf_direction,
            budget=selected,
        )
        children.append(before_form.report)
        checked += 1
        retain_report(before_form.report, selected, issues)
        after = prepare_usages(
            edited, base=base, document_loader=loader, budget=selected
        )
        after_form = check_document_form(
            after,
            after_forms,
            expectations=after_expected,
            rdf_direction=rdf_direction,
            budget=selected,
        )
        children.append(after_form.report)
        checked += 1
        retain_report(after_form.report, selected, issues)
        retained_report = _retained(
            original, edited, before, after, preserved, selected
        )
        children.append(retained_report)
        checked += 1
        retain_report(retained_report, selected, issues)
        complete = checked == requested and all(child.complete for child in children)
        if (
            complete
            and all(child.verified for child in children)
            and before_form.source is not None
            and after_form.source is not None
        ):
            output = candidate
    except (LimitExceeded, MemoryError, RecursionError):
        issues.exhausted("Source patch preparation exceeded its operation allowance")
    except (InputError, ResourceError) as error:
        try:
            if isinstance(error, _Precondition):
                # A failed base/test is a completed precondition check; later
                # source/meaning stages remain explicitly pending.
                checked = 1
                issues.append(
                    Issue(
                        "documents.patch_precondition_failed",
                        "The exact base or a supplied JSON Patch test did not hold",
                    )
                )
            else:
                unknown = isinstance(error, (_Unsupported, ResourceError))
                issues.append(
                    Issue(
                        "documents.patch_unverified"
                        if unknown
                        else "documents.patch_invalid",
                        "Source patches require bijux-ontology[jsonpatch,jsonld], "
                        "supported edits and explicit source obligations",
                        kind="unverified" if unknown else "input_error",
                        details=Location.from_error(error),
                    )
                )
        except LimitExceeded:
            issues.exhausted("Source patch diagnostic allowance exhausted")
    report = Report(
        "documents.patch",
        checked,
        tuple(issues),
        requested=requested,
        complete=complete,
        children=tuple(children),
        regime="jsonld-source-patch",
    )
    return DocumentPatch._from_owned(
        document_json=output if report.verified else None,
        report=report,
        before_form=before_form,
        after_form=after_form,
        retained_report=retained_report,
        operations_checked=edits_checked,
    )


def export_document_patch(patched: DocumentPatch) -> RDFExport:
    """Bind exact accepted candidate bytes to the ordinary atomic write protocol."""
    if not isinstance(patched, DocumentPatch):
        raise InputError("Expected an owned DocumentPatch result")
    if not patched.report.verified or patched.document_json is None:
        return RDFExport(None, patched.report, "json-ld")
    return RDFExport(patched.document_json.encode("utf-8"), patched.report, "json-ld")
