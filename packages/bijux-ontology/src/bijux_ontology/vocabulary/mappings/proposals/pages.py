"""Bounded resumable lexical joins with page-scoped explicit review."""

from threading import Event
from typing import Literal

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import Budget, IssueCollector, bound_text
from bijux_ontology.vocabulary.lexical.types import LexicalIndex

from .generation import _pair, _qualify_indexes
from .types import LexicalMappingProposal, MappingProposalCursor, MappingProposalPage


def propose_lexical_mappings_page(
    source: LexicalIndex,
    target: LexicalIndex,
    *,
    language: Literal["same", "any"] = "same",
    page_size: int = 100,
    cursor: MappingProposalCursor | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> MappingProposalPage:
    """Review finite join pages in key/source/target occurrence order.

    Cursor positions can resume within a many-to-many lexical group, without
    storing that whole group or rescanning earlier occurrences. Page completion
    certifies its actual occurrences, never global rank or semantic equivalence.
    """
    _qualify_indexes(source, target, language)
    operation = Operation(
        source.limits if budget is None else None, budget, timeout, cancel
    )
    if type(page_size) is not int or not 0 < page_size <= operation.limits.max_states:
        raise InputError("Mapping page size must be a positive bounded integer")
    if cursor is not None and (
        not isinstance(cursor, MappingProposalCursor)
        or cursor._source is not source
        or cursor._target is not target
        or cursor._language != language
    ):
        raise InputError("Mapping cursor does not match the exact indexes and language")
    key_position, first_position, second_position = (
        (0, 0, 0) if cursor is None else cursor._position
    )
    items: list[LexicalMappingProposal] = []
    issues = IssueCollector(operation)
    complete = False
    enumeration_complete = False
    try:
        while key_position < len(source._keys) and len(items) < page_size:
            key = source._keys[key_position]
            bound_text(key, operation)
            operation.consume("work")
            matches = target._entries.get(key, ())
            firsts = source._entries[key]
            if not matches:
                key_position += 1
                first_position = second_position = 0
                continue
            while first_position < len(firsts) and len(items) < page_size:
                while second_position < len(matches) and len(items) < page_size:
                    proposal = _pair(
                        firsts[first_position],
                        matches[second_position],
                        source,
                        target,
                        language,
                        operation,
                    )
                    if proposal is not None:
                        operation.consume("states")
                        operation.consume("bytes", 16)
                        items.append(proposal)
                    second_position += 1
                if second_position == len(matches):
                    first_position += 1
                    second_position = 0
            if first_position == len(firsts):
                key_position += 1
                first_position = second_position = 0
        operation.consume("work", 0)
        complete = True
        enumeration_complete = key_position >= len(source._keys)
    except LimitExceeded as error:
        issues.exhausted(str(error), location="mapping.proposals.page")
    continuation = (
        None
        if enumeration_complete
        else MappingProposalCursor._at(
            source, target, language, (key_position, first_position, second_position)
        )
    )
    return MappingProposalPage._page(
        tuple(items),
        Report(
            "mapping.proposals.page",
            len(items),
            tuple(issues),
            allow_empty=True,
            requested=len(items) if complete else len(items) + 1,
            complete=complete,
            regime="advisory-key-source-target-occurrence-order",
            families=(
                "casefold" if source.casefold else "exact",
                "language:" + language,
            ),
        ),
        operation.limits,
        continuation,
        enumeration_complete,
    )
