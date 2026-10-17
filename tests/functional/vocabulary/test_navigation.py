"""Asserted navigation retains direction, cycles, source evidence and list order."""

from rdflib import BNode, Graph, Literal, Namespace
from rdflib.collection import Collection
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from bijux_ontology.limits import Limits
from bijux_ontology.vocabulary.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.navigation import (
    collection_members,
    neighbours,
    scheme_members,
    shortest_path,
    topology,
    traverse,
)

EX = Namespace("https://example.org/")


def test_direct_transitive_and_shortest_paths_keep_asserted_edge_evidence() -> None:
    graph = Graph()
    for node in (EX.A, EX.B, EX.C):
        graph.add((node, RDF.type, OWL.Class))
    for child, parent in ((EX.A, EX.B), (EX.B, EX.C), (EX.C, EX.A)):
        graph.add((child, RDFS.subClassOf, parent))
    view = inspect_vocabulary(graph)
    assert [edge.object for edge in neighbours(view, str(EX.A)).items] == [EX.B]
    assert [visit.iri for visit in traverse(view, str(EX.A)).items] == [
        str(EX.B),
        str(EX.C),
    ]
    assert [
        visit.depth for visit in traverse(view, str(EX.A), include_self=True).items
    ] == [0, 1, 2]
    path = shortest_path(view, str(EX.A), str(EX.C))
    assert path.status == "found" and path.report.verified
    assert [edge.subject for edge in path.edges] == [EX.A, EX.B]
    reverse = traverse(view, str(EX.A), direction="incoming")
    assert reverse.items[0].iri == str(EX.C)
    assert shortest_path(view, str(EX.A), str(EX.Missing)).status == "unreachable"


def test_truncated_navigation_is_incomplete_and_cannot_claim_unreachable() -> None:
    graph = Graph().add((EX.A, RDFS.subClassOf, EX.B))
    graph.add((EX.B, RDFS.subClassOf, EX.C))
    view = inspect_vocabulary(graph)
    selected = traverse(view, str(EX.A), max_depth=1)
    assert [visit.iri for visit in selected.items] == [str(EX.B)]
    assert selected.report.outcome == "exhausted"
    assert shortest_path(view, str(EX.A), str(EX.C), max_depth=1).status == "incomplete"
    limited = inspect_vocabulary(graph, limits=Limits(max_work=1))
    assert traverse(limited, str(EX.A)).report.outcome == "exhausted"


def test_topology_keeps_cycles_separate_from_validity_and_includes_isolates() -> None:
    graph = Graph()
    for node in (EX.A, EX.B, EX.C, EX.Isolated):
        graph.add((node, RDF.type, OWL.Class))
    graph.add((EX.A, RDFS.subClassOf, EX.B))
    graph.add((EX.B, RDFS.subClassOf, EX.A))
    graph.add((EX.C, RDFS.subClassOf, EX.B))
    selected = topology(inspect_vocabulary(graph), kinds=(str(OWL.Class),))
    assert selected.cycles == ((str(EX.A), str(EX.B)),)
    assert selected.components == (
        (str(EX.A), str(EX.B), str(EX.C)),
        (str(EX.Isolated),),
    )
    assert selected.roots == (str(EX.Isolated),)
    assert selected.leaves == (str(EX.C), str(EX.Isolated))
    assert selected.report.verified


def test_skos_inverse_membership_and_ordered_lists_do_not_become_hierarchy() -> None:
    graph = Graph()
    graph.add((EX.A, SKOS.inScheme, EX.Scheme))
    graph.add((EX.Scheme, SKOS.hasTopConcept, EX.B))
    graph.add((EX.C, SKOS.topConceptOf, EX.Scheme))
    head = BNode()
    Collection(graph, head, [EX.C, EX.A, EX.B])
    graph.add((EX.Collection, SKOS.memberList, head))
    view = inspect_vocabulary(graph)
    assert {item.member for item in scheme_members(view, str(EX.Scheme)).items} == {
        EX.A,
        EX.B,
        EX.C,
    }
    assert {
        item.member
        for item in scheme_members(view, str(EX.Scheme), top_only=True).items
    } == {EX.B, EX.C}
    members = collection_members(view, str(EX.Collection), ordered=True)
    assert [item.member for item in members.items] == [EX.C, EX.A, EX.B]
    assert members.report.complete and all(item.implied for item in members.items)
    assert not neighbours(
        view, str(EX.Collection), predicates=(str(SKOS.broader),)
    ).items
    graph.add((EX.Collection, SKOS.memberList, BNode()))
    assert (
        collection_members(
            inspect_vocabulary(graph), str(EX.Collection), ordered=True
        ).report.outcome
        == "unverified"
    )


def test_anonymous_branches_do_not_invalidate_an_existing_named_path() -> None:
    graph = Graph().add((EX.A, RDFS.subClassOf, BNode()))
    graph.add((EX.A, RDFS.subClassOf, EX.B))
    graph.add((EX.A, EX.annotation, Literal("unrelated")))
    view = inspect_vocabulary(graph)
    assert traverse(view, str(EX.A)).report.outcome == "unverified"
    assert shortest_path(view, str(EX.A), str(EX.B)).report.verified
