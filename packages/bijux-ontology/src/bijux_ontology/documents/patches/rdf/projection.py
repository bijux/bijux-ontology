"""Checked RDF-to-compact-source projection through existing native boundaries."""

from collections.abc import Sequence
from itertools import islice

from rdflib import BNode, Literal, Variable

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.documents.forms.types import FormRequirement
from bijux_ontology.documents.patches.checking import apply_document_patch
from bijux_ontology.documents.patches.operations import (
    _Precondition,
    _Unsupported,
    declarations,
    loader_resources,
)
from bijux_ontology.documents.patches.types import DocumentPatch, RetainedUsage
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.json.snapshots import serialized, snapshot
from bijux_ontology.json.values import _measure
from bijux_ontology.jsonld.processing.conversion import DirectionPolicy
from bijux_ontology.jsonld.processing.preparation import prepare_jsonld
from bijux_ontology.jsonld.processing.resources import ContextResources, DocumentLoader
from bijux_ontology.jsonld.processing.runtime import processor
from bijux_ontology.jsonld.usages.checking import prepare_usages
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.patches.applying import apply_rdf_patch
from bijux_ontology.rdf.patches.codecs import patch_from_data, patch_to_data
from bijux_ontology.rdf.patches.types import DatasetPatch

from .binding import AmbiguousSourceEdit, capture_bindings, project_operations
from .types import RDFSourcePatch, SourceEditBinding


class _Stopped(Exception):
    """A returned actual native report already explains why projection cannot finish."""


def project_rdf_patch(
    document: object,
    patch: DatasetPatch,
    bindings: Sequence[SourceEditBinding],
    *,
    expected_base: object,
    before_requirements: Sequence[FormRequirement],
    after_requirements: Sequence[FormRequirement],
    retained: Sequence[RetainedUsage],
    base: str,
    document_loader: DocumentLoader | None = None,
    rdf_direction: DirectionPolicy | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> RDFSourcePatch:
    """Project explicit paired ground edits without recompacting the source.

    Every removal/addition must have one declared pairing. Actual maintained
    source observations establish candidate occurrences; duplicate aliases need
    an explicit all/selected-path policy. Typed lexical strings preserve datatype
    and language; pure named forward/reverse references preserve their owner.
    Edited blanks, graph inventory changes, lists and unsupported correspondence
    remain unknown. Whole RDF re-expansion must equal the actual requested patch
    target before accepted source bytes, operations or export capability appear.
    """
    if document_loader is not None and not callable(document_loader):
        raise InputError("RDF source projection requires a callable document loader")
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("RDF source projection requires Limits")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("RDF source projection requires Budget")
    inherited = (
        document_loader.budget
        if isinstance(document_loader, ContextResources)
        else None
    )
    operation = (
        budget
        if budget is not None
        else inherited
        if inherited is not None
        else Budget(limits)
    )
    if limits is not None and limits != operation.limits:
        raise InputError("RDF source projection limits conflict with the shared Budget")
    if inherited is not None and inherited is not operation:
        raise InputError("RDF source resources require their originating shared Budget")
    issues = IssueCollector(operation)
    children: list[Report] = []
    checked = 0
    output: DocumentPatch | None = None
    operations_json = None
    qualified: tuple[SourceEditBinding, ...] = ()
    prior: frozenset[int] = frozenset()
    handoff = min(operation.limits.max_states, operation.limits.max_issues)

    def accept(report: Report) -> None:
        """Retain native report metadata and paid findings before attaching a child."""
        operation.consume("states")
        operation.consume("bytes", 64)
        retain_report(report, operation, issues)
        children.append(report)

    try:
        operation.consume("states", 16 + len(operation._retained_issues))
        operation.consume(
            "bytes", 2048 + 64 * len(operation._retained_issues) + 8 * handoff
        )
        prior = frozenset(operation._retained_issues)
        if isinstance(base, (BNode, Literal, Variable)):
            raise InputError("RDF source projection base requires named IRI text")
        bound_text(base, operation)
        absolute_iri(base)
        base = str(base)
        if rdf_direction not in (None, "i18n-datatype", "compound-literal"):
            raise InputError("Choose an explicit supported RDF direction policy")
        original, original_json = snapshot(document, operation)
        _, expected_json = snapshot(expected_base, operation)
        if original_json != expected_json:
            raise _Precondition("The supplied source differs from expected_base")
        owned_patch = patch_from_data(
            patch_to_data(patch, budget=operation), budget=operation
        )
        selected = capture_bindings(bindings, owned_patch, operation)
        before_forms = declarations(before_requirements, FormRequirement, operation)
        after_forms = declarations(after_requirements, FormRequirement, operation)
        preserved = declarations(retained, RetainedUsage, operation)
        if not before_forms or not after_forms or not preserved:
            raise _Unsupported(
                "Source projection needs explicit form and retained-use contracts"
            )
        loader = loader_resources(document_loader, operation)
        processor()
        prepared = prepare_jsonld(
            original,
            base=base,
            document_loader=loader,
            direction=rdf_direction,
            budget=operation,
        )
        accept(prepared.report)
        if prepared.source is None or not prepared.report.verified:
            raise _Stopped
        applied = apply_rdf_patch(prepared.source, owned_patch, budget=operation)
        accept(applied.report)
        checked = 1
        if applied.source is None or not applied.report.verified:
            raise _Stopped
        # Existing preparation owns several defensive source copies. Reserve
        # their JSON traversal/storage before invoking that concrete boundary.
        cost = _measure(original, operation)
        operation.consume("bytes", 128 * (cost + 1))
        usages = prepare_usages(
            original, base=base, document_loader=loader, budget=operation
        )
        edits, expectations, qualified = project_operations(
            original, usages, owned_patch, selected, operation, issues
        )
        candidate_operations = serialized(list(edits), operation)
        checked = 2
        candidate = apply_document_patch(
            original,
            edits,
            expected_base=original,
            before_requirements=before_forms,
            after_requirements=after_forms,
            after_expectations=expectations,
            retained=preserved,
            base=base,
            document_loader=loader,
            rdf_direction=rdf_direction,
            budget=operation,
        )
        accept(candidate.report)
        checked = 3
        if (
            not candidate.report.verified
            or candidate.before_form is None
            or candidate.after_form is None
            or candidate.before_form.source is None
            or candidate.after_form.source is None
        ):
            raise _Stopped
        # T132's returned snapshots are the actual before/after conversion under
        # this same resource owner. Check that its before source still matches
        # the already qualified original, then the entire requested RDF target.
        same_before = compare_rdf(
            prepared.source, candidate.before_form.source, budget=operation
        )
        accept(same_before)
        if not same_before.verified:
            raise _Stopped
        postcondition = compare_rdf(
            applied.source, candidate.after_form.source, budget=operation
        )
        accept(postcondition)
        checked = 4
        if postcondition.verified and all(child.verified for child in children):
            output = candidate
            operations_json = candidate_operations
    except (LimitExceeded, MemoryError, RecursionError) as error:
        issues.retain_completed(
            Report(
                "documents.source_edit_paid",
                1,
                tuple(
                    issue
                    for issue in islice(operation._retained_issues.values(), handoff)
                    if id(issue) not in prior
                ),
            )
        )
        issues.exhausted(str(error) or "Source projection exceeded its allowance")
    except _Stopped:
        pass
    except (InputError, ResourceError) as error:
        try:
            precondition = isinstance(error, _Precondition)
            if precondition:
                checked = max(checked, 1)
            ambiguous = isinstance(error, AmbiguousSourceEdit)
            unsupported = isinstance(error, (_Unsupported, ResourceError))
            issues.append(
                Issue(
                    "documents.source_edit_base_failed"
                    if precondition
                    else "documents.source_edit_ambiguous"
                    if ambiguous
                    else "documents.source_edit_unverified"
                    if unsupported
                    else "documents.source_edit_invalid",
                    str(error),
                    kind="finding"
                    if precondition
                    else "unverified"
                    if unsupported
                    else "input_error",
                )
            )
        except LimitExceeded as stopped:
            issues.exhausted(str(stopped))
    report = Report(
        "documents.rdf_source_patch",
        checked,
        tuple(issues),
        requested=4,
        complete=checked == 4 and all(child.complete for child in children),
        children=tuple(children),
    )
    return RDFSourcePatch._from_owned(
        report,
        output if report.verified else None,
        operations_json if report.verified else None,
        qualified,
    )
