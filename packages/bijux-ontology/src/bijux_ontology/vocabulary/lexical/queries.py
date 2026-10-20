"""Prefix ranges and finite one-edit alternatives over an immutable derivative."""

from bisect import bisect_left
from itertools import chain
from threading import Event
from typing import Literal

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget, IssueCollector, bound_text
from bijux_ontology.vocabulary.browsing.pages import _comparison_text, _PageBudget
from bijux_ontology.vocabulary.browsing.results import SearchMatch
from bijux_ontology.vocabulary.sources.view import _LexicalCandidate

from .preparation import _variants
from .types import LexicalIndex, LexicalResults, LexicalSuggestion

_Rank = tuple[int, int, str, str, str, str, str]


def _distance(left: str, right: str, budget: Budget) -> int | None:
    """Verify distance zero/one linearly; never allocate a dynamic-program table."""
    budget.consume("work", 2 * (len(left) + len(right) + 1))
    if abs(len(left) - len(right)) > 1:
        return None
    i = j = edits = 0
    while i < len(left) and j < len(right):
        if left[i] == right[j]:
            i += 1
            j += 1
            continue
        edits += 1
        if edits > 1:
            return None
        if len(left) <= len(right):
            j += 1
        if len(left) >= len(right):
            i += 1
    edits += int(i < len(left) or j < len(right))
    return edits if edits <= 1 else None


def _offer(
    candidate: _LexicalCandidate,
    key: str,
    distance: int,
    kind: Literal["exact", "prefix", "one-edit"],
    priority: int,
    limit: int,
    ranks: list[_Rank],
    matches: list[LexicalSuggestion],
    budget: Budget,
) -> None:
    """Retain bounded best alternatives; pay comparisons and shifts before insert."""
    field = _comparison_text(candidate.field, budget)
    value = _comparison_text(candidate.value, budget)
    for text in chain(
        (candidate.term.iri,),
        candidate.term.kinds,
        candidate.term.scopes,
        (candidate.language or "", candidate.scope),
    ):
        bound_text(text, budget)
    budget.consume(
        "work",
        (
            len(key)
            + len(value)
            + len(candidate.term.iri)
            + len(field)
            + len(candidate.scope)
            + len(candidate.language or "")
            + 1
        )
        * (len(ranks).bit_length() + 1),
    )
    rank = (
        distance,
        priority,
        key,
        candidate.term.iri,
        candidate.language or "",
        value,
        candidate.scope,
    )
    position = bisect_left(ranks, rank)
    if position >= limit:
        return
    budget.consume("work", len(ranks) + 1)
    budget.consume("states", 2)
    budget.consume("bytes", 256)
    suggestion = LexicalSuggestion(
        SearchMatch(
            candidate.term, field, value, candidate.language, candidate.scope, distance
        ),
        kind,
        None if kind == "prefix" else distance,
        priority,
    )
    ranks.insert(position, rank)
    matches.insert(position, suggestion)
    if len(matches) > limit:
        ranks.pop()
        matches.pop()


def _query(
    index: LexicalIndex,
    query: str,
    *,
    typo: bool,
    limit: int,
    budget: Budget | None,
    cancel: Event | None,
) -> LexicalResults:
    """Compute only the finite requested top set, retaining interruption evidence."""
    if not isinstance(index, LexicalIndex):
        raise InputError("Lexical queries require a prepared LexicalIndex")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Lexical query budget requires Budget")
    if type(limit) is not int or limit <= 0:
        raise InputError("Lexical result limit must be a positive integer")
    if not isinstance(query, str) or not query:
        raise InputError("Lexical queries require nonempty text")
    if cancel is not None and not isinstance(cancel, Event):
        raise InputError("Lexical cancellation requires threading.Event")
    selected = budget if budget is not None else Budget(index.limits)
    operation = _PageBudget(selected, cancel)
    issues = IssueCollector(selected)
    matches: list[LexicalSuggestion] = []
    ranks: list[_Rank] = []
    complete = False
    observed = 0
    original = ""
    try:
        if limit > operation.limits.max_states:
            raise InputError("Lexical result limit exceeds the selected state bound")
        original = _comparison_text(query, operation)
        compared = _comparison_text(original, operation, fold=index.casefold)
        bound_text(compared, operation)
        operation.consume("states", 3 * limit)
        operation.consume("bytes", 128 + 32 * limit)
        keys: list[str] = []
        if typo:
            found: set[str] = set()
            for variant in _variants(compared, operation):
                operation.consume("work", len(variant) + 1)
                for key in index._deletions.get(variant, ()):
                    bound_text(key, operation)
                    if key not in found:
                        operation.consume("states", 2)
                        operation.consume("bytes", 64)
                        found.add(key)
                        keys.append(key)
            operation.consume(
                "work", sum(len(k) + 1 for k in keys) * max(1, len(keys).bit_length())
            )
            keys.sort()
        else:
            # Binary search comparison work is bounded by the maximum compared
            # prefix length; no preceding lexical inventory is traversed.
            operation.consume(
                "work", (len(compared) + 1) * (len(index._keys).bit_length() + 1)
            )
            position = bisect_left(index._keys, compared)
            while position < len(index._keys):
                operation.consume("work", len(compared) + 1)
                key = index._keys[position]
                if not key.startswith(compared):
                    break
                operation.consume("states")
                operation.consume("bytes", 16)
                keys.append(key)
                position += 1
        for key in keys:
            bound_text(key, operation)
            distance = (
                _distance(compared, key, operation) if typo else int(key != compared)
            )
            if distance is None:
                continue
            kind: Literal["exact", "prefix", "one-edit"] = (
                "exact" if distance == 0 else "one-edit" if typo else "prefix"
            )
            for candidate in index._entries[key]:
                operation.consume("work")
                _offer(
                    candidate,
                    key,
                    distance,
                    kind,
                    index._priority[_comparison_text(candidate.field, operation)],
                    limit,
                    ranks,
                    matches,
                    operation,
                )
                observed += 1
        complete = True
    except LimitExceeded as error:
        issues.exhausted(
            str(error),
            location="terms.lexical.suggest" if typo else "terms.lexical.autocomplete",
        )
    return LexicalResults(
        original,
        tuple(matches),
        observed > limit,
        Report(
            "terms.lexical.suggest" if typo else "terms.lexical.autocomplete",
            observed,
            tuple(issues),
            allow_empty=True,
            requested=observed if complete else observed + 1,
            complete=complete,
            regime="indexed-one-codepoint-edit" if typo else "indexed-lexical-prefix",
        ),
    )


def autocomplete(
    index: LexicalIndex,
    prefix: str,
    *,
    limit: int = 10,
    budget: Budget | None = None,
    cancel: Event | None = None,
) -> LexicalResults:
    """Return exact/prefix alternatives; no source fact or submitted IRI changes."""
    return _query(index, prefix, typo=False, limit=limit, budget=budget, cancel=cancel)


def suggest(
    index: LexicalIndex,
    query: str,
    *,
    limit: int = 10,
    budget: Budget | None = None,
    cancel: Event | None = None,
) -> LexicalResults:
    """Return exact or one insertion/deletion/substitution, not transposition.

    Ranking is edit distance, explicit field priority, normalized lexical key,
    term IRI, language, original value and source. Evidence is advisory lexical
    data only; no replacement, equivalence, confidence or existence is inferred.
    """
    return _query(index, query, typo=True, limit=limit, budget=budget, cancel=cancel)
