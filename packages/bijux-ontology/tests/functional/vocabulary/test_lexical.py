"""Owned lexical alternatives preserve evidence and avoid repeated inventory work."""

from dataclasses import replace
from threading import Event
from typing import cast

import pytest
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.vocabulary.browsing import search_page
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.lexical import (
    LexicalIndex,
    autocomplete,
    prepare_lexical_index,
    suggest,
)
from bijux_ontology.vocabulary.sources.view import Vocabulary

EX = Namespace("https://example.org/lexical/")


def _term(graph: Graph, name: str, text: str, language: str | None = "en") -> None:
    """Author ordinary source declarations and exact lexical annotations."""
    graph.add((EX[name], RDF.type, OWL.Class)).add(
        (EX[name], RDFS.label, Literal(text, lang=language))
    )


def test_prefix_and_one_edit_preserve_homonyms_fields_languages_and_roles() -> None:
    """Equal labels remain alternatives; only selected owned evidence is indexed."""
    local, imported, lookup = Graph(), Graph(), Graph()
    _term(local, "A", "Alpha")
    _term(local, "B", "Alpha")
    _term(local, "French", "Alphabétique", "fr")
    _term(imported, "Imported", "Alpha imported")
    _term(lookup, "Hidden", "Alpha hidden")
    local.add((EX.A, SKOS.altLabel, Literal("Cardiac", lang="en")))
    view = inspect_vocabulary(
        Vocabulary(local, (imported,), (lookup,)), scopes=("local",)
    )
    built = prepare_lexical_index(
        view, fields=(URIRef(RDFS.label), str(SKOS.altLabel)), languages=("EN",)
    )
    assert built.index is not None and built.report.verified
    index = built.index
    assert index.scopes == ("local",) and index.languages == ("en",)
    prefix = autocomplete(index, "Alp")
    assert prefix.report.verified and [m.match.term.iri for m in prefix.items] == [
        str(EX.A),
        str(EX.B),
    ]
    assert all(
        m.kind == "prefix"
        and m.edit_distance is None
        and m.match.value == "Alpha"
        and m.match.language == "en"
        and m.match.scope == "local"
        for m in prefix.items
    )
    for query in ("Alphx", "Alpha"[:-1], "Alphaa"):
        typo = suggest(index, query)
        assert typo.report.verified and {m.match.term.iri for m in typo.items} == {
            str(EX.A),
            str(EX.B),
        }
        assert all(m.edit_distance == 1 and m.kind == "one-edit" for m in typo.items)
        assert typo.query == query
    text = "Alpha"
    transposed = text[:1] + text[2] + text[1] + text[3:]
    assert not suggest(index, transposed).items
    assert not suggest(index, "Zlphz").items
    assert not autocomplete(index, "alpha").items
    synonym = suggest(index, "Cardiax")
    assert synonym.report.verified and len(synonym.items) == 1
    assert (
        synonym.items[0].match.field == str(SKOS.altLabel)
        and synonym.items[0].field_priority == 1
    )
    exact = suggest(index, "Alpha")
    assert all(m.kind == "exact" and m.edit_distance == 0 for m in exact.items)
    assert not any(
        m.match.term.iri in {str(EX.Hidden), str(EX.Imported), str(EX.French)}
        for m in exact.items
    )
    assert all(m.match.term.iri != "Alphx" for m in suggest(index, "Alphx").items)


def test_case_policy_identifier_priority_exact_lexicals_and_source_immutability() -> (
    None
):
    """Casefold is explicit, lexical spelling survives and callers cannot mutate it."""
    graph = Graph()
    _term(graph, "Street", "Straße", "de")
    _term(graph, "A", "01", None)
    graph.remove((EX.A, RDFS.label, None)).add(
        (EX.A, RDFS.label, Literal("01", datatype=XSD.integer, normalize=False))
    )
    _term(graph, "B", "1", None)
    owned = Vocabulary(graph)
    view = inspect_vocabulary(owned)
    sensitive = prepare_lexical_index(view, fields=(str(RDFS.label),))
    folded = prepare_lexical_index(view, fields=(str(RDFS.label), "iri"), casefold=True)
    assert sensitive.index is not None and folded.index is not None
    assert not autocomplete(sensitive.index, "STRAS").items
    street = autocomplete(folded.index, "STRAS")
    assert street.report.verified and street.items[0].match.value == "Straße"
    assert suggest(folded.index, "01").items[0].match.value == "01"
    assert (
        autocomplete(folded.index, "https://example.org/lexical/Street")
        .items[0]
        .match.field
        == "iri"
    )
    graph.remove((None, None, None))
    exported = view.graph()
    exported.remove((None, None, None))
    assert autocomplete(folded.index, "STRAS").items == street.items
    with pytest.raises(InputError):
        LexicalIndex()
    with pytest.raises(TypeError):
        replace(folded.index, casefold=False)
    with pytest.raises(TypeError):
        cast(dict[str, object], folded.index._entries)["invented"] = ()


def test_top_order_truncation_empty_scope_and_invalid_contracts() -> None:
    """A complete top set does not claim all homonyms were returned."""
    graph = Graph()
    for n in reversed(range(12)):
        _term(graph, f"A{n:02}", "Shared")
    prepared = prepare_lexical_index(
        inspect_vocabulary(graph), fields=(str(RDFS.label),)
    )
    assert prepared.index is not None
    selected = autocomplete(prepared.index, "Sha", limit=3)
    assert selected.report.verified and selected.truncated
    assert [m.match.term.iri for m in selected.items] == [
        str(EX.A00),
        str(EX.A01),
        str(EX.A02),
    ]
    assert not autocomplete(prepared.index, "Absent").items
    empty = prepare_lexical_index(
        inspect_vocabulary(Graph()), fields=(str(RDFS.label),)
    )
    assert (
        empty.index is not None and empty.report.complete and empty.report.allow_empty
    )
    nothing = autocomplete(empty.index, "word")
    assert nothing.report.complete and not nothing.items and not nothing.truncated
    for kwargs in (
        {"fields": ()},
        {"fields": ("bad field",)},
        {"languages": ("bad_tag",)},
    ):
        with pytest.raises(InputError):
            prepare_lexical_index(inspect_vocabulary(graph), **kwargs)
    for options in ({"limit": 0}, {"limit": True}):
        with pytest.raises(InputError):
            suggest(prepared.index, "Shared", **options)
    with pytest.raises(InputError):
        suggest(prepared.index, "")


def test_preallocation_shared_limits_cancellation_and_interrupted_candidates() -> None:
    """Deletion workspace is paid first; interrupted candidates stay nongreen."""
    graph = Graph()
    _term(graph, "Large", "x" * 3000)
    view = inspect_vocabulary(graph)
    refused = prepare_lexical_index(
        view, fields=(str(RDFS.label),), budget=Budget(Limits(max_total_bytes=500000))
    )
    assert refused.index is None and refused.report.outcome == "exhausted"
    small = Graph()
    for n in range(20):
        _term(small, f"T{n:02}", "Alpha")
    built = prepare_lexical_index(
        inspect_vocabulary(small), fields=(str(RDFS.label),), casefold=True
    )
    assert built.index is not None
    event = Event()
    event.set()
    assert (
        prepare_lexical_index(inspect_vocabulary(small), cancel=event).report.outcome
        == "exhausted"
    )
    assert suggest(built.index, "Alpha", cancel=event).report.outcome == "exhausted"
    oversized = suggest(
        built.index, "x" * 3000, budget=Budget(Limits(max_total_bytes=500000))
    )
    assert oversized.report.outcome == "exhausted" and not oversized.items
    expanded = suggest(built.index, "ΐ", budget=Budget(Limits(max_bytes=4)))
    assert expanded.report.outcome == "exhausted" and not expanded.items
    cuts = []
    for cap in range(100, 5000, 100):
        result = autocomplete(
            built.index, "Al", limit=20, budget=Budget(Limits(max_work=cap))
        )
        assert result.report.outcome in {"exhausted", "agreement"}
        if result.items:
            assert all(m.match.value == "Alpha" for m in result.items)
        cuts.append(result)
    assert any(r.items and not r.report.complete for r in cuts)
    shared = Budget()
    assert suggest(built.index, "Alphx", budget=shared).report.verified
    shared.consume("work", shared.remaining("work"))
    assert autocomplete(built.index, "Al", budget=shared).report.outcome == "exhausted"
    assert autocomplete(built.index, "Al").report.verified


def test_five_thousand_term_repeated_lookup_uses_less_than_full_inventory_work() -> (
    None
):
    """Preparation is explicit once; repeated selective reads use native derivatives."""
    limits = Limits(max_work=30000000, max_states=1000000, max_total_bytes=300000000)
    graph = Graph()
    for n in range(5000):
        _term(graph, f"T{n:05}", f"term{n:05}")
    view = inspect_vocabulary(Vocabulary(graph, limits=limits))
    prep_budget = Budget(limits)
    prepared = prepare_lexical_index(
        view, fields=(str(RDFS.label),), budget=prep_budget
    )
    assert prepared.index is not None and prepared.report.verified
    queries = [f"term{n:05}" for n in (17, 812, 2491, 3702, 4999)]
    indexed = Budget(limits)
    scanned = Budget(limits)
    for query in queries:
        exact = autocomplete(prepared.index, query, budget=indexed)
        typo = suggest(prepared.index, query[:-1] + "x", budget=indexed, limit=20)
        assert exact.report.verified and exact.items[0].match.value == query
        assert typo.report.verified and any(m.match.value == query for m in typo.items)
        scan = search_page(
            view, query, fields=(str(RDFS.label),), page_size=20, budget=scanned
        )
        assert scan.report.verified and scan.enumeration_complete
        assert exact.items[0].match.term == scan.items[0].term
    actual_indexed = limits.max_work - indexed.remaining("work")
    actual_scanned = limits.max_work - scanned.remaining("work")
    assert actual_indexed * 10 < actual_scanned
    assert limits.max_work - prep_budget.remaining("work") > actual_indexed
