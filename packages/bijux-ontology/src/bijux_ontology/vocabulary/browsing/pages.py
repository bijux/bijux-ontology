"""Bounded native-index continuation for asserted vocabulary browsing."""

from collections.abc import Callable, Sequence
from threading import Event
from typing import TypeVar

from rdflib.namespace import RDFS, SKOS

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, BudgetKind, IssueCollector
from bijux_ontology.rdf.identifiers import absolute_iri, language_tag
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.inspection import VocabularyView
from bijux_ontology.vocabulary.results import Edge, SearchMatch, TermInfo
from bijux_ontology.vocabulary.view import _LexicalCandidate

from .types import VocabularyCursor, VocabularyPage

S = TypeVar("S")
T = TypeVar("T")
_DEFAULT_FIELDS = (
    "iri",
    str(RDFS.label),
    str(SKOS.prefLabel),
    str(SKOS.altLabel),
    str(SKOS.notation),
)


class _PageBudget(Budget):
    """Forward one allowance while checking cancellation during lazy preparation."""

    def __init__(self, budget: Budget, cancel: Event | None) -> None:
        """Borrow counters; never reset or inherit a separate operation budget."""
        self.limits = budget.limits
        self._budget = budget
        self._cancel = cancel

    def consume(self, kind: BudgetKind, amount: int = 1) -> None:
        """Check cancellation before each preparation or candidate work unit."""
        if self._cancel is not None and self._cancel.is_set():
            raise LimitExceeded("Vocabulary browsing cancelled")
        self._budget.consume(kind, amount)


def _operation(view: VocabularyView, budget: Budget | None) -> Budget:
    """Validate the owned input boundary before reading snapshot limits."""
    if not isinstance(view, VocabularyView) or (
        budget is not None and not isinstance(budget, Budget)
    ):
        raise InputError("Expected a VocabularyView and optional Budget")
    return budget if budget is not None else Budget(view.limits)


def _text(value: str, budget: Budget) -> None:
    """Bound scalar UTF-8 text before capture, validation or normalization."""
    if not isinstance(value, str):
        raise InputError("Page query and filter text must be strings")
    if len(value) > budget.limits.max_bytes:
        raise LimitExceeded("Page text exceeds max_bytes")
    budget.consume("work", len(value))
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise InputError("Page text requires valid Unicode") from error
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Page text exceeds max_bytes")
    budget.consume("bytes", size)


def _comparison_text(value: str, budget: Budget, *, fold: bool = False) -> str:
    """Charge text conversion and optional Unicode casefold before allocating."""
    _text(value, budget)
    budget.consume("work", len(value))
    budget.consume("bytes", 64 + 4 * len(value))
    result = str(value)
    if fold:
        # Unicode full casefold expands each input character to at most three.
        budget.consume("work", 3 * len(value))
        budget.consume("bytes", 64 + 12 * len(value))
        result = result.casefold()
    return result


def _strings(
    values: Sequence[str],
    budget: Budget,
    *,
    languages: bool = False,
    iri_field: bool = False,
) -> tuple[str, ...]:
    """Own finite filter sets; equivalent filter order has equivalent meaning."""
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        raise InputError("Page filters require a finite sequence of strings")
    count = len(values)
    budget.consume("states", count)
    budget.consume("bytes", 128 + 64 * count if count else 0)
    selected: list[str] = []
    for index in range(count):
        budget.consume("work")
        value = values[index]
        _text(value, budget)
        if languages:
            language_tag(value)
            budget.consume("work", len(value))
            budget.consume("bytes", 64 + 4 * len(value))
            value = value.lower()
        elif not (iri_field and value == "iri"):
            absolute_iri(value)
        selected.append(value)
    budget.consume("work", count * max(1, count.bit_length()))
    return tuple(sorted(set(selected)))


def _filter_set(values: tuple[str, ...], budget: Budget) -> frozenset[str]:
    """Prepare exact membership once, charging retained references and hashing."""
    budget.consume("work", sum(len(value) + 1 for value in values))
    budget.consume("bytes", 128 + 64 * len(values) if values else 0)
    return frozenset(values)


def _page(
    view: VocabularyView,
    read: Callable[[Budget], Sequence[S]],
    select: Callable[[S, int, Budget], T | None],
    contract: tuple[object, ...],
    *,
    page_size: int,
    cursor: VocabularyCursor | None,
    budget: Budget,
    cancel: Event | None,
    phases: int = 1,
) -> VocabularyPage[T]:
    """Resume directly; commit a position only after retaining its proved result."""
    if type(page_size) is not int or page_size <= 0:
        raise InputError("Page size must be a positive integer")
    if page_size > budget.limits.max_states:
        raise InputError("Page size exceeds the selected state bound")
    if cancel is not None and not isinstance(cancel, Event):
        raise InputError("Cancellation must be a threading.Event")
    if cursor is not None and (
        not isinstance(cursor, VocabularyCursor)
        or cursor._index is not view._index
        or cursor._contract != contract
    ):
        raise InputError("Cursor does not match this snapshot, filters or ordering")
    position = 0 if cursor is None else cursor._position
    phase = 0 if cursor is None else cursor._phase
    items: list[T] = []
    issues = IssueCollector(budget)
    page_complete = True
    guarded = _PageBudget(budget, cancel)
    try:
        guarded.consume("work", 0)
        candidates = read(guarded)
        count = len(candidates)
        while phase < phases and len(items) < page_size:
            if cancel is not None and cancel.is_set():
                raise LimitExceeded("Vocabulary browsing cancelled")
            if position == count:
                position = 0
                phase += 1
                continue
            guarded.consume("work")
            value = select(candidates[position], phase, guarded)
            if value is not None:
                budget.consume("states")
                items.append(value)
            position += 1
        # An exact boundary needs no speculative access to the next candidate.
        if position == count:
            position = 0
            phase += 1
        enumeration_complete = phase >= phases
    except LimitExceeded as error:
        issues.exhausted(str(error), location=str(contract[0]))
        page_complete = False
        enumeration_complete = False
    family = str(contract[0])
    return VocabularyPage(
        tuple(items),
        None
        if enumeration_complete
        else VocabularyCursor._at(view._index, contract, position, phase),
        enumeration_complete,
        Report(
            family,
            len(items),
            tuple(issues),
            allow_empty=True,
            complete=page_complete,
            requested=len(items) if page_complete else len(items) + 1,
            regime="asserted",
            families=(family,),
        ),
    )


def terms_page(
    view: VocabularyView,
    *,
    kinds: Sequence[str] = (),
    namespaces: Sequence[str] = (),
    order: str = "iri",
    page_size: int = 100,
    cursor: VocabularyCursor | None = None,
    budget: Budget | None = None,
    cancel: Event | None = None,
) -> VocabularyPage[TermInfo]:
    """Page declared terms in exact IRI order, retaining kinds and source scopes."""
    if order != "iri":
        raise InputError("Term pages require iri ordering")
    selected = _operation(view, budget)
    wanted_kinds = _strings(kinds, selected)
    wanted_namespaces = _strings(namespaces, selected)

    kind_set: frozenset[str] = frozenset()

    def candidates(allowance: Budget) -> Sequence[TermInfo]:
        """Prepare exact filters and native positions inside the resumable read."""
        nonlocal kind_set
        kind_set = _filter_set(wanted_kinds, allowance)
        return view._vocabulary._browse_terms(view._reader, view._index, allowance)

    def include(term: TermInfo, _phase: int, allowance: Budget) -> TermInfo | None:
        """Bound each comparison without rebuilding filters for every term."""
        if kind_set:
            for kind in term.kinds:
                allowance.consume("work", len(kind) + 1)
                if kind in kind_set:
                    break
            else:
                return None
        if wanted_namespaces:
            for namespace in wanted_namespaces:
                allowance.consume("work", min(len(term.iri), len(namespace)) + 1)
                if term.iri.startswith(namespace):
                    break
            else:
                return None
        return term

    return _page(
        view,
        candidates,
        include,
        ("terms.page", order, wanted_kinds, wanted_namespaces),
        page_size=page_size,
        cursor=cursor,
        budget=selected,
        cancel=cancel,
    )


def statements_page(
    view: VocabularyView,
    *,
    predicates: Sequence[str] = (),
    order: str = "subject-predicate-object-scope",
    page_size: int = 100,
    cursor: VocabularyCursor | None = None,
    budget: Budget | None = None,
    cancel: Event | None = None,
) -> VocabularyPage[Edge]:
    """Page original scoped assertions in the existing inspection index order."""
    if order != "subject-predicate-object-scope":
        raise InputError(
            "Statement pages require subject-predicate-object-scope ordering"
        )
    selected = _operation(view, budget)
    wanted = _strings(predicates, selected)

    predicate_set: frozenset[str] = frozenset()

    def candidates(allowance: Budget) -> Sequence[Edge]:
        """Prepare exact predicates without allocating a separate edge index."""
        nonlocal predicate_set
        predicate_set = _filter_set(wanted, allowance)
        return view._index.edges

    def include(edge: Edge, _phase: int, allowance: Budget) -> Edge | None:
        """Retain original assertions after one bounded exact membership lookup."""
        if not predicate_set:
            return edge
        predicate = _comparison_text(edge.predicate, allowance)
        return edge if predicate in predicate_set else None

    return _page(
        view,
        candidates,
        include,
        ("terms.statements.page", order, wanted),
        page_size=page_size,
        cursor=cursor,
        budget=selected,
        cancel=cancel,
    )


def search_page(
    view: VocabularyView,
    query: str,
    *,
    fields: Sequence[str] = _DEFAULT_FIELDS,
    languages: Sequence[str] = (),
    exact: bool = False,
    casefold: bool = False,
    order: str = "rank-term-field-language-value-scope",
    page_size: int = 100,
    cursor: VocabularyCursor | None = None,
    budget: Budget | None = None,
    cancel: Event | None = None,
) -> VocabularyPage[SearchMatch]:
    """Page lexical evidence in the same ranking and tie order as view.search.

    Three finite rank passes use native candidate positions. Resuming a page
    never scans the earlier positions of its current pass or materializes all
    query matches. Literal lexical forms, language and source remain unchanged.
    """
    if not isinstance(query, str) or not query:
        raise InputError("Search requires nonempty text")
    if type(exact) is not bool or type(casefold) is not bool:
        raise InputError("Search exact/casefold options must be booleans")
    if order != "rank-term-field-language-value-scope":
        raise InputError(
            "Search pages require rank-term-field-language-value-scope ordering"
        )
    selected = _operation(view, budget)
    _text(query, selected)
    wanted_fields = _strings(fields, selected, iri_field=True)
    wanted_languages = _strings(languages, selected, languages=True)
    match_query = (
        _comparison_text(query, selected, fold=casefold) if casefold else query
    )

    field_set: frozenset[str] = frozenset()
    language_set: frozenset[str] = frozenset()

    def candidates(allowance: Budget) -> Sequence[_LexicalCandidate]:
        """Prepare exact lexical filters and shared positions within one page."""
        nonlocal field_set, language_set
        field_set = _filter_set(wanted_fields, allowance)
        language_set = _filter_set(wanted_languages, allowance)
        return view._vocabulary._browse_lexical(view._reader, view._index, allowance)

    def include(
        candidate: _LexicalCandidate, phase: int, allowance: Budget
    ) -> SearchMatch | None:
        """Classify one maintained RDF annotation in its current rank pass."""
        field = _comparison_text(candidate.field, allowance)
        if field not in field_set:
            return None
        if language_set and field != "iri":
            language = candidate.language or ""
            allowance.consume("work", len(language))
            allowance.consume("bytes", 64 + 4 * len(language))
            if language.lower() not in language_set:
                return None
        value = _comparison_text(candidate.value, allowance)
        compared = _comparison_text(value, allowance, fold=True) if casefold else value
        allowance.consume("work", len(compared) + len(match_query))
        equal = compared == match_query
        if not equal and (exact or match_query not in compared):
            return None
        rank = 0 if field == "iri" and equal else 1 if equal else 2
        if rank != phase:
            return None
        allowance.consume("bytes", 128)
        return SearchMatch(
            candidate.term,
            field,
            value,
            candidate.language,
            candidate.scope,
            rank,
        )

    return _page(
        view,
        candidates,
        include,
        (
            "terms.search.page",
            order,
            query,
            wanted_fields,
            wanted_languages,
            exact,
            casefold,
        ),
        page_size=page_size,
        cursor=cursor,
        budget=selected,
        cancel=cancel,
        phases=3,
    )
