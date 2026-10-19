"""Ordered standards processing with every selected module contract retained."""

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal, cast

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.jsonld.contexts.processing import (
    _capture,
    _data,
    _dump,
    _operation,
    _own,
    _preflight,
    inspect_context,
)
from bijux_ontology.jsonld.contracts.expectations import ContextExpectation
from bijux_ontology.jsonld.processing.resources import (
    ContextResources,
    DocumentLoader,
    context_resources,
)
from bijux_ontology.jsonld.processing.runtime import processor
from bijux_ontology.jsonld.usages.checking import check_context
from bijux_ontology.rdf.identifiers import absolute_iri

from .types import ContextCase, ContextCaseResult, ContextComposition, ContextModule


@dataclass(frozen=True)
class _Case:
    """Operation-owned immutable representative input and typed expectations."""

    name: str
    document_json: str
    expectations: tuple[ContextExpectation, ...]


def _case(case: ContextCase, budget: Budget) -> _Case:
    """Qualify every field before copying, hashing or invoking a resource loader."""
    if not isinstance(case, ContextCase):
        raise InputError("Retained contracts require typed ContextCase records")
    bound_text(case.name, budget)
    if not case.name:
        raise InputError("Retained cases require nonempty names")
    document = _own(case.document, budget)
    if not isinstance(document, dict) or "@context" in document:
        raise InputError("Retained case roots must be objects without @context")
    expectations = _capture(case.expectations, budget)
    if not expectations:
        raise InputError("Each retained case requires actual usage expectations")
    captured_expectations = []
    for expectation in expectations:
        if not isinstance(expectation, ContextExpectation):
            raise InputError("Expected typed ContextExpectation obligations")
        _data(expectation, budget)
        budget.consume("states")
        budget.consume("bytes", 64)
        captured_expectations.append(replace(expectation))
    return _Case(str(case.name), _dump(document, budget), tuple(captured_expectations))


def _document(case: _Case, context: object, budget: Budget) -> dict[str, object]:
    """Bind the selected context to the same bounded representative object."""
    document = json.loads(case.document_json)
    budget.consume("states")
    budget.consume("bytes", 32)
    document["@context"] = context
    return cast(dict[str, object], document)


def compose_contexts(
    modules: Sequence[ContextModule],
    *,
    resolution: Literal["retain-required"],
    base: str | None = None,
    document_loader: DocumentLoader | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ContextComposition:
    """Compose an exact ordered context array, retaining every module's cases.

    The only supported resolution policy preserves all required obligations.
    Later definitions use actual JSON-LD context-array semantics; they do not
    discard prior contracts. Each case checks independently and in the composite.
    A failing or unknown baseline cannot establish compatibility. Validity and
    compatibility concern the declared representative scope, never all documents.
    Nested/scoped/null contexts are processed by the maintained implementation;
    unsupported occurrence association stays unknown. No dictionary merge occurs.
    """
    operation = _operation(
        limits,
        budget
        if budget is not None
        else document_loader.budget
        if isinstance(document_loader, ContextResources)
        else None,
    )
    issues = IssueCollector(operation)
    inspection = None
    order: list[str] = []
    outcomes: list[ContextCaseResult] = []
    children: list[Report] = []
    contexts: list[object] = []
    captured: list[tuple[int, _Case, int]] = []
    candidate = None
    checked = 0
    requested = 1
    complete = False
    try:
        bound_text(resolution, operation)
        if resolution != "retain-required":
            raise InputError("Select the retain-required context resolution policy")
        if base is not None:
            bound_text(base, operation)
            absolute_iri(base)
        selected = _capture(modules, operation)
        if not selected:
            raise InputError("Context composition requires selected modules")
        seen: set[str] = set()
        for module in selected:
            operation.consume("work")
            if not isinstance(module, ContextModule):
                raise InputError("Composition requires typed ContextModule records")
            bound_text(module.name, operation)
            name = str(module.name)
            if not name or name in seen:
                raise InputError("Selected module names must be nonempty and unique")
            seen.add(name)
            context = _own(module.context, operation)
            cases = _capture(module.cases, operation)
            if not cases:
                raise InputError("Every context module requires retained cases")
            operation.consume("states")
            operation.consume("bytes", 32)
            index = len(contexts)
            contexts.append(context)
            order.append(name)
            case_names = set()
            for supplied_case in cases:
                owned = _case(supplied_case, operation)
                if owned.name in case_names:
                    raise InputError("Retained case names must be unique per module")
                case_names.add(owned.name)
                operation.consume("states", 3)
                operation.consume("bytes", 128)
                slot = len(outcomes)
                outcomes.append(ContextCaseResult(name, owned.name))
                captured.append((index, owned, slot))
        requested += 2 * len(captured)
        # All mutable caller JSON and selected metadata are captured before this
        # point. One resource owner pins each retrieved response for the operation.
        processor()
        if document_loader is None or isinstance(document_loader, ContextResources):
            resources = context_resources(document_loader, budget=operation)
        else:

            def qualified(url: str, options: dict[str, object]) -> dict[str, object]:
                """Qualify callback metadata before copying or retaining errors."""
                try:
                    response = document_loader(url, options)
                except InputError as error:
                    bound_text(str(error), operation)
                    raise
                _preflight(response, operation)
                return response

            resources = context_resources(qualified, budget=operation)
        for index, case, slot in captured:
            result = check_context(
                _document(case, contexts[index], operation),
                case.expectations,
                base=base,
                document_loader=resources,
                budget=operation,
            )
            outcomes[slot] = replace(outcomes[slot], independent=result)
            children.append(result)
            checked += 1
            retain_report(result, operation, issues)
        inspection = inspect_context(
            contexts, base=base, document_loader=resources, budget=operation
        )
        children.append(inspection.report)
        checked += 1
        retain_report(inspection.report, operation, issues)
        if inspection.report.verified:
            for _, case, slot in captured:
                result = check_context(
                    _document(case, contexts, operation),
                    case.expectations,
                    base=base,
                    document_loader=resources,
                    budget=operation,
                )
                outcomes[slot] = replace(outcomes[slot], composite=result)
                children.append(result)
                checked += 1
                retain_report(result, operation, issues)
            complete = checked == requested and all(
                child.complete for child in children
            )
            if complete and all(child.verified for child in children):
                candidate = _dump(contexts, operation)
    except (LimitExceeded, MemoryError, RecursionError):
        issues.exhausted("Context composition exceeded its operation limits")
    except InputError as error:
        try:
            issues.append(
                Issue(
                    "context.composition_unverified",
                    str(error),
                    kind="unverified"
                    if isinstance(error, ResourceError)
                    else "input_error",
                )
            )
        except LimitExceeded:
            issues.exhausted("Context composition diagnostic allowance exhausted")
    report = Report(
        "jsonld.composition",
        checked,
        tuple(issues),
        requested=requested,
        complete=complete,
        children=tuple(children),
        regime="ordered-retained-context-cases",
    )
    return ContextComposition(
        candidate if report.verified else None,
        tuple(order),
        tuple(outcomes),
        inspection,
        report,
    )
