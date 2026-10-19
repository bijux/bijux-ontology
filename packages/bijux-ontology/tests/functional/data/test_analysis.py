"""Finite population identity, distinct ancestor counts and required pair coverage."""

import math
from dataclasses import replace
from threading import Event
from typing import TypedDict, Unpack, cast

import pytest
from rdflib import BNode, Graph, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.data.analysis import (
    AnnotationPopulation,
    EntityAnnotation,
    PopulationRead,
    prepare_annotation_population,
    resnik_similarity,
    resnik_term_sets,
)
from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.vocabulary.inspection import VocabularyView, inspect_vocabulary
from bijux_ontology.vocabulary.relation_paths import RelationInverse, RelationStep
from bijux_ontology.vocabulary.restrictions.types import RestrictionProjection

EX = Namespace("urn:annotation-analysis:")
ISA = RelationStep(str(RDFS.subClassOf), "class")
TERMS = tuple(
    EX[name]
    for name in ("A", "B", "C", "D1", "D2", "M", "Root", "Zero", "Disconnected")
)
ROWS = tuple(
    EntityAnnotation(entity, EX[term])
    for entity, term in (
        ("e1", "A"),
        ("e1", "A"),
        ("e2", "B"),
        ("e3", "C"),
        ("e4", "C"),
    )
)


def _graph() -> Graph:
    graph = Graph(identifier=EX.graph)
    for term in TERMS:
        graph.add((term, RDF.type, OWL.Class))
    for child, parent in (
        ("A", "D1"),
        ("A", "D2"),
        ("D1", "M"),
        ("D2", "M"),
        ("B", "M"),
        ("M", "Root"),
        ("C", "Root"),
        ("Zero", "Root"),
    ):
        graph.add((EX[child], RDFS.subClassOf, EX[parent]))
    return graph


class _ReadOptions(TypedDict, total=False):
    view: VocabularyView
    annotations: tuple[EntityAnnotation, ...]
    terms: tuple[URIRef, ...]
    background: tuple[str, ...]
    propagation: tuple[RelationStep | RelationInverse, ...]
    corpus_id: str
    annotation_snapshot_id: str
    hierarchy_regime: str
    limits: Limits
    budget: Budget
    cancel: Event


def _read(
    graph: Graph | None = None, **options: Unpack[_ReadOptions]
) -> PopulationRead:
    return prepare_annotation_population(
        options.get("view") or inspect_vocabulary(_graph() if graph is None else graph),
        options.get("annotations", ROWS),
        terms=options.get("terms", TERMS),
        background=options.get("background", ("e1", "e2", "e3", "e4")),
        propagation=options.get("propagation", (ISA,)),
        corpus_id=options.get("corpus_id", "supplied-corpus"),
        annotation_snapshot_id=options.get("annotation_snapshot_id", "snapshot"),
        hierarchy_regime=options.get("hierarchy_regime", "asserted"),
        limits=options.get("limits"),
        budget=options.get("budget"),
        cancel=options.get("cancel"),
    )


def _population(
    graph: Graph | None = None, **options: Unpack[_ReadOptions]
) -> AnnotationPopulation:
    result = _read(graph, **options)
    assert result.report.verified, result.report.to_dict()
    assert result.population is not None
    return result.population


def test_distinct_population_resnik_ties_and_explicit_set_formula() -> None:
    population = _population()
    assert population.members[EX.M] == frozenset(("e1", "e2"))
    assert len(population.members[EX.Root]) == 4
    assert population.duplicate_annotations == 1 and population.annotations == ROWS
    pair = resnik_similarity(population, EX.A, EX.B)
    assert pair.report.verified and pair.score == 1.0
    assert [(row.term, row.count, row.background_count) for row in pair.ancestors] == [
        (EX.M, 2, 4)
    ]
    self_pair = resnik_similarity(population, EX.A, EX.A)
    assert {row.term for row in self_pair.ancestors} == {EX.A, EX.D1, EX.D2}
    assert self_pair.score == 2.0
    sets = resnik_term_sets(population, (EX.A, EX.B, EX.A), (EX.A,))
    assert sets.report.verified and sets.score == 1.75
    assert len(sets.pairs) == 2 and sets.left == (EX.A, EX.B)
    assert sets.left_best == ((EX.A, EX.A), (EX.B, EX.A))
    assert sets.right_best == ((EX.A, EX.A),)
    with pytest.raises(TypeError):
        population.members[EX.M] = frozenset()  # type: ignore[index]
    with pytest.raises(InputError):
        AnnotationPopulation()


def test_counted_identity_changes_only_with_real_analytical_inputs() -> None:
    view = inspect_vocabulary(_graph())

    def read(
        rows: tuple[EntityAnnotation, ...] = ROWS,
        *,
        terms: tuple[URIRef, ...] = TERMS,
        background: tuple[str, ...] = ("e1", "e2", "e3", "e4"),
        propagation: tuple[RelationStep | RelationInverse, ...] = (ISA,),
        annotation_snapshot_id: str = "snapshot",
        hierarchy_regime: str = "asserted",
    ) -> AnnotationPopulation:
        return _population(
            view=view,
            annotations=rows,
            terms=terms,
            background=background,
            propagation=propagation,
            corpus_id="corpus",
            annotation_snapshot_id=annotation_snapshot_id,
            hierarchy_regime=hierarchy_regime,
        )

    baseline = read()
    reordered = read(
        tuple(reversed(ROWS[:-1])) + (ROWS[-1],), terms=tuple(reversed(TERMS))
    )
    assert reordered.identity == baseline.identity
    dedup = read(tuple(dict.fromkeys(ROWS)))
    assert dedup.identity == baseline.identity
    changed = read(background=("e1", "e2", "e3", "e4", "unannotated"))
    assert changed.unannotated == ("unannotated",)
    assert changed.identity != baseline.identity
    assert resnik_similarity(changed, EX.A, EX.B).score == -math.log2(2 / 5)
    assert read(annotation_snapshot_id="different").identity != baseline.identity
    assert read(hierarchy_regime="caller-inferred").identity != baseline.identity
    graph = _graph()
    for term in (EX.A, EX.B, EX.C):
        graph.add((term, EX.selected_parent, EX.Root))
    alternative = _population(
        graph=graph,
        propagation=(RelationStep(str(EX.selected_parent), "asserted"),),
    )
    assert resnik_similarity(alternative, EX.A, EX.B).score == 0.0
    assert (
        alternative.identity.hierarchy_fingerprint
        != baseline.identity.hierarchy_fingerprint
    )
    assert (
        read(propagation=(RelationStep(str(RDFS.subClassOf), "asserted"),)).identity
        != baseline.identity
    )


def test_unknown_zero_disconnected_cycle_and_missing_parent_never_become_scores() -> (
    None
):
    population = _population()
    for left, right in ((EX.Zero, EX.A), (EX.Disconnected, EX.A), (EX.Unknown, EX.A)):
        pair = resnik_similarity(population, left, right)
        assert pair.score is None and pair.report.outcome == "unverified"
    # Positive annotation cannot create a common ancestor between disjoint terms.
    disjoint = _population(
        annotations=ROWS + (EntityAnnotation("e1", EX.Disconnected),)
    )
    assert (
        resnik_similarity(disjoint, EX.Disconnected, EX.A).report.issues[0].code
        == "analysis.disconnected"
    )
    sets = resnik_term_sets(disjoint, (EX.A, EX.Disconnected), (EX.B,))
    assert sets.score is None and len(sets.pairs) == 2 and not sets.report.complete
    graph = _graph()
    graph.add((EX.Root, RDFS.subClassOf, EX.A))
    assert _read(graph).population is None
    missing = _read(terms=tuple(term for term in TERMS if term != EX.Root))
    assert missing.population is None and any(
        issue.code == "analysis.parent_unknown" for issue in missing.report.issues
    )
    unknown = _read(annotations=ROWS + (EntityAnnotation("e1", EX.Unknown),))
    assert unknown.population is None and unknown.unknown_annotations == (
        EntityAnnotation("e1", EX.Unknown),
    )
    assert (
        _read(annotations=ROWS + (EntityAnnotation("outside", EX.A),)).report.outcome
        == "input_error"
    )


def test_typed_oriented_native_projection_preserves_original_witnesses() -> None:
    graph = Graph(identifier=EX.graph)
    for term in (EX.A, EX.B):
        graph.add((term, RDF.type, OWL.Class))
    node = BNode()
    for triple in (
        (EX.A, RDFS.subClassOf, node),
        (node, RDF.type, OWL.Restriction),
        (node, OWL.onProperty, EX.part),
        (node, OWL.someValuesFrom, EX.B),
    ):
        graph.add(triple)
    positive = _population(
        graph=graph,
        terms=(EX.A, EX.B),
        annotations=(EntityAnnotation("e1", EX.A),),
        propagation=(RelationStep(str(EX.part), "existential"),),
    )
    assert positive.members[EX.B] == frozenset(("e1",))
    evidence = positive.witnesses[0].evidence
    assert isinstance(evidence, RestrictionProjection)
    assert evidence.quantifier == OWL.someValuesFrom
    graph.remove((node, OWL.someValuesFrom, EX.B))
    graph.add((node, OWL.allValuesFrom, EX.B))
    unknown = _read(
        graph,
        terms=(EX.A, EX.B),
        annotations=(EntityAnnotation("e1", EX.A),),
        propagation=(RelationStep(str(EX.part), "existential"),),
    )
    assert unknown.population is None and unknown.report.outcome == "unverified"
    inverse_graph = Graph(identifier=EX.graph)
    inverse_graph.add((EX.B, EX.child, EX.A))
    inverse = _population(
        graph=inverse_graph,
        terms=(EX.A, EX.B),
        annotations=(EntityAnnotation("e1", EX.A),),
        propagation=(RelationInverse(RelationStep(str(EX.child), "asserted")),),
    )
    assert inverse.members[EX.B] == frozenset(("e1",)) and inverse.witnesses[0].inverse


def test_shared_bounds_cancel_and_cross_product_withhold_unpaid_results() -> None:
    population = _population()
    for kind in ("max_work", "max_states", "max_total_bytes"):
        limits = replace(Limits(), **{kind: 1})
        assert (
            resnik_similarity(population, EX.A, EX.B, limits=limits).report.outcome
            == "exhausted"
        )
        assert _read(limits=limits).population is None
    shared = Budget()
    resnik_term_sets(population, (EX.A, EX.B), (EX.A, EX.B), budget=shared)
    needed = 1_000_000 - shared.remaining("work")
    cut = resnik_term_sets(
        population,
        (EX.A, EX.B),
        (EX.A, EX.B),
        limits=replace(Limits(), max_work=needed - 1),
    )
    assert cut.score is None and cut.report.outcome == "exhausted"
    paid = Budget()
    rows = ROWS + (EntityAnnotation("e1", EX.Unknown),)
    _read(annotations=rows, budget=paid)
    consumed = paid.limits.max_work - paid.remaining("work")
    prefix = _read(annotations=rows, limits=replace(Limits(), max_work=consumed // 2))
    assert prefix.report.outcome == "exhausted"
    assert any(
        issue.code == "analysis.annotation_unknown" for issue in prefix.report.issues
    )
    cancelled = Event()
    cancelled.set()
    assert _read(cancel=cancelled).report.outcome == "exhausted"
    assert resnik_similarity(population, EX.A, EX.B, cancel=cancelled).score is None
    assert resnik_similarity(population, EX.A, EX.B).score == 1.0
    assert (
        resnik_similarity(
            cast(AnnotationPopulation, object()), EX.A, EX.B
        ).report.outcome
        == "input_error"
    )
