"""Inspect effective context definitions through the maintained processing adapter."""

from collections.abc import Sequence

from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Location, Report
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.jsonld.contexts.preparation import (
    capture_context_json,
    capture_selections,
    context_budget,
    context_data,
    encode_context_json,
)
from bijux_ontology.jsonld.processing.contexts.state import (
    ContextProcessor,
    UnsupportedContext,
)
from bijux_ontology.jsonld.processing.resources import ContextResources, DocumentLoader
from bijux_ontology.jsonld.processing.runtime import processor
from bijux_ontology.rdf.identifiers import absolute_iri

from .types import ContextInspection, ContextScope, ContextView


def inspect_context(
    context: object,
    *,
    processing_mode: str = "json-ld-1.1",
    base: str | None = None,
    inherited_context: object | Absent = ABSENT,
    scopes: Sequence[ContextScope] = (),
    document_loader: DocumentLoader | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ContextInspection:
    """Inspect finite effective definitions, without proving document compatibility.

    Root processing starts from the initial context when inheritance is omitted.
    Selected scopes require an explicitly declared parent (None asserts initial).
    Unselected scoped combinations remain visible raw metadata, not instantiations.
    """
    processor()
    operation = context_budget(
        limits,
        budget
        if budget is not None
        else document_loader.budget
        if isinstance(document_loader, ContextResources)
        else None,
    )
    issues = IssueCollector(operation)
    root = None
    selected: tuple[ContextScope, ...] = ()
    scoped: list[tuple[ContextScope, ContextView | None]] = []
    checked = 0
    requested = 1
    complete = False
    data_json = '{"view":null,"scopes":[]}'
    try:
        bound_text(processing_mode, operation)
        if processing_mode not in ("json-ld-1.0", "json-ld-1.1"):
            raise InputError("Select JSON-LD 1.0 or 1.1 processing mode")
        if base is not None:
            bound_text(base, operation)
            absolute_iri(base)
        selected = capture_selections(scopes, operation)
        requested += len(selected)
        seen = set()
        for item in selected:
            if not isinstance(item, ContextScope):
                raise InputError("Expected typed ContextScope selections")
            bound_text(item.term, operation)
            bound_text(item.kind, operation)
            if item.kind not in ("property", "type") or (item.term, item.kind) in seen:
                raise InputError("Context scope selections must be distinct and typed")
            seen.add((item.term, item.kind))
        local = capture_context_json(context, operation)
        inherited = (
            capture_context_json(inherited_context, operation)
            if inherited_context is not ABSENT
            else ABSENT
        )
        bridge = ContextProcessor(document_loader, base, processing_mode, operation)
        active = bridge.initial()
        if inherited is not ABSENT:
            active = bridge.apply(active, inherited)
        active = bridge.apply(active, local)
        root = bridge.view(active)
        checked = 1
        for item in selected:
            operation.consume("work")
            if inherited is ABSENT:
                scoped.append((item, None))
                issues.append(
                    Issue(
                        "context.definition_unverified",
                        "Selected scope requires an explicit inherited parent",
                        kind="unverified",
                    )
                )
                continue
            transitioned = bridge.selected_scope(active, item)
            if transitioned is None:
                scoped.append((item, None))
                issues.append(
                    Issue(
                        "context.definition_unverified",
                        "Selected term has no established scoped context",
                        kind="unverified",
                    )
                )
                continue
            scoped.append((item, bridge.view(transitioned)))
            checked += 1
        data_json = encode_context_json(
            {
                "view": context_data(root, operation),
                "scopes": [
                    {
                        "selection": context_data(item, operation),
                        "view": context_data(view, operation),
                    }
                    for item, view in scoped
                ],
            },
            operation,
        )
        complete = checked == requested
    except (LimitExceeded, MemoryError, RecursionError):
        root = None
        scoped = []
        issues.exhausted("Processed context inspection exceeded its limits")
    except UnsupportedContext as error:
        root = None
        scoped = []
        try:
            issues.append(
                Issue(
                    "context.definition_unverified",
                    "Processor state or context scope is not qualified: " + str(error),
                    kind="unverified",
                )
            )
        except LimitExceeded:
            issues.exhausted("Context diagnostic allowance exhausted")
    except InputError as error:
        root = None
        scoped = []
        try:
            code = getattr(error, "processor_code", None)
            if isinstance(code, str):
                bound_text(code, operation)
            else:
                code = None
            retain_report(
                Report(
                    "context.definitions",
                    issues=(
                        Issue(
                            "context.input_error",
                            "Context processing rejected the supplied input",
                            kind="input_error",
                            actual=code,
                            details=Location.from_error(error),
                        ),
                    ),
                ),
                operation,
                issues,
            )
        except LimitExceeded:
            issues.exhausted("Context diagnostic allowance exhausted")
    return ContextInspection(
        root,
        tuple(scoped),
        Report(
            "context.definitions",
            checked,
            tuple(issues),
            requested=requested,
            complete=complete,
        ),
        data_json,
    )
