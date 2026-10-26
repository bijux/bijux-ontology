"""Bounded native-index continuation for asserted vocabulary browsing."""

from collections.abc import Callable, Sequence
from threading import Event
from typing import TypeVar

from rdflib.namespace import RDFS, SKOS

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget, IssueCollector
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.browsing.results import Edge, SearchMatch, TermInfo
from bijux_ontology.vocabulary.sources.view import _LexicalCandidate

from .admission import _comparison_text, _filter_set, _PageBudget, _strings
from .matching import _Search
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


def _operation(view: VocabularyView, budget: Budget | None) -> Budget:
    """Validate the owned input boundary before reading snapshot limits."""
    if not isinstance(view, VocabularyView) or (
        budget is not None and not isinstance(budget, Budget)
    ):
        raise InputError("Expected a VocabularyView and optional Budget")
    return view._operation_budget(budget)


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
        or cursor._source is not view._vocabulary
        or cursor._scopes != tuple(scope for scope, _ in view._scopes)
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
        else VocabularyCursor._at(
            view._vocabulary,
            tuple(scope for scope, _ in view._scopes),
            contract,
            position,
            phase,
        ),
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
        return view._vocabulary._browse_terms(
            view._reader, view._prepare_index(allowance), allowance
        )

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
        return view._prepare_index(allowance).edges

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
    if order != "rank-term-field-language-value-scope":
        raise InputError(
            "Search pages require rank-term-field-language-value-scope ordering"
        )
    selected = _operation(view, budget)
    search = _Search.prepare(query, fields, languages, exact, casefold, selected)

    def candidates(allowance: Budget) -> Sequence[_LexicalCandidate]:
        """Share prepared source positions without retaining query matches."""
        nonlocal search
        search = search.admit(allowance)
        return view._vocabulary._browse_lexical(
            view._reader, view._prepare_index(allowance), allowance
        )

    def include(
        candidate: _LexicalCandidate, phase: int, allowance: Budget
    ) -> SearchMatch | None:
        """Use the admitted plan after preparation completes within this page."""
        return search.match(candidate, phase, allowance)

    return _page(
        view,
        candidates,
        include,
        (
            "terms.search.page",
            order,
            query,
            search.fields,
            search.languages,
            exact,
            casefold,
        ),
        page_size=page_size,
        cursor=cursor,
        budget=selected,
        cancel=cancel,
        phases=3,
    )
