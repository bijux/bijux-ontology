"""Dictionary mentions over shared lexical keys and original text boundaries."""

import unicodedata
from bisect import bisect_left
from dataclasses import dataclass
from threading import Event
from typing import Literal

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.limits import Budget, IssueCollector
from bijux_ontology.vocabulary.browsing.pages import _comparison_text, _PageBudget
from bijux_ontology.vocabulary.browsing.results import SearchMatch

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
    """Original text and complete mentions, or a proved prefix on interruption."""

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
    index: LexicalIndex,
    text: str,
    *,
    boundary: Literal["word", "substring"] = "word",
    budget: Budget | None = None,
    cancel: Event | None = None,
) -> TextAnnotation:
    """Locate overlapping exact dictionary mentions without resolving ambiguity.

    The prepared index owns fields, languages, source roles and case policy.
    Unicode casefold may expand characters, but matches must begin and end at
    original character boundaries: half of a folded sharp-s is never a mention.
    No Unicode normalization, tokenization or semantic disambiguation occurs.
    Word mode requires nonword characters (or text edges) around a mention;
    substring mode removes that restriction. Offsets count Unicode codepoints,
    not bytes, grapheme clusters or positions in the casefolded comparison text.
    """
    if not isinstance(index, LexicalIndex) or type(text) is not str:
        raise InputError("Text annotation requires a prepared index and text")
    if boundary not in ("word", "substring"):
        raise InputError("Text boundary must be word or substring")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Text annotation budget requires Budget")
    if cancel is not None and not isinstance(cancel, Event):
        raise InputError("Text annotation cancellation requires threading.Event")
    selected = budget if budget is not None else Budget(index.limits)
    operation = _PageBudget(selected, cancel)
    issues = IssueCollector(selected)
    mentions: list[TextMention] = []
    original = ""
    checked = 0
    complete = False
    try:
        original = _comparison_text(text, operation)
        compared = _comparison_text(original, operation, fold=index.casefold)
        operation.consume("states", 2 * (len(original) + 1))
        operation.consume("bytes", 128 + 112 * (len(original) + 1))
        starts = [0]
        ends = {0: 0}
        position = 0
        for offset, character in enumerate(original):
            operation.consume("work", 4)
            position += len(character.casefold()) if index.casefold else 1
            starts.append(position)
            ends[position] = offset + 1
        for start in range(len(original)):
            operation.consume("work")
            if boundary == "word" and start and _word(original[start - 1]):
                checked += 1
                continue
            prefix = ""
            for offset in range(starts[start], len(compared)):
                length = len(prefix) + 1
                operation.consume("work", length * (len(index._keys).bit_length() + 3))
                operation.consume("bytes", 64 + 4 * length)
                prefix += compared[offset]
                lower = bisect_left(index._keys, prefix)
                if lower == len(index._keys) or not index._keys[lower].startswith(
                    prefix
                ):
                    break
                end = ends.get(offset + 1)
                if end is None or prefix not in index._entries:
                    continue
                if boundary == "word" and end < len(original) and _word(original[end]):
                    continue
                entries = index._entries[prefix]
                operation.consume("states", len(entries) + 1)
                operation.consume("bytes", 128 + 128 * len(entries))
                operation.consume("work", len(entries))
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
                mentions.append(TextMention(start, end, candidates))
            checked += 1
        complete = True
    except LimitExceeded as error:
        issues.exhausted(str(error), location="terms.lexical.annotate")
    return TextAnnotation(
        original,
        tuple(mentions),
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
