"""Resumable immutable vocabulary inventories, lexical ranks and stopped pages."""

from threading import Event

import pytest
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.browsing.pages import (
    search_page,
    statements_page,
    terms_page,
)
from bijux_ontology.vocabulary.browsing.results import Edge, SearchMatch
from bijux_ontology.vocabulary.browsing.types import VocabularyCursor
from bijux_ontology.vocabulary.sources.view import Vocabulary

EX = Namespace("https://example.org/")


def test_five_thousand_terms_resume_in_native_order_without_offset_work() -> None:
    graph = Graph()
    expected = [f"{EX}term{index:05}" for index in range(5000)]
    for iri in reversed(expected):
        graph.add((EX[iri.removeprefix(str(EX))], RDF.type, OWL.Class))
    source = Vocabulary(graph)
    view = inspect_vocabulary(source)
    # One shared counter proves linear consumption across all pages. Offset
    # rescans would exceed this allowance after only a few pages.
    assert terms_page(view, page_size=1).report.verified
    budget = Budget(Limits(max_work=5000))
    cursor = None
    actual: list[str] = []
    pages = 0
    while True:
        page = terms_page(view, cursor=cursor, page_size=37, budget=budget)
        pages += 1
        assert page.report.verified and page.report.complete
        actual.extend(term.iri for term in page.items)
        if page.enumeration_complete:
            assert page.next_cursor is None
            break
        assert page.next_cursor is not None
        cursor = page.next_cursor
    assert pages == 136 and actual == expected
    assert len(set(actual)) == 5000
    assert (
        terms_page(view, page_size=37).items
        == terms_page(inspect_vocabulary(source), page_size=37).items
    )


def test_cursor_contract_rejects_replacement_filter_or_operation_changes() -> None:
    graph = Graph().add((EX.A, RDF.type, OWL.Class))
    graph.add((EX.B, RDF.type, OWL.Class))
    source = Vocabulary(graph)
    view = inspect_vocabulary(source)
    first = terms_page(view, page_size=1)
    assert first.report.verified and not first.enumeration_complete
    cursor = first.next_cursor
    assert cursor is not None
    # Compatible readers share the actual owned index, not just equal contents.
    second = terms_page(inspect_vocabulary(source), cursor=cursor, page_size=7)
    assert [term.iri for term in second.items] == [str(EX.B)]
    assert second.enumeration_complete
    for invalid_view, options in (
        (inspect_vocabulary(Vocabulary(graph)), {}),
        (view, {"kinds": (str(OWL.Class),)}),
        (view, {"namespaces": (str(EX),)}),
        (view, {"order": "descending"}),
    ):
        with pytest.raises(InputError):
            terms_page(invalid_view, cursor=cursor, **options)
    with pytest.raises(InputError, match="Cursor"):
        statements_page(view, cursor=cursor)
    with pytest.raises(InputError, match="issued"):
        VocabularyCursor()
    graph.remove((None, None, None))
    exported = view.graph()
    exported.remove((None, None, None))
    assert terms_page(view, cursor=cursor).items == second.items


def test_scoped_statements_and_ranked_lexical_results_match_existing_reader() -> None:
    local = Graph()
    for term in (EX.A, EX.B, EX.C):
        local.add((term, RDF.type, SKOS.Concept))
    local.add((EX.A, RDFS.label, Literal("Image", lang="en")))
    local.add((EX.A, RDFS.label, Literal("Image", lang="sv")))
    local.add((EX.B, SKOS.altLabel, Literal("image plus", lang="en")))
    local.add(
        (EX.C, SKOS.notation, Literal("01", datatype=XSD.integer, normalize=False))
    )
    imported = Graph().add((EX.A, RDF.type, SKOS.Concept))
    imported.add((EX.A, RDFS.label, Literal("Image", lang="en")))
    view = inspect_vocabulary(Vocabulary(local, imports=(imported,)))
    edges: list[Edge] = []
    cursor = None
    while True:
        page = statements_page(view, cursor=cursor, page_size=2)
        assert page.report.verified
        edges.extend(page.items)
        if page.enumeration_complete:
            break
        cursor = page.next_cursor
    assert tuple(edges) == view.all_statements().items
    assert {
        edge.scope for edge in edges if edge.object == Literal("Image", lang="en")
    } == {"local", "import[0]"}
    for query, options in (
        ("image", {"casefold": True}),
        (str(EX.A), {}),
        ("01", {"exact": True, "fields": (str(SKOS.notation),)}),
        ("Image", {"languages": ("EN",), "exact": True}),
        ("absent", {}),
    ):
        matches: list[SearchMatch] = []
        cursor = None
        while True:
            found = search_page(view, query, page_size=1, cursor=cursor, **options)
            assert found.report.outcome in ("agreement", "not_applicable")
            matches.extend(found.items)
            if found.enumeration_complete:
                break
            cursor = found.next_cursor
        assert tuple(matches) == view.search(query, **options).items
    first = search_page(view, "Image", page_size=1)
    assert first.next_cursor is not None
    with pytest.raises(InputError, match="Cursor"):
        search_page(view, "image", cursor=first.next_cursor)


def test_exhausted_and_cancelled_pages_resume_without_omitting_candidates() -> None:
    graph = Graph()
    for term in (EX.A, EX.B, EX.C):
        graph.add((term, RDF.type, OWL.Class))
    view = inspect_vocabulary(graph)
    assert terms_page(view, page_size=1).report.verified
    stopped = terms_page(view, page_size=2, budget=Budget(Limits(max_work=1)))
    assert [term.iri for term in stopped.items] == [str(EX.A)]
    assert stopped.report.outcome == "exhausted" and not stopped.report.complete
    assert not stopped.enumeration_complete and stopped.next_cursor is not None
    resumed = terms_page(view, page_size=2, cursor=stopped.next_cursor)
    assert [term.iri for term in resumed.items] == [str(EX.B), str(EX.C)]
    assert resumed.enumeration_complete
    # State failure occurs after matching, before retaining the next candidate.
    state_stop = terms_page(view, page_size=1, budget=Budget(Limits(max_states=1)))
    used = Budget(Limits(max_states=1))
    used.consume("states")
    retry = terms_page(view, cursor=state_stop.next_cursor, page_size=1, budget=used)
    assert not retry.items and retry.report.outcome == "exhausted"
    assert (
        terms_page(view, cursor=retry.next_cursor, page_size=1).items
        == resumed.items[:1]
    )
    cancel = Event()
    cancel.set()
    cancelled = terms_page(view, page_size=1, cancel=cancel)
    assert not cancelled.items and not cancelled.enumeration_complete
    assert cancelled.report.outcome == "exhausted" and cancelled.next_cursor is not None
    cancel.clear()
    assert terms_page(
        view, cursor=cancelled.next_cursor, cancel=cancel
    ).enumeration_complete


def test_browsing_preparation_is_lazy_shared_and_bounded() -> None:
    graph = Graph().add((EX.A, RDF.type, OWL.Class))
    graph.add((EX.A, RDFS.label, Literal("Image", lang="en")))
    graph.add((EX.B, RDF.type, OWL.Class))
    source = Vocabulary(graph)
    view = inspect_vocabulary(source)
    assert view.terms().report.verified
    assert view.search("Image").report.verified
    assert not source._term_pages and not source._lexical_pages
    assert statements_page(view, page_size=1).report.verified
    assert not source._term_pages and not source._lexical_pages
    # An incomplete derivative is never published as a reusable positive.
    for limits in (
        Limits(max_work=1),
        Limits(max_states=1),
        Limits(max_total_bytes=1),
    ):
        failed = terms_page(view, page_size=1, budget=Budget(limits))
        assert failed.report.outcome == "exhausted" and failed.next_cursor is not None
        assert not source._term_pages
    first = terms_page(view, page_size=1)
    assert first.report.verified and not source._lexical_pages
    terms = next(iter(source._term_pages.values()))
    compatible = inspect_vocabulary(source)
    reused = terms_page(compatible, page_size=1, budget=Budget(Limits(max_work=1)))
    assert (
        reused.items == first.items and next(iter(source._term_pages.values())) is terms
    )
    failed_lexical = search_page(
        view, "Image", page_size=1, budget=Budget(Limits(max_total_bytes=1024))
    )
    assert failed_lexical.report.outcome == "exhausted" and not source._lexical_pages
    lexical = search_page(view, "Image", page_size=1)
    assert lexical.report.verified
    candidates = next(iter(source._lexical_pages.values()))
    literal = next(
        edge.object
        for edge in view.all_statements().items
        if edge.predicate == RDFS.label
    )
    assert isinstance(literal, Literal)
    assert any(candidate.value is literal for candidate in candidates)
    assert search_page(compatible, "Image", page_size=1).items == lexical.items
    assert next(iter(source._lexical_pages.values())) is candidates


def test_page_text_bounds_and_lexical_cancellation_preserve_continuation() -> None:
    graph = Graph().add((EX.A, RDF.type, OWL.Class))
    graph.add((EX.A, RDFS.label, Literal("Image", lang="en")))
    view = inspect_vocabulary(graph)
    for query in ("x" * 1000, "é" * 6):
        with pytest.raises(LimitExceeded, match="max_bytes"):
            search_page(view, query, budget=Budget(Limits(max_bytes=10)))
    with pytest.raises(LimitExceeded, match="max_bytes"):
        terms_page(
            view, namespaces=("urn:" + "x" * 1000,), budget=Budget(Limits(max_bytes=10))
        )
    with pytest.raises(InputError, match="VocabularyView"):
        terms_page(None)  # type: ignore[arg-type]
    with pytest.raises(InputError, match="Budget"):
        search_page(view, "Image", budget=object())  # type: ignore[arg-type]
    expected = view.search("image", casefold=True).items
    assert search_page(view, "image", casefold=True).items == expected
    # Small page comparison work can stop inside a rank pass even with a warm
    # derivative. The unprocessed candidate is retried with a fresh allowance.
    stopped = search_page(
        view, "image", casefold=True, budget=Budget(Limits(max_work=350))
    )
    assert stopped.report.outcome == "exhausted" and stopped.next_cursor is not None
    resumed = search_page(view, "image", casefold=True, cursor=stopped.next_cursor)
    assert stopped.items + resumed.items == expected and resumed.enumeration_complete
    cancelled = Event()
    cancelled.set()
    stopped = search_page(view, "image", casefold=True, cancel=cancelled)
    assert stopped.report.outcome == "exhausted" and not stopped.enumeration_complete
    cancelled.clear()
    resumed = search_page(
        view, "image", casefold=True, cursor=stopped.next_cursor, cancel=cancelled
    )
    assert resumed.items == expected and resumed.enumeration_complete


def test_page_filters_share_exact_membership_and_bound_prefix_comparisons() -> None:
    graph = Graph()
    for index in range(200):
        graph.add((EX[f"term{index:03}"], RDF.type, OWL.Class))
    view = inspect_vocabulary(graph)
    assert terms_page(view, page_size=1).report.verified
    filters = tuple(f"https://example.org/absent/{index:03}/" for index in range(100))
    stopped = terms_page(
        view, namespaces=filters, budget=Budget(Limits(max_work=15000))
    )
    assert stopped.report.outcome == "exhausted"
    assert not stopped.enumeration_complete and stopped.next_cursor is not None
    resumed = terms_page(view, namespaces=filters, cursor=stopped.next_cursor)
    assert not resumed.items and resumed.enumeration_complete
    # Exact filters are prepared once, rather than rebuilt or linearly scanned
    # for every assertion. No matching item is silently discarded.
    kinds = terms_page(view, kinds=(*filters, str(OWL.Class)), page_size=200)
    assert len(kinds.items) == 200 and kinds.enumeration_complete
    statements = statements_page(
        view, predicates=(*filters, str(RDF.type)), page_size=200
    )
    assert len(statements.items) == 200 and statements.enumeration_complete
