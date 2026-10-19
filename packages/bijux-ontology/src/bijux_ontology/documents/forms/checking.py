"""Check selected JSON-LD source forms beside maintained semantic qualification."""

from collections.abc import Sequence
from dataclasses import replace
from typing import TypeVar, cast

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report, combine_reports
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.json.paths import (
    SourcePath,
    at_path,
    bound_pointer_text,
    bounded_location,
)
from bijux_ontology.jsonld.evidence.entities import source_entity
from bijux_ontology.jsonld.expectations import ContextExpectation
from bijux_ontology.jsonld.processing.conversion import DirectionPolicy
from bijux_ontology.jsonld.processing.preparation import prepare_jsonld
from bijux_ontology.jsonld.processing.resources import DocumentLoader
from bijux_ontology.jsonld.selectors.checking import resolve_selection
from bijux_ontology.jsonld.selectors.types import SourceSelector
from bijux_ontology.jsonld.usages.checking import PreparedUsages, prepare_usages
from bijux_ontology.rdf.sources.snapshots import PreparedSource

from .types import DocumentFormCheck, FormRequirement

T = TypeVar("T")


def _capture(values: Sequence[T], budget: Budget, kind: type[T]) -> tuple[T, ...]:
    """Freeze required finite inventory before any checking can consume its tail."""
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        raise InputError("Form contracts require finite sequences")
    count = len(values)
    budget.consume("states", count)
    budget.consume("bytes", 128 + 64 * count)
    captured: list[T] = []
    for index in range(count):
        budget.consume("work")
        try:
            item = values[index]
        except IndexError as error:
            raise InputError(
                "Form contract inventory changed during capture"
            ) from error
        if not isinstance(item, kind):
            raise InputError("Unexpected form contract inventory item")
        captured.append(item)
    return tuple(captured)


def _result(
    form: Report, semantic: Report, *, source: PreparedSource | None = None
) -> DocumentFormCheck:
    """Aggregate both required scopes without hiding incomplete empty reports."""
    combined = combine_reports("documents.form_check", form, semantic)
    return DocumentFormCheck(
        form,
        semantic,
        replace(combined, complete=form.complete and semantic.complete),
        source=source,
    )


def _failure(error: InputError, scope: str) -> Report:
    """Retain expected input or bounded failures in their actual operation scope."""
    return Report(
        scope,
        issues=(
            Issue(
                "execution.limit_exceeded"
                if isinstance(error, LimitExceeded)
                else "context.input_error",
                str(error),
                error.location,
                kind="exhausted" if isinstance(error, LimitExceeded) else "input_error",
            ),
        ),
        requested=1,
        complete=False,
    )


def _where(path: SourcePath, budget: Budget) -> str:
    """Charge escaped diagnostic pointer text before materializing it."""
    return bounded_location(path, budget)


def _form(
    prepared: PreparedUsages,
    requirements: tuple[FormRequirement, ...],
    budget: Budget,
) -> Report:
    """Reuse original source selection and associations; impose no default schema."""
    issues = IssueCollector(budget)
    checked = 0
    complete = True
    try:
        for requirement in requirements:
            budget.consume("work")
            for key in requirement.required_keys:
                bound_pointer_text(key, budget)
            if requirement.allowed_keys is not None:
                for key in requirement.allowed_keys:
                    bound_pointer_text(key, budget)
            selected = (
                resolve_selection(
                    requirement.selector,
                    prepared._document,
                    prepared.entities,
                    prepared.observe,
                    budget,
                    require_usage=False,
                )
                if isinstance(requirement.selector, SourceSelector)
                else prepared.select(requirement.selector)
            )
            list.extend(issues, selected.report.issues)
            complete &= selected.report.complete
            allowed = requirement.allowed_keys
            if allowed is not None:
                budget.consume("work", sum(len(key) + 1 for key in allowed))
                budget.consume("bytes", 128 + 64 * len(allowed))
            allowed_set = None if allowed is None else frozenset(allowed)
            for path in selected.paths:
                budget.consume("work", len(path) + 1)
                value = at_path(prepared._document, path)
                shape = (
                    "object"
                    if isinstance(value, dict)
                    else "array"
                    if isinstance(value, list)
                    else "scalar"
                )
                if requirement.shape is not None and requirement.shape != shape:
                    issues.append(
                        Issue(
                            "documents.form_shape",
                            "Selected source has the wrong shape",
                            _where(path, budget),
                            expected=requirement.shape,
                            actual=shape,
                        )
                    )
                if requirement.required_keys or allowed_set is not None:
                    if not isinstance(value, dict):
                        issues.append(
                            Issue(
                                "documents.form_shape",
                                "Object keys require an object",
                                _where(path, budget),
                                expected="object",
                                actual=shape,
                            )
                        )
                    else:
                        for key in requirement.required_keys:
                            budget.consume("work", len(key) + 1)
                            if key not in value:
                                issues.append(
                                    Issue(
                                        "documents.form_key_missing",
                                        "Required exact source key is absent",
                                        _where((*path, key), budget),
                                        expected=key,
                                    )
                                )
                        if allowed_set is not None:
                            for key in value:
                                budget.consume("work", len(key) + 1)
                                if key not in allowed_set:
                                    issues.append(
                                        Issue(
                                            "documents.form_key_unexpected",
                                            "Source key is not permitted",
                                            _where((*path, key), budget),
                                            actual=key,
                                        )
                                    )
                if requirement.embedding is not None:
                    _embedding(
                        prepared, path, value, requirement.embedding, budget, issues
                    )
            checked += 1
    except LimitExceeded as error:
        issues.exhausted(str(error))
        complete = False
    return Report(
        "documents.form",
        checked,
        tuple(issues),
        requested=len(requirements),
        complete=complete,
        regime="source_form",
        families=("source_form",),
    )


def _embedding(
    prepared: PreparedUsages,
    path: SourcePath,
    value: object,
    expected: str,
    budget: Budget,
    issues: IssueCollector,
) -> None:
    """Keep canonical references distinct from actual describing occurrences."""
    budget.consume("work")
    if expected == "reference":
        if (
            isinstance(value, dict)
            and len(value) == 1
            and isinstance(value.get("@id"), str)
        ):
            return
    elif isinstance(value, dict):
        try:
            binding = source_entity(
                cast(dict[str, object], value), path, prepared.observe, budget
            )
        except LimitExceeded:
            raise
        except InputError:
            binding = None
        if binding is None:
            issues.append(
                Issue(
                    "documents.form_binding_unverified",
                    "Inline resource description has no established source binding",
                    _where(path, budget),
                    kind="unverified",
                )
            )
            return
        if binding.described:
            return
    issues.append(
        Issue(
            "documents.form_embedding",
            "Selected occurrence violates its embedding requirement",
            _where(path, budget),
            expected=expected,
        )
    )


def check_document_form(
    document: object | PreparedUsages,
    requirements: Sequence[FormRequirement],
    *,
    expectations: Sequence[ContextExpectation] = (),
    base: str | None = None,
    document_loader: DocumentLoader | None = None,
    rdf_direction: DirectionPolicy | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> DocumentFormCheck:
    """Check owned source syntax and selected meanings with independent raw reports.

    Reusing PreparedUsages pins its snapshot, resources, base and shared budget.
    Preparations with applied omissions cannot witness original document form.
    Semantic qualification always includes maintained JSON-LD-to-RDF conversion;
    explicit expectations additionally check the caller's intended term meanings.
    Shape alone implies neither RDF equality nor supplied ontology agreement.
    """
    if rdf_direction not in (None, "i18n-datatype", "compound-literal"):
        raise InputError("Unknown RDF direction policy")
    if (
        limits is not None
        and not isinstance(limits, Limits)
        or budget is not None
        and not isinstance(budget, Budget)
    ):
        raise InputError("Expected Limits and optional Budget")
    if isinstance(document, PreparedUsages):
        if document_loader is not None or base is not None and base != document._base:
            raise InputError("Prepared source form pins its base and resources")
        operation = document._budget
        if (
            budget is not None
            and budget is not operation
            or limits is not None
            and limits != operation.limits
        ):
            raise InputError("Prepared source form pins its shared budget and limits")
        if document.allowed_omissions:
            raise InputError("Applied source omissions cannot witness original form")
    else:
        if limits is not None and budget is not None and limits != budget.limits:
            raise InputError("limits conflict with shared Budget")
        operation = budget if budget is not None else Budget(limits)
    try:
        contracts = _capture(requirements, operation, FormRequirement)
        semantic_contracts = _capture(expectations, operation, ContextExpectation)
        if not contracts:
            raise InputError("At least one source form requirement is required")
        prepared = (
            document
            if isinstance(document, PreparedUsages)
            else prepare_usages(
                document, base=base, document_loader=document_loader, budget=operation
            )
        )
    except LimitExceeded as error:
        return _result(
            _failure(error, "documents.form"),
            _failure(error, "documents.form_semantics"),
        )
    except InputError as error:
        return _result(
            Report(
                "documents.form",
                issues=(
                    Issue(
                        "documents.form_scope_unverified",
                        "No prepared source is available for form checking",
                        kind="unverified",
                    ),
                ),
                requested=1,
                complete=False,
            ),
            _failure(error, "documents.form_semantics"),
        )
    form = _form(prepared, contracts, operation)
    semantics = prepared.check(semantic_contracts, allow_empty=True)
    converted = prepare_jsonld(
        prepared._document,
        base=prepared._base,
        document_loader=prepared._loader,
        allowed_drops=prepared.allowed_drops,
        direction=rdf_direction,
        budget=operation,
    )
    return _result(
        form,
        combine_reports("documents.form_semantics", semantics, converted.report),
        source=converted.source,
    )
