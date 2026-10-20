"""Exact identities, source scopes, multilingual evidence and owned inspection."""

import tracemalloc
from threading import Event

import pytest
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget, BudgetKind, Limits
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.browsing.pages import terms_page
from bijux_ontology.vocabulary.registry.prefixes import compact_iri, expand_curie
from bijux_ontology.vocabulary.sources.view import Vocabulary

EX = Namespace("https://example.org/")


def test_inventory_lookup_and_batch_resolution_keep_scope_and_identity() -> None:
    local = Graph().add((EX.A, RDF.type, OWL.Class))
    local.add((EX.A, RDFS.subClassOf, EX.Reference))
    lookup = Graph().add((EX.p, RDF.type, OWL.ObjectProperty))
    source = Vocabulary(local, lookup=(lookup,))
    view = inspect_vocabulary(source)
    assert [term.iri for term in view.terms().items] == [str(EX.A)]
    assert view.lookup(str(EX.Reference)).status == "referenced"
    assert view.lookup(str(EX.Missing)).status == "absent"
    assert view.lookup(str(EX.p)).status == "absent"
    all_sources = inspect_vocabulary(source, scopes=("local", "lookup"))
    assert all_sources.lookup(str(EX.p)).scopes == ("lookup[0]",)
    resolved = view.resolve(("ex:A", "ex:Missing", "ex:A"), prefixes={"ex": str(EX)})
    assert [term.status for term in resolved.items] == [
        "declared",
        "absent",
        "declared",
    ]
    assert expand_curie(
        compact_iri(str(EX.A), {"ex": str(EX)}), {"ex": str(EX)}
    ) == str(EX.A)
    with pytest.raises(InputError, match="Unknown prefix"):
        expand_curie("missing:A", {"ex": str(EX)})
    exported = view.graph()
    exported.remove((None, None, None))
    local.remove((None, None, None))
    assert view.lookup(str(EX.A)).status == "declared"
    assert len(view.graph()) == 2


def test_language_preferences_and_ambiguous_search_retain_exact_evidence() -> None:
    graph = Graph()
    for term in (EX.A, EX.B):
        graph.add((term, RDF.type, SKOS.Concept))
        graph.add((term, SKOS.prefLabel, Literal("Image", lang="en")))
    graph.add((EX.A, RDFS.label, Literal("Bild", lang="sv")))
    graph.add((EX.A, RDFS.label, Literal("Avbildning", lang="sv")))
    graph.add((EX.A, SKOS.notation, Literal("01", datatype=XSD.string)))
    view = inspect_vocabulary(graph)
    labels = view.preferred_label(str(EX.A), languages=("sv", "en"))
    assert {label.value for label in labels.preferred} == {"Bild", "Avbildning"}
    assert len(labels.alternatives) == 3
    matches = view.search("image", casefold=True, languages=("en",))
    assert [match.term.iri for match in matches.items] == [str(EX.A), str(EX.B)]
    assert all(match.rank == 1 and match.language == "en" for match in matches.items)
    notation = view.metadata(str(EX.A)).items[0]
    assert notation.value == "01" and notation.datatype == str(XSD.string)


def test_relationships_deprecation_and_summary_expose_asserted_facts() -> None:
    graph = Graph().add((EX.A, RDF.type, OWL.Class))
    graph.add((EX.A, RDFS.subClassOf, EX.B))
    graph.add((EX.A, OWL.deprecated, Literal(True)))
    graph.add((EX.A, OWL.deprecated, Literal(False)))
    graph.add((EX.A, EX.replacedBy, EX.B))
    graph.add((EX.A, EX.replacedBy, EX.C))
    view = inspect_vocabulary(graph)
    assert view.structure(str(EX.A)).named[0].object == EX.B
    incoming = view.relationships(str(EX.B), direction="incoming")
    assert {edge.predicate for edge in incoming.items} == {
        RDFS.subClassOf,
        EX.replacedBy,
    }
    deprecated = view.deprecation(
        str(EX.A), replacement_predicates=(str(EX.replacedBy),)
    )
    assert deprecated.state == "conflicting" and len(deprecated.replacements) == 2
    description = view.describe(str(EX.A), replacement_predicates=(str(EX.replacedBy),))
    assert description.term.iri == str(EX.A)
    assert description.deprecation == deprecated
    assert view.summary().declared == 1 and view.summary().statements == 6


def test_nonmatching_search_and_filtered_iteration_still_obey_work_limits() -> None:
    graph = Graph().add((EX.A, RDF.type, OWL.Class))
    graph.add((EX.B, RDF.type, OWL.Class))
    view = inspect_vocabulary(graph, limits=Limits(max_work=1))
    inventory = view.terms(kinds=(str(OWL.DatatypeProperty),))
    assert not inventory.items and inventory.report.outcome == "exhausted"
    search = view.search("absent")
    assert not search.items and search.report.outcome == "exhausted"
    assert not search.report.verified


def test_predicate_selection_does_not_copy_large_unselected_iris() -> None:
    unselected = URIRef("urn:" + "x" * 1_000_000)
    graph = Graph().add((EX.A, unselected, EX.C))
    graph.add((EX.A, RDFS.subClassOf, EX.B))
    view = inspect_vocabulary(graph)
    tracemalloc.start()
    try:
        selected = view.relationships(str(EX.A), predicates=(str(RDFS.subClassOf),))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert selected.report.verified
    assert [(edge.predicate, edge.object) for edge in selected.items] == [
        (RDFS.subClassOf, EX.B)
    ]
    assert peak < 100_000


def test_bulk_index_limits_cancel_and_cached_admission_are_atomic() -> None:
    graph = Graph()
    for index in range(300):
        graph.add((EX[f"Item{index}"], RDF.type, OWL.Class))
    source = Vocabulary(graph)
    view = inspect_vocabulary(source)
    for limits in (Limits(max_states=1), Limits(max_total_bytes=1)):
        result = view.terms(budget=Budget(limits))
        assert result.report.outcome == "exhausted"
        assert not source._indexes
    cancel = Event()

    class InterruptedBudget(Budget):
        """Cancel after part of the index has been built, before publication."""

        def consume(self, kind: BudgetKind, amount: int = 1) -> None:
            super().consume(kind, amount)
            if (
                kind == "states"
                and self.remaining("states") < self.limits.max_states - 32
            ):
                cancel.set()

    stopped = terms_page(view, budget=InterruptedBudget(), cancel=cancel)
    assert stopped.report.outcome == "exhausted" and not source._indexes
    cancel.clear()
    assert view.terms().report.verified
    retained = source._indexes[("local",)]
    assert not view.terms(budget=Budget(Limits(max_states=1))).report.verified
    assert source._indexes[("local",)] is retained
    assert view.terms().report.verified


def test_targeted_inspection_avoids_full_index_and_agrees_with_bulk_roles() -> None:
    local = Graph()
    for index in range(2000):
        local.add((EX[f"Item{index}"], RDF.type, OWL.Class))
    local.add((EX.Item0, EX.predicate, Literal("value", datatype=EX.Datatype)))
    imported = Graph().add((EX.Item0, RDF.type, OWL.Class))
    lookup = Graph().add((EX.Lookup, RDF.type, OWL.Class))
    source = Vocabulary(local, (imported,), (lookup,))
    view = inspect_vocabulary(source)
    selected = view.lookup(str(EX.Item0), budget=Budget(Limits(max_work=100)))
    assert selected.scopes == ("import[0]", "local") and not source._indexes
    adjacent = view.relationships(str(EX.Item0))
    assert len(adjacent.items) == 3 and not source._indexes
    index = view._prepare_index(Budget(Limits(max_work=20_000_000)))
    for iri in (EX.Item0, EX.predicate, EX.Datatype, EX.Lookup, EX.Absent):
        assert view.lookup(str(iri)) == index.terms.get(
            str(iri), type(selected)(str(iri), "absent", (), ())
        )
    assert adjacent.items == index.outgoing[EX.Item0]


def test_lookup_and_deferred_union_retain_the_callers_allowance() -> None:
    local = Graph().add((EX.A, RDF.type, OWL.Class))
    imported = Graph().add((EX.B, RDF.type, OWL.Class))
    source = Vocabulary(local, (imported,))
    budget = Budget(Limits(max_work=10))
    view = inspect_vocabulary(source, budget=budget)
    budget.consume("work", budget.remaining("work"))
    with pytest.raises(LimitExceeded, match="max_work"):
        _ = view._reader.graph
    assert not source._reads
    fresh = inspect_vocabulary(source)
    assert len(fresh.graph()) == 2
    with pytest.raises(LimitExceeded, match="max_work"):
        view._read()
    with pytest.raises(LimitExceeded, match="max_bytes"):
        fresh.lookup("urn:" + "x" * 10000, budget=Budget(Limits(max_bytes=32)))
