"""Explicit language negotiation over one owned annotation selection."""

from collections.abc import Sequence
from typing import Literal, TypeVar, cast

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.limits import Budget, IssueCollector
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.browsing.results import Annotation, Selection

from .matching import _lookup, _matches, _range, _tag, _text

T = TypeVar("T")
Mode = Literal["lookup", "basic-filter", "extended-filter"]
Fallback = Literal["untagged", "other"]
_ROLES = ("local", "imports", "lookup")


def _capture(values: Sequence[T], budget: Budget) -> tuple[T, ...]:
    if not isinstance(values, Sequence) or isinstance(
        cast(object, values), (str, bytes)
    ):
        raise InputError("Annotation preferences require finite sequences")
    count = len(values)
    budget.consume("states", count)
    return tuple(values[index] for index in range(count))


def _role(scope: str) -> str:
    if scope == "local":
        return "local"
    return "imports" if scope.startswith("import[") else "lookup"


def _ordered(
    values: list[tuple[int, int, int, Annotation]], budget: Budget
) -> tuple[Annotation, ...]:
    budget.consume("work", len(values) * max(1, len(values).bit_length()))
    return tuple(item[3] for item in sorted(values, key=lambda item: item[:3]))


def negotiate_annotations(
    view: VocabularyView,
    iri: str,
    *,
    predicates: Sequence[str],
    ranges: Sequence[str],
    mode: Mode = "lookup",
    fallback: Sequence[Fallback] = (),
    source_priority: Sequence[str] = _ROLES,
    budget: Budget | None = None,
) -> Selection[Annotation]:
    """Negotiate original annotation terms, preserving ties and selected scopes.

    Lookup accepts basic ranges, tries their exact tag and successive RFC 4647
    truncations, then returns every annotation at the winning source-role tier.
    '*' requests the explicit default only after later requests are tried.
    Basic/extended filtering returns all matching annotations in request/source
    order. Untagged and other-language fallback tiers apply only after all
    requests miss. A source role omitted from source_priority is excluded even
    from fallback; importing additional resources is never part of negotiation.

    Completed empty selection is allowed and proves no publication completeness.
    Partial results carry an incomplete report and cannot establish final ties.
    Existing preferred_label behavior is unchanged.
    """
    if not isinstance(view, VocabularyView) or (
        budget is not None and not isinstance(budget, Budget)
    ):
        raise InputError("Expected an owned VocabularyView and optional Budget")
    if mode not in ("lookup", "basic-filter", "extended-filter"):
        raise InputError("Unknown annotation language matching mode")
    operation = budget if budget is not None else Budget(view.limits)
    issues = IssueCollector(operation)
    complete = True
    result: tuple[Annotation, ...] = ()
    checked = 0
    try:
        _text(iri, operation)
        absolute_iri(iri)
        selected_predicates = _capture(predicates, operation)
        if not selected_predicates:
            raise InputError("Select at least one annotation predicate")
        for predicate in selected_predicates:
            _text(predicate, operation)
            absolute_iri(predicate)
        compiled = tuple(
            _range(value, mode == "extended-filter", operation)
            for value in _capture(ranges, operation)
        )
        tiers = _capture(fallback, operation)
        if any(tier not in ("untagged", "other") for tier in tiers):
            raise InputError("Unknown annotation fallback tier")
        priorities = _capture(source_priority, operation)
        if not priorities or any(role not in _ROLES for role in priorities):
            raise InputError("Select local/imports/lookup source-role priority")
        if len(set(priorities)) != len(priorities) or len(set(tiers)) != len(tiers):
            raise InputError("Source priorities and fallback tiers must be unique")
        roles = {role: index for index, role in enumerate(priorities)}
        selected = view.annotations(
            iri, predicates=selected_predicates, budget=operation
        )
        list.extend(issues, selected.report.issues)
        complete = selected.report.complete
        entries = []
        tags: dict[str | None, tuple[str, ...] | None] = {}
        for annotation in selected.items:
            operation.consume("work")
            role = _role(annotation.scope)
            if role not in roles:
                continue
            for text in (annotation.value, annotation.predicate, annotation.scope):
                _text(text, operation)
            if annotation.language not in tags:
                tags[annotation.language] = _tag(annotation.language, operation)
            operation.consume("states")
            entries.append((annotation, tags[annotation.language], roles[role]))
        if mode == "lookup":
            for wanted in compiled:
                for candidate in _lookup(wanted, operation):
                    winners = []
                    for annotation, tag, priority in entries:
                        operation.consume("work", 1 + len(candidate))
                        if tag == candidate:
                            operation.consume("states")
                            winners.append((annotation, priority))
                    if winners:
                        best = min(priority for _, priority in winners)
                        result = tuple(a for a, priority in winners if priority == best)
                        break
                if result:
                    break
        else:
            matches = []
            for index, (annotation, tag, priority) in enumerate(entries):
                for request, wanted in enumerate(compiled):
                    if _matches(tag, wanted, mode == "extended-filter", operation):
                        operation.consume("states")
                        matches.append((request, priority, index, annotation))
                        break
            result = _ordered(matches, operation)
        if not result:
            for tier in tiers:
                matches = []
                for index, (annotation, tag, priority) in enumerate(entries):
                    operation.consume("work")
                    if (tag is None) == (tier == "untagged"):
                        operation.consume("states")
                        matches.append((0, priority, index, annotation))
                if matches:
                    if mode == "lookup":
                        best = min(item[1] for item in matches)
                        matches = [item for item in matches if item[1] == best]
                    result = _ordered(matches, operation)
                    break
        checked = len(entries)
    except LimitExceeded as error:
        complete = False
        issues.exhausted(str(error), location=iri)
    return Selection(
        result,
        Report(
            "annotations.language_selection",
            checked,
            tuple(issues),
            allow_empty=True,
            complete=complete,
            requested=checked if complete else checked + 1,
            families=("annotation-negotiation", mode),
            regime="asserted",
        ),
    )
