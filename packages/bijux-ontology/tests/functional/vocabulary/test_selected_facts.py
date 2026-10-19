"""Exact selected assertions retain graph ownership and occurrence roles."""

from collections.abc import Sequence
from typing import cast

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.rdf.terms import term_key
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.comparison import compare_vocabularies
from bijux_ontology.vocabulary.facts import (
    FactGraph,
    FactPredicate,
    FactSelection,
    compare_selected_facts,
)
from bijux_ontology.vocabulary.sources.loading import load_vocabulary

EX = Namespace("urn:selected:")


def _selection(*predicates: str) -> FactSelection:
    """Qualify custom property endpoints explicitly in the fixture contract."""
    return FactSelection(
        tuple(
            FactPredicate(
                str(predicate), subject_role="individual", object_role="individual"
            )
            for predicate in predicates
        )
    )


def test_arbitrary_assertions_and_exact_literals_are_selected_without_legacy_cost() -> (
    None
):
    before = Graph().add((EX.s, EX.link, EX.old))
    after = Graph().add((EX.s, EX.link, EX.new))
    assert compare_vocabularies(before, after).changes == ()
    assert not compare_selected_facts(before, after, _selection(EX.other)).differences
    result = compare_selected_facts(before, after, _selection(EX.link))
    assert result.report.complete and len(result.differences) == 2
    assert {item.status for item in result.differences} == {"added", "removed"}
    for old, new in (
        (
            Literal("01", datatype=XSD.integer, normalize=False),
            Literal("1", datatype=XSD.integer, normalize=False),
        ),
        (Literal("name", lang="en"), Literal("name", lang="fr")),
        (
            Literal("1", datatype=XSD.string),
            Literal("1", datatype=XSD.integer, normalize=False),
        ),
    ):
        result = compare_selected_facts(
            Graph().add((EX.s, EX.label, old)),
            Graph().add((EX.s, EX.label, new)),
            FactSelection((FactPredicate(str(EX.label), subject_role="individual"),)),
        )
        assert len(result.differences) == 2
        assert {term_key(item.fact.statement[2]) for item in result.differences} == {
            term_key(old),
            term_key(new),
        }
    for datatype, language, expected_type in (
        (None, None, XSD.string),
        (XSD.string, None, XSD.string),
        (None, "en", RDF.langString),
    ):
        scoped = FactSelection(
            (FactPredicate(str(EX.label), subject_role="individual"),),
            entities=(str(expected_type),),
            positions=("datatype",),
        )
        result = compare_selected_facts(
            Graph().add(
                (EX.s, EX.label, Literal("a", datatype=datatype, lang=language))
            ),
            Graph().add(
                (EX.s, EX.label, Literal("b", datatype=datatype, lang=language))
            ),
            scoped,
        )
        assert len(result.differences) == 2
        assert all(
            any(
                o.position == "datatype" and o.term == expected_type
                for o in item.fact.occurrences
            )
            for item in result.differences
        )


def test_named_default_and_plain_graph_ownership_do_not_collapse() -> None:
    before, after = Dataset(), Dataset()
    before.graph(EX.G).add((EX.s, EX.p, EX.o))
    after.graph(EX.H).add((EX.s, EX.p, EX.o))
    left = load_vocabulary(RdfSource(before, union=True))
    right = load_vocabulary(RdfSource(after, union=True))
    result = compare_selected_facts(left, right, _selection(EX.p))
    assert {(item.status, item.fact.graph.name) for item in result.differences} == {
        ("removed", EX.G),
        ("added", EX.H),
    }
    selected = FactSelection(
        _selection(EX.p).predicates, graphs=(FactGraph("named", EX.G),)
    )
    result = compare_selected_facts(left, right, selected)
    assert [(item.status, item.fact.graph.name) for item in result.differences] == [
        ("removed", EX.G)
    ]
    assert not compare_selected_facts(
        left, right, FactSelection(selected.predicates, graphs=())
    ).differences
    dataset = Dataset()
    dataset.default_graph.add((EX.s, EX.p, EX.o))
    default = load_vocabulary(RdfSource(dataset, graph_name="@default"))
    plain = Graph(identifier=EX.G).add((EX.s, EX.p, EX.o))
    result = compare_selected_facts(plain, default, _selection(EX.p))
    assert {item.fact.graph.kind for item in result.differences} == {"graph", "default"}
    pinned = prepare_rdf(RdfSource(before, graph_name=str(EX.G)))
    assert compare_selected_facts(pinned, left, _selection(EX.p)).report.verified
    blank_dataset = Dataset()
    blank_dataset.graph(BNode("named")).add((EX.s, EX.p, EX.o))
    owned = load_vocabulary(RdfSource(blank_dataset, union=True))
    facts = compare_selected_facts(owned, owned, _selection(EX.p))
    name = facts.anonymous[0].before[0].graph.name
    assert isinstance(name, BNode)
    foreign = FactSelection(
        _selection(EX.p).predicates, graphs=(FactGraph("named", BNode(str(name))),)
    )
    unresolved = compare_selected_facts(owned, owned, foreign)
    assert not unresolved.report.verified and not unresolved.differences
    assert any(
        i.code == "vocabulary.fact_graph_unverified" for i in unresolved.report.issues
    )


def test_source_and_endpoint_selection_remain_explicit() -> None:
    local = Graph().add((EX.ontology, RDF.type, OWL.Ontology))
    left = load_vocabulary(local, lookup=(Graph().add((EX.s, EX.p, EX.old)),))
    right = load_vocabulary(local, lookup=(Graph().add((EX.s, EX.p, EX.new)),))
    assert not compare_selected_facts(left, right, _selection(EX.p)).differences
    selected = FactSelection(
        _selection(EX.p).predicates,
        scopes=("lookup",),
        entities=(str(EX.s),),
        positions=("subject",),
    )
    result = compare_selected_facts(left, right, selected)
    assert len(result.differences) == 2
    assert all(item.fact.source_scope == "lookup[0]" for item in result.differences)
    view = inspect_vocabulary(left, scopes=("local",))
    with pytest.raises(InputError, match="widens"):
        compare_selected_facts(view, right, selected)
    object_selected = FactSelection(
        selected.predicates,
        scopes=selected.scopes,
        entities=(str(EX.s),),
        positions=("object",),
    )
    assert not compare_selected_facts(left, right, object_selected).differences
    declarations = (
        Graph()
        .add((EX.A, RDF.type, OWL.Class))
        .add((EX.B, RDF.type, OWL.NamedIndividual))
    )
    only_class = FactSelection(
        (FactPredicate(str(RDF.type), subject_role="class"),),
        entities=(str(EX.A),),
        positions=("subject",),
    )
    assert compare_selected_facts(
        declarations, declarations, only_class
    ).report.verified
    only_individual = FactSelection(
        only_class.predicates, entities=(str(EX.B),), positions=("subject",)
    )
    with pytest.raises(InputError, match="conflicts"):
        compare_selected_facts(declarations, declarations, only_individual)


def test_punned_and_self_typing_occurrences_keep_distinct_roles() -> None:
    before = Graph().add((EX.C, RDF.type, EX.C)).add((EX.C, SKOS.inScheme, EX.S))
    before.add((EX.C, RDFS.subClassOf, EX.Parent))
    after = (
        Graph()
        .add((EX.C, RDF.type, EX.Other))
        .add((EX.C, SKOS.inScheme, EX.OtherScheme))
    )
    after.add((EX.C, RDFS.subClassOf, EX.OtherParent))
    selection = FactSelection(
        (
            FactPredicate(str(RDF.type)),
            FactPredicate(str(SKOS.inScheme)),
            FactPredicate(str(RDFS.subClassOf)),
        )
    )
    result = compare_selected_facts(before, after, selection)
    self_type = next(
        item.fact
        for item in result.differences
        if item.status == "removed" and item.fact.statement[1] == RDF.type
    )
    assert {(o.position, o.role) for o in self_type.occurrences if o.term == EX.C} == {
        ("subject", "individual"),
        ("object", "class"),
    }
    membership = next(
        item.fact
        for item in result.differences
        if item.fact.statement[1] == SKOS.inScheme
    )
    assert {(o.position, o.role) for o in membership.occurrences if o.term == EX.C} == {
        ("subject", "individual")
    }
    hierarchy = next(
        item.fact
        for item in result.differences
        if item.fact.statement[1] == RDFS.subClassOf
    )
    assert {(o.position, o.role) for o in hierarchy.occurrences if o.term == EX.C} == {
        ("subject", "class")
    }
    with pytest.raises(InputError, match="conflicts"):
        compare_selected_facts(
            before,
            after,
            FactSelection((FactPredicate(str(SKOS.inScheme), subject_role="class"),)),
        )
    unknown = compare_selected_facts(
        Graph().add((EX.s, EX.p, EX.old)),
        Graph().add((EX.s, EX.p, EX.new)),
        FactSelection((FactPredicate(str(EX.p)),)),
    )
    assert len(unknown.differences) == 2 and not unknown.report.complete
    assert any(
        i.code == "vocabulary.fact_role_unverified" for i in unknown.report.issues
    )
    assert any(
        o.role == "unknown"
        for item in unknown.differences
        for o in item.fact.occurrences
    )
    assert compare_selected_facts(
        Graph(), Graph(), FactSelection((FactPredicate(str(RDFS.subClassOf)),))
    ).report.verified
    literal = Graph().add((EX.s, EX.p, Literal("value")))
    assert (
        compare_selected_facts(literal, literal, _selection(EX.p)).report.outcome
        == "unverified"
    )
    exact_annotation = FactSelection(
        (FactPredicate(str(EX.p), subject_role="individual"),)
    )
    assert compare_selected_facts(literal, literal, exact_annotation).report.verified


def test_anonymous_joint_graph_correspondence_preserves_sharing() -> None:
    def document(split: bool, label: str) -> Dataset:
        result = Dataset()
        shared = BNode(label)
        result.graph(EX.G).add((EX.s, EX.p, shared))
        result.graph(EX.H).add((EX.t, EX.p, BNode() if split else shared))
        return result

    before = load_vocabulary(RdfSource(document(False, "left"), union=True))
    renamed = load_vocabulary(RdfSource(document(False, "right"), union=True))
    result = compare_selected_facts(before, renamed, _selection(EX.p))
    assert result.report.verified and result.anonymous[0].report.verified
    assert not result.differences
    changed = load_vocabulary(RdfSource(document(True, "left"), union=True))
    result = compare_selected_facts(before, changed, _selection(EX.p))
    assert result.report.outcome == "unverified" and not result.differences
    assert any(
        i.code == "rdf.structure_changed" for i in result.anonymous[0].report.issues
    )
    assert len(result.anonymous[0].before) == 2
    assert all(fact.occurrences for fact in result.anonymous[0].before)
    shared = prepare_rdf(Graph().add((EX.s, EX.p, BNode())))
    reused = load_vocabulary(shared, lookup=(shared,))
    independent = load_vocabulary(
        prepare_rdf(Graph().add((EX.s, EX.p, BNode()))),
        lookup=(prepare_rdf(Graph().add((EX.s, EX.p, BNode()))),),
    )
    scope = FactSelection(_selection(EX.p).predicates, scopes=("local", "lookup"))
    result = compare_selected_facts(reused, reused, scope)
    assert result.report.verified
    broken = compare_selected_facts(reused, independent, scope)
    assert broken.report.outcome == "unverified"
    assert not broken.anonymous[0].report.verified


def test_incomplete_side_cannot_manufacture_removals_or_erase_prior_scope() -> None:
    before = Graph().add((EX.s, EX.p, EX.o))
    after = Graph()
    selection = _selection(EX.p)
    seen_complete = False
    for work in range(1, 500):
        result = compare_selected_facts(
            before, after, selection, limits=Limits(max_work=work)
        )
        if result.report.complete:
            seen_complete = True
            assert len(result.differences) == 1
        elif result.differences:
            assert result.differences[0].status == "removed"
        else:
            assert result.report.outcome == "exhausted"
    assert seen_complete
    # A completed difference survives exhaustion while retaining the next one.
    result = compare_selected_facts(
        before,
        Graph().add((EX.s, EX.p, EX.new)),
        selection,
        limits=Limits(max_issues=1),
    )
    assert len(result.differences) == 1 and result.report.outcome == "exhausted"
    assert any(i.code == "vocabulary.fact_changed" for i in result.report.issues)
    huge = FactSelection((FactPredicate("urn:" + "x" * 1000),))
    assert (
        compare_selected_facts(
            Graph(), Graph(), huge, budget=Budget(Limits(max_bytes=20))
        ).report.outcome
        == "exhausted"
    )


def test_finite_declarations_own_input_and_reject_invalid_containers() -> None:
    rules = [FactPredicate(str(EX.p))]
    selection = FactSelection(cast(tuple[FactPredicate, ...], rules))
    rules.clear()
    assert len(selection.predicates) == 1
    invalid: tuple[object, ...] = (None, "urn:p", iter(()), ({},))
    for values in invalid:
        with pytest.raises(InputError):
            FactSelection(cast(tuple[FactPredicate, ...], values))
    with pytest.raises(InputError):
        FactGraph("named")
    with pytest.raises(InputError):
        FactSelection(
            (FactPredicate(str(EX.p)),), entities=cast(tuple[str, ...], ({},))
        )
    assert isinstance(selection.predicates, Sequence)
    accepted = FactSelection(
        (FactPredicate(EX.p, subject_role="individual", object_role="individual"),),
        entities=(EX.s,),
    )
    assert type(accepted.predicates[0].iri) is str and type(accepted.entities[0]) is str
    result = compare_selected_facts(
        Graph().add((EX.s, EX.p, EX.old)), Graph().add((EX.s, EX.p, EX.new)), accepted
    )
    assert len(result.differences) == 2
