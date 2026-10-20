"""Public dictionary annotation keeps ambiguity and original character offsets."""

from threading import Event
from typing import Literal as Boundary
from typing import cast

import pytest
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.lexical import (
    LexicalIndex,
    annotate_text,
    prepare_lexical_index,
    prepare_mention_index,
)
from bijux_ontology.vocabulary.sources.view import Vocabulary

EX = Namespace("https://example.org/mentions/")


def _index(values: tuple[str, ...], *, casefold: bool = False) -> LexicalIndex:
    """Prepare the same public lexical index used by autocomplete and suggestions."""
    graph = Graph()
    for number, value in enumerate(values):
        graph.add((EX[str(number)], RDF.type, OWL.Class)).add(
            (EX[str(number)], RDFS.label, Literal(value))
        )
    result = prepare_lexical_index(
        inspect_vocabulary(graph), fields=(str(RDFS.label),), casefold=casefold
    )
    assert result.report.complete and result.index is not None
    return result.index


def test_overlaps_homonyms_synonym_roles_and_explicit_selected_scope() -> None:
    """Nested names and equal labels stay candidates; excluded roles cannot leak."""
    local, imported = Graph(), Graph()
    for term, label in ((EX.A, "heart"), (EX.B, "heart"), (EX.C, "heart failure")):
        local.add((term, RDF.type, SKOS.Concept)).add(
            (term, SKOS.prefLabel, Literal(label, lang="en"))
        )
    local.add((EX.A, SKOS.altLabel, Literal("cardiac", lang="en"))).add(
        (EX.A, SKOS.altLabel, Literal("coeur", lang="fr"))
    )
    imported.add((EX.Imported, RDF.type, SKOS.Concept)).add(
        (EX.Imported, SKOS.prefLabel, Literal("heart", lang="en"))
    )
    prepared = prepare_lexical_index(
        inspect_vocabulary(Vocabulary(local, (imported,)), scopes=("local",)),
        fields=(str(SKOS.prefLabel), str(SKOS.altLabel)),
        languages=("en",),
    )
    assert prepared.index is not None
    text = "heart failure; cardiac; coeur"
    result = annotate_text(prepared.index, text)
    assert result.report.verified and result.text == text
    assert [(m.start, m.end) for m in result.mentions] == [(0, 5), (0, 13), (15, 22)]
    assert {m.term.iri for m in result.mentions[0].candidates} == {str(EX.A), str(EX.B)}
    synonym = result.mentions[2].candidates[0]
    assert (synonym.field, synonym.language, synonym.scope) == (
        str(SKOS.altLabel),
        "en",
        "local",
    )
    assert all(
        candidate.rank == 0 for m in result.mentions for candidate in m.candidates
    )
    assert not annotate_text(prepared.index, "hearts").mentions
    assert (
        len(annotate_text(prepared.index, "hearts", boundary="substring").mentions) == 1
    )


def test_unicode_case_expansion_and_exact_composition_preserve_original_spans() -> None:
    """Expanded folds cannot create partial-character hits or move later offsets."""
    index = _index(("Straße", "s", "Café", "Cafe\u0301", "i"), casefold=True)
    text = "🙂 STRASSE / Straße / ß / Café / Cafe\u0301 / İ"
    result = annotate_text(index, text)
    assert result.report.verified and result.text == text
    spans = [text[m.start : m.end] for m in result.mentions]
    assert spans == ["STRASSE", "Straße", "Café", "Cafe\u0301"]
    assert [m.start for m in result.mentions] == [2, 12, 25, 32]
    assert not annotate_text(
        _index(("s",), casefold=True), "ß", boundary="substring"
    ).mentions
    assert not annotate_text(
        _index(("i",), casefold=True), "İ", boundary="substring"
    ).mentions
    assert not annotate_text(_index(("Café",)), "Cafe\u0301").mentions
    assert not annotate_text(_index(("a",)), "a\u0301").mentions
    assert not annotate_text(_index(("Straße",)), "STRASSE").mentions


def test_interrupted_capture_keeps_complete_ambiguous_mentions() -> None:
    """Shared budgets/cancellation qualify coverage and retain completed hit groups."""
    index = _index(("heart", "heart", "heart failure", "failure"))
    text = "heart failure heart"
    full_budget = Budget()
    full = annotate_text(index, text, budget=full_budget)
    assert full.report.verified and len(full.mentions) == 4
    work = full_budget.limits.max_work - full_budget.remaining("work")
    saw_prefix = False
    for cap in range(1, work, max(1, work // 25)):
        result = annotate_text(index, text, budget=Budget(Limits(max_work=cap)))
        assert not result.report.verified and result.report.outcome == "exhausted"
        assert result.mentions == full.mentions[: len(result.mentions)]
        saw_prefix |= bool(result.mentions)
        assert all(
            len(m.candidates) == 2 for m in result.mentions if m.end - m.start == 5
        )
    assert saw_prefix
    event = Event()
    event.set()
    assert annotate_text(index, text, cancel=event).report.outcome == "exhausted"
    for limits in (
        Limits(max_bytes=2),
        Limits(max_states=2),
        Limits(max_total_bytes=10),
    ):
        assert (
            annotate_text(index, text, budget=Budget(limits)).report.outcome
            == "exhausted"
        )
    assert annotate_text(index, text).mentions == full.mentions


def test_empty_text_dictionary_and_invalid_boundary_contract() -> None:
    """A complete empty extraction is useful evidence, never a semantic annotation."""
    index = _index(())
    assert annotate_text(index, "word").report.complete
    assert not annotate_text(index, "word").mentions
    assert annotate_text(index, "").report.complete
    with pytest.raises(InputError):
        annotate_text(
            index, "word", boundary=cast("Boundary['word', 'substring']", "tokens")
        )


def test_prepared_failure_transitions_bound_shared_prefix_scanning() -> None:
    limits = Limits(
        max_work=20_000_000, max_states=1_000_000, max_total_bytes=300_000_000
    )
    graph = Graph()
    for number in range(1, 301):
        graph.add((EX[str(number)], RDF.type, OWL.Class))
        graph.add((EX[str(number)], RDFS.label, Literal("a" * number + "b")))
    base = prepare_lexical_index(
        inspect_vocabulary(Vocabulary(graph, limits=limits)), fields=(str(RDFS.label),)
    )
    assert base.index is not None
    refused = prepare_mention_index(base.index, budget=Budget(Limits(max_states=1)))
    assert refused.index is None and refused.report.outcome == "exhausted"
    prepared = prepare_mention_index(base.index)
    assert prepared.index is not None and prepared.report.verified
    text = "a" * 10_000 + "b"
    budget = Budget(Limits(max_work=500_000))
    result = annotate_text(prepared.index, text, boundary="substring", budget=budget)
    assert result.report.verified and len(result.mentions) == 300
    assert [(mention.start, mention.end) for mention in result.mentions] == [
        (start, len(text)) for start in range(9700, 10000)
    ]
    assert budget.remaining("work") > 0
    assert (
        annotate_text(base.index, "ab", boundary="substring").mentions
        == annotate_text(prepared.index, "ab", boundary="substring").mentions
    )
    event = Event()
    event.set()
    assert prepare_mention_index(base.index, cancel=event).index is None
    assert annotate_text(prepared.index, "ab", boundary="substring").report.verified
