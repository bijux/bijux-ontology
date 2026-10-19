"""Cross-vocabulary joins over the existing owned lexical index."""

from threading import Event
from typing import Literal

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, IssueCollector
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.browsing.pages import _comparison_text
from bijux_ontology.vocabulary.lexical.types import LexicalIndex
from bijux_ontology.vocabulary.results import SearchMatch
from bijux_ontology.vocabulary.view import _LexicalCandidate

from .types import LexicalMappingProposal, MappingProposals


def _match(candidate: _LexicalCandidate, operation: Budget) -> SearchMatch:
    """Charge retained source evidence before constructing a public match."""
    operation.consume(
        "states", 1 + len(candidate.term.kinds) + len(candidate.term.scopes)
    )
    operation.consume("bytes", 128)
    for values in ((candidate.term.iri,), candidate.term.kinds, candidate.term.scopes):
        for value in values:
            bound_text(value, operation)
    return SearchMatch(
        candidate.term,
        _comparison_text(candidate.field, operation),
        _comparison_text(candidate.value, operation),
        _comparison_text(candidate.language, operation) if candidate.language else None,
        _comparison_text(candidate.scope, operation),
        0,
    )


def _order(
    item: LexicalMappingProposal,
) -> tuple[tuple[int, int, int], tuple[str, ...]]:
    """Make tie ordering stable while leaving semantic ambiguity unresolved."""
    return (
        item.rank,
        (
            item.source.term.iri,
            item.target.term.iri,
            item.source.field,
            item.target.field,
            item.source.value,
            item.target.value,
            item.source.language or "",
            item.target.language or "",
            item.source.scope,
            item.target.scope,
        ),
    )


def propose_lexical_mappings(
    source: LexicalIndex,
    target: LexicalIndex,
    *,
    language: Literal["same", "any"] = "same",
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> MappingProposals:
    """Join exact keys under the indexes' explicitly equal case policies.

    Fields, languages and source scopes come from each independently prepared
    index. No new lexical inventory or fuzzy matcher is built. By default only
    case-insensitively equal language tags (including untagged pairs) join; any
    explicitly
    permits cross-language candidates. No Unicode composition occurs. Broad
    synonyms, identical strings and homonyms supply evidence, never equivalence.

    All matching occurrences remain alternatives. Each lexical-key group is
    atomic; interruption returns only completed groups without claiming global
    rank or completeness. Complete results sort by documented rank and exact
    evidence, not probability, confidence or a chosen winning mapping.
    """
    if not isinstance(source, LexicalIndex) or not isinstance(target, LexicalIndex):
        raise InputError("Mapping proposals require two prepared lexical indexes")
    if source.casefold != target.casefold or language not in ("same", "any"):
        raise InputError("Select matching case policies and same or any language")
    operation = Operation(
        source.limits if budget is None else None, budget, timeout, cancel
    )
    issues = IssueCollector(operation)
    items: list[LexicalMappingProposal] = []
    checked = 0
    complete = False
    try:
        for key in source._keys:
            bound_text(key, operation)
            operation.consume("work")
            group: list[LexicalMappingProposal] = []
            matches = target._entries.get(key, ())
            for first in source._entries[key] if matches else ():
                for second in matches:
                    operation.consume("work")
                    if language == "same":
                        left_tag = _comparison_text(
                            first.language or "", operation, fold=True
                        )
                        right_tag = _comparison_text(
                            second.language or "", operation, fold=True
                        )
                        if left_tag != right_tag:
                            continue
                    left, right = _match(first, operation), _match(second, operation)
                    operation.consume("work", len(left.value) + len(right.value))
                    exact = left.value == right.value
                    operation.consume("states", 2)
                    operation.consume("bytes", 160)
                    group.append(
                        LexicalMappingProposal(
                            left,
                            right,
                            "exact" if exact else "casefold",
                            (
                                int(not exact),
                                source._priority[left.field],
                                target._priority[right.field],
                            ),
                        )
                    )
            operation.consume("states", len(group))
            operation.consume("bytes", 64 + 8 * len(group))
            items.extend(group)
            checked += 1
        operation.consume("states", len(items))
        operation.consume("bytes", 64 + 320 * len(items))
        operation.consume("work", len(items))
        size = sum(sum(map(len, _order(item)[1])) for item in items)
        operation.consume("work", (size + len(items)) * max(1, len(items).bit_length()))
        items.sort(key=_order)
        complete = True
    except LimitExceeded as error:
        issues.exhausted(str(error), location="mapping.proposals")
    return MappingProposals._owned(
        tuple(items),
        Report(
            "mapping.proposals",
            checked,
            tuple(issues),
            requested=len(source._keys)
            if complete
            else max(len(source._keys), checked + 1),
            complete=complete,
            allow_empty=True,
            regime="advisory-indexed-lexical-pairs",
            families=(
                "casefold" if source.casefold else "exact",
                "language:" + language,
            ),
        ),
        operation.limits,
    )
