"""Version comparison distinguishes owned facts from consumer compatibility."""

from rdflib import BNode, Graph, Literal, Namespace
from rdflib.collection import Collection
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

from bijux_ontology.execution.limits import Limits
from bijux_ontology.vocabulary.comparison import compare_vocabularies
from bijux_ontology.vocabulary.sources.loading import load_vocabulary

EX = Namespace("https://example.org/vocabulary#")


def test_local_declaration_loss_cannot_be_concealed_by_lookup_copies() -> None:
    original = Graph().add((EX.Image, RDF.type, OWL.Class))
    before = load_vocabulary(original, lookup=[original])
    after = load_vocabulary(Graph(), lookup=[original])
    result = compare_vocabularies(before, after)
    assert result.report.complete and not result.report.verified
    assert [(change.kind, change.iri) for change in result.changes] == [
        ("term-removed", str(EX.Image))
    ]
    assert result.changes[0].before_scopes == ("local",)
    assert len(original) == 1 and len(before.local) == 1


def test_kind_and_multilingual_changes_retain_exact_annotation_values() -> None:
    before = Graph().add((EX.size, RDF.type, OWL.ObjectProperty))
    before.add((EX.size, RDFS.label, Literal("size", lang="EN")))
    before.add((EX.size, RDFS.label, Literal("storlek", lang="sv")))
    before.add((EX.size, RDFS.comment, Literal("text")))
    after = Graph().add((EX.size, RDF.type, OWL.DatatypeProperty))
    after.add((EX.size, RDFS.label, Literal("size", lang="en")))
    after.add((EX.size, RDFS.label, Literal("mått", lang="sv")))
    after.add((EX.size, RDFS.comment, Literal("text", datatype=XSD.string)))
    result = compare_vocabularies(
        before, after, annotation_predicates=(str(RDFS.label), str(RDFS.comment))
    )
    assert result.report.complete
    assert [change.kind for change in result.changes] == ["kinds", "annotation"]
    labels = result.changes[1]
    assert set(labels.before) == {
        Literal("size", lang="EN"),
        Literal("storlek", lang="sv"),
    }
    assert set(labels.after) == {Literal("size", lang="en"), Literal("mått", lang="sv")}
    assert labels.before_scopes == labels.after_scopes == ("local",)


def test_conjunctive_ranges_disjoint_pairs_and_characteristics_are_distinct() -> None:
    before = Graph().add((EX.value, RDF.type, OWL.ObjectProperty))
    before.add((EX.value, RDFS.range, EX.A))
    before.add((EX.value, RDFS.range, EX.B))
    before.add((EX.A, OWL.disjointWith, EX.B))
    after = Graph().add((EX.value, RDF.type, OWL.ObjectProperty))
    after.add((EX.value, RDFS.range, EX.A))
    after.add((EX.value, RDFS.range, EX.C))
    after.add((EX.B, OWL.disjointWith, EX.A))
    after.add((EX.value, RDF.type, OWL.FunctionalProperty))
    result = compare_vocabularies(before, after)
    assert [(change.kind, change.predicate) for change in result.changes] == [
        ("characteristics", str(RDF.type)),
        ("structure", str(RDFS.range)),
    ]
    assert set(result.changes[1].before) == {EX.A, EX.B}
    assert set(result.changes[1].after) == {EX.A, EX.C}


def test_anonymous_ids_are_noise_but_list_order_is_an_actual_fact_change() -> None:
    before, after = Graph(), Graph()
    left, right = BNode("left"), BNode("right")
    Collection(before, left, [EX.A, EX.B])
    Collection(after, right, [EX.A, EX.B])
    before.add((EX.Class, OWL.unionOf, left))
    after.add((EX.Class, OWL.unionOf, right))
    assert compare_vocabularies(before, after).report.verified
    changed = Graph().add((EX.Class, OWL.unionOf, right))
    Collection(changed, right, [EX.B, EX.A])
    result = compare_vocabularies(before, changed)
    assert result.report.complete
    assert [(change.kind, change.iri) for change in result.changes] == [
        ("anonymous", "")
    ]
    assert not result.changes[0].before and not result.changes[0].after


def test_selected_scope_moves_and_limits_never_invent_removed_terms() -> None:
    declaration = Graph().add((EX.A, RDF.type, OWL.Class))
    before = load_vocabulary(declaration, lookup=[Graph()])
    after = load_vocabulary(Graph(), lookup=[declaration])
    moved = compare_vocabularies(before, after, scopes=("local", "lookup"))
    assert [change.kind for change in moved.changes] == ["scopes"]
    assert moved.changes[0].before_scopes == ("local",)
    assert moved.changes[0].after_scopes == ("lookup[0]",)
    limited = compare_vocabularies(declaration, declaration, limits=Limits(max_work=1))
    assert limited.report.outcome == "exhausted" and not limited.report.complete
    assert not limited.changes
    empty = compare_vocabularies(Graph(), Graph())
    assert empty.report.verified and not empty.changes


def test_skos_hierarchy_and_scheme_membership_facts_remain_visible() -> None:
    before = Graph().add((EX.Concept, RDF.type, SKOS.Concept))
    before.add((EX.Concept, SKOS.broader, EX.Parent))
    before.add((EX.Concept, SKOS.inScheme, EX.Scheme))
    after = Graph().add((EX.Concept, RDF.type, SKOS.Concept))
    after.add((EX.Concept, SKOS.broader, EX.OtherParent))
    after.add((EX.Concept, SKOS.inScheme, EX.OtherScheme))
    result = compare_vocabularies(before, after)
    assert {change.predicate for change in result.changes} == {
        str(SKOS.broader),
        str(SKOS.inScheme),
    }
    assert all(change.kind == "structure" for change in result.changes)
