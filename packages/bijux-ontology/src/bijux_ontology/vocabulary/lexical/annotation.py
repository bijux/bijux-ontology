"""Dictionary mentions over shared lexical keys and original text boundaries."""

import unicodedata
from dataclasses import dataclass
from threading import Event
from typing import Literal

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget, IssueCollector
from bijux_ontology.vocabulary.browsing.admission import _comparison_text, _PageBudget
from bijux_ontology.vocabulary.browsing.results import SearchMatch

from .automaton import MentionIndex, _prepare_mentions
from .types import LexicalIndex


@dataclass(frozen=True)
class TextMention:
    """One half-open Python character span with every selected lexical candidate.

    Candidates are dictionary evidence, not confirmed ontology annotations.
    Their original predicates, languages and source roles remain independent.
    """

    start: int
    end: int
    candidates: tuple[SearchMatch, ...]


@dataclass(frozen=True)
class TextAnnotation:
    """Original text, complete mentions and truthful interrupted-prefix coverage."""

    text: str
    mentions: tuple[TextMention, ...]
    report: Report


def _word(character: str) -> bool:
    """Treat combining marks as word continuations, alongside letters and digits."""
    return (
        character.isalnum()
        or character == "_"
        or unicodedata.category(character).startswith("M")
    )


def annotate_text(
    index: LexicalIndex | MentionIndex,
    text: str,
    *,
    boundary: Literal["word", "substring"] = "word",
    budget: Budget | None = None,
    cancel: Event | None = None,
) -> TextAnnotation:
    """Locate overlapping exact dictionary mentions without resolving ambiguity.

    The prepared index owns fields, languages, source roles and case policy.
    A MentionIndex reuses its completed automaton; a base LexicalIndex prepares
    the same bounded derivative within this operation.
    Unicode casefold may expand characters, but matches must begin and end at
    original character boundaries: half of a folded sharp-s is never a mention.
    No Unicode normalization, tokenization or semantic disambiguation occurs.
    Word mode requires nonword characters (or text edges) around a mention;
    substring mode removes that restriction. Offsets count Unicode codepoints,
    not bytes, grapheme clusters or positions in the casefolded comparison text.
    """
    if not isinstance(index, LexicalIndex | MentionIndex) or type(text) is not str:
        raise InputError("Text annotation requires a prepared index and text")
    if boundary not in ("word", "substring"):
        raise InputError("Text boundary must be word or substring")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Text annotation budget requires Budget")
    if cancel is not None and not isinstance(cancel, Event):
        raise InputError("Text annotation cancellation requires threading.Event")
    lexical = index._index if isinstance(index, MentionIndex) else index
    selected = budget if budget is not None else Budget(lexical.limits)
    operation = _PageBudget(selected, cancel)
    issues = IssueCollector(selected)
    buckets: list[list[TextMention] | None] = []
    original = ""
    checked = 0
    complete = False
    settled = 0
    try:
        original = _comparison_text(text, operation)
        dictionary = (
            index
            if isinstance(index, MentionIndex)
            else _prepare_mentions(index, operation)
        )
        compared = _comparison_text(original, operation, fold=lexical.casefold)
        operation.consume("states", 3 * (len(original) + 1))
        operation.consume("bytes", 128 + 144 * (len(original) + 1))
        # Ordering buckets avoid a post-interruption sort. Their traversal and
        # the final reference vector are prepaid before matching begins.
        operation.consume("work", 2 * (len(original) + 1))
        buckets = [None] * (len(original) + 1)
        boundaries = {0: 0}
        starts = [0]
        position = 0
        for offset, character in enumerate(original):
            operation.consume("work", 4)
            position += len(character.casefold()) if lexical.casefold else 1
            boundaries[position] = offset + 1
            starts.append(position)
        state = 0
        for position, character in enumerate(compared, 1):
            operation.consume("work")
            while state and character not in dictionary._transitions[state]:
                operation.consume("work")
                state = dictionary._failures[state]
            operation.consume("work")
            state = dictionary._transitions[state].get(character, 0)
            end = boundaries.get(position)
            if end is None:
                continue
            checked = end
            if boundary == "word" and end < len(original) and _word(original[end]):
                while (
                    settled < len(original)
                    and starts[settled] + dictionary._max_length <= position
                ):
                    settled += 1
                continue
            output = state
            while output:
                operation.consume("work")
                key = dictionary._terminal[output]
                if key is not None:
                    start = boundaries.get(position - len(key))
                    if start is not None and not (
                        boundary == "word" and start and _word(original[start - 1])
                    ):
                        entries = lexical._entries[key]
                        operation.consume("states", len(entries) + 1)
                        operation.consume("bytes", 160 + 128 * len(entries))
                        operation.consume("work", len(entries) + 2)
                        candidates = tuple(
                            SearchMatch(
                                candidate.term,
                                _comparison_text(candidate.field, operation),
                                _comparison_text(candidate.value, operation),
                                candidate.language,
                                candidate.scope,
                                0,
                            )
                            for candidate in entries
                        )
                        mention = TextMention(start, end, candidates)
                        bucket = buckets[start]
                        if bucket is None:
                            bucket = []
                            buckets[start] = bucket
                        bucket.append(mention)
                output = dictionary._suffix[output]
            while (
                settled < len(original)
                and starts[settled] + dictionary._max_length <= position
            ):
                settled += 1
        operation.consume("work", 0)
        complete = True
    except LimitExceeded as error:
        issues.exhausted(str(error), location="terms.lexical.annotate")
    # Only funded complete matches are present, even after a failed candidate.
    mentions = tuple(
        mention
        for start, bucket in enumerate(buckets)
        if (complete or start < settled) and bucket
        for mention in bucket
    )
    return TextAnnotation(
        original,
        mentions,
        Report(
            "terms.lexical.annotate",
            checked,
            tuple(issues),
            allow_empty=True,
            requested=len(original) if complete else max(len(original), checked + 1),
            complete=complete,
            regime="dictionary-candidates-original-codepoint-spans",
            families=("unicode:" + unicodedata.unidata_version, "boundary:" + boundary),
        ),
    )
