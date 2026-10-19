"""Bounded prefix and deletion derivatives of existing owned lexical candidates."""

from collections.abc import Sequence
from threading import Event
from types import MappingProxyType

from rdflib.namespace import RDFS, SKOS

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.limits import Budget, IssueCollector, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri, language_tag
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.browsing.pages import (
    _comparison_text,
    _operation,
    _PageBudget,
)
from bijux_ontology.vocabulary.sources.view import _LexicalCandidate

from .types import LexicalIndex, LexicalPreparation

_DEFAULT_FIELDS = ("iri", str(RDFS.label), str(SKOS.prefLabel), str(SKOS.altLabel))


def _capture(
    values: Sequence[str], budget: Budget, *, languages: bool = False
) -> tuple[str, ...]:
    """Capture normalized accepted text once, preserving explicit field priority."""
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        raise InputError("Lexical fields/languages require finite text sequences")
    count = len(values)
    budget.consume("states", count)
    budget.consume("bytes", 64 + 64 * count)
    selected: dict[str, None] = {}
    for index in range(count):
        budget.consume("work")
        value = _comparison_text(values[index], budget)
        if languages:
            language_tag(value)
            budget.consume("work", len(value))
            value = value.lower()
        elif value != "iri":
            absolute_iri(value)
        selected.setdefault(value, None)
    return tuple(selected)


def _variants(text: str, budget: Budget) -> tuple[str, ...]:
    """Prepay all single-deletion text and hashing before quadratic allocation."""
    count = len(text)
    budget.consume("work", (count + 1) * (count + 1))
    budget.consume("bytes", 128 + (count + 1) * (64 + 12 * count))
    budget.consume("states", count + 1)
    result = {text: None}
    for offset in range(count):
        budget.consume("work")
        result.setdefault(text[:offset] + text[offset + 1 :], None)
    return tuple(result)


def prepare_lexical_index(
    view: VocabularyView,
    *,
    fields: Sequence[str] = _DEFAULT_FIELDS,
    languages: Sequence[str] = (),
    casefold: bool = False,
    budget: Budget | None = None,
    cancel: Event | None = None,
) -> LexicalPreparation:
    """Prepare one immutable selected-role lexical index, without copying RDF.

    Existing declared-term lexical candidates supply the selected inventory.
    Languages select exact case-insensitive tags; an empty selection admits all
    tags and untagged text. Identifier fields ignore language selection. Unicode
    casefold is optional; no normalization, tokenization or IRI expansion occurs.
    Typo lookup supports at most one codepoint insertion/deletion/substitution,
    not adjacent transposition or semantic similarity.
    """
    selected = _operation(view, budget)
    if (
        type(casefold) is not bool
        or cancel is not None
        and not isinstance(cancel, Event)
    ):
        raise InputError("Lexical casefold/cancellation metadata is invalid")
    operation = _PageBudget(selected, cancel)
    issues = IssueCollector(selected)
    result = None
    checked = 0
    try:
        wanted = _capture(fields, operation)
        tags = _capture(languages, operation, languages=True)
        if not wanted:
            raise InputError("Lexical indexing requires at least one selected field")
        operation.consume("work", sum(map(len, wanted)) + sum(map(len, tags)))
        operation.consume("states", len(wanted) + len(tags))
        operation.consume("bytes", 128 + 64 * (len(wanted) + len(tags)))
        priority = {field: index for index, field in enumerate(wanted)}
        language_set = frozenset(tags)
        entries: dict[str, list[_LexicalCandidate]] = {}
        candidates = view._vocabulary._browse_lexical(
            view._reader, view._index, operation
        )
        for candidate in candidates:
            operation.consume("work")
            field = _comparison_text(candidate.field, operation)
            if field not in priority:
                continue
            language = _comparison_text(candidate.language or "", operation)
            if language_set and field != "iri" and language.lower() not in language_set:
                continue
            key = _comparison_text(candidate.value, operation, fold=casefold)
            bound_text(key, operation)
            operation.consume("states", 2)
            operation.consume("bytes", 96)
            entries.setdefault(key, []).append(candidate)
            checked += 1
        operation.consume(
            "work", sum(len(k) + 1 for k in entries) * max(1, len(entries).bit_length())
        )
        operation.consume("states", 3 * len(entries))
        operation.consume("bytes", 128 + 96 * len(entries) + 16 * checked)
        keys = tuple(sorted(entries))
        deletions: dict[str, list[str]] = {}
        for key in keys:
            for variant in _variants(key, operation):
                operation.consume("work", len(variant) + 1)
                operation.consume("states", 2)
                operation.consume("bytes", 96)
                deletions.setdefault(variant, []).append(key)
        operation.consume("states", len(deletions))
        operation.consume("bytes", 64 + 64 * len(deletions))
        frozen_entries = MappingProxyType(
            {key: tuple(value) for key, value in entries.items()}
        )
        frozen_deletions = MappingProxyType(
            {key: tuple(value) for key, value in deletions.items()}
        )
        operation.consume("states", len(view._reader.scopes))
        operation.consume("bytes", 32 + 8 * len(view._reader.scopes))
        for scope, _ in view._reader.scopes:
            bound_text(scope, operation)
        scopes = tuple(scope for scope, _ in view._reader.scopes)
        result = LexicalIndex._owned(
            wanted,
            tags,
            scopes,
            casefold,
            selected.limits,
            keys,
            frozen_entries,
            frozen_deletions,
            MappingProxyType(priority),
        )
    except LimitExceeded as error:
        issues.exhausted(str(error), location="terms.lexical.prepare")
    return LexicalPreparation(
        result,
        Report(
            "terms.lexical.prepare",
            checked,
            tuple(issues),
            allow_empty=True,
            requested=checked if result is not None else checked + 1,
            complete=result is not None,
            regime="indexed-asserted-lexical-evidence",
        ),
    )
