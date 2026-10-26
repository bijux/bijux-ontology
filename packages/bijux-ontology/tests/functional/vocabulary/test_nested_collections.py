"""Actual nested SKOS paths preserve roles, ordering and bounded shared work."""

from collections.abc import Iterator, Sequence

import pytest
from rdflib import BNode, Graph, Literal, Namespace
from rdflib.collection import Collection
from rdflib.namespace import RDF, SKOS
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.structure import iter_rdf_list
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.collections import expand_collection

EX = Namespace("https://example.org/collections/")


def ordered(graph: Graph, node: Node, members: Sequence[Node]) -> None:
    """Supply an authored RDF list without inventing direct memberships."""
    graph.add((node, RDF.type, SKOS.OrderedCollection))
    head = BNode() if members else RDF.nil
    if members:
        Collection(graph, head, list(members))
    graph.add((node, SKOS.memberList, head))


def concepts(graph: Graph, *members: Node) -> None:
    for member in members:
        graph.add((member, RDF.type, SKOS.Concept))


def test_ordered_nested_paths_duplicates_and_retain_have_distinct_scope() -> None:
    graph = Graph()
    concepts(graph, EX.A, EX.B)
    ordered(graph, EX.Child, (EX.A, EX.B, EX.A))
    ordered(graph, EX.Root, (EX.Child, EX.A))
    view = inspect_vocabulary(graph)
    graph.remove((None, RDF.first, None))
    expanded = expand_collection(view, str(EX.Root))
    assert expanded.report.verified, expanded.report.to_dict()
    assert expanded.concepts == frozenset((EX.A, EX.B))
    assert expanded.sequence == (EX.A, EX.B, EX.A, EX.A)
    assert expanded.ordering == "total"
    assert [
        tuple(step.position for step in item.path) for item in expanded.members
    ] == [(0, 0), (0, 1), (0, 2), (1,)]
    retained = expand_collection(view, str(EX.Root), nested="retain")
    assert retained.report.verified
    assert retained.sequence == (EX.Child, EX.A)
    assert retained.concepts == frozenset((EX.A,))
    assert [item.kind for item in retained.members] == ["collection", "concept"]


def test_unordered_children_and_extra_direct_assertions_cannot_gain_order() -> None:
    graph = Graph()
    concepts(graph, EX.A, EX.B, EX.C)
    graph.add((EX.Child, RDF.type, SKOS.Collection))
    graph.add((EX.Child, SKOS.member, EX.A))
    graph.add((EX.Child, SKOS.member, EX.B))
    ordered(graph, EX.Root, (EX.Child, EX.C))
    result = expand_collection(inspect_vocabulary(graph), str(EX.Root))
    assert result.report.verified and result.sequence is None
    assert result.ordering == "mixed" and result.concepts == frozenset(
        (EX.A, EX.B, EX.C)
    )
    assert all(
        item.path[1].position is None for item in result.members if len(item.path) == 2
    )
    root = expand_collection(inspect_vocabulary(graph), str(EX.Child))
    assert (
        root.report.verified and root.ordering == "unordered" and root.sequence is None
    )
    ordered(graph, EX.Child, (EX.A, EX.B))
    result = expand_collection(inspect_vocabulary(graph), str(EX.Root))
    assert result.ordering == "mixed" and result.sequence is None
    assert any(item.path[-1].position is None for item in result.members)


def test_shared_blank_subcollection_decodes_once_but_retains_each_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bijux_ontology.vocabulary.browsing.navigation as navigation

    graph = Graph()
    shared = BNode()
    concepts(graph, EX.A, EX.B)
    ordered(graph, shared, (EX.A, EX.B))
    ordered(graph, EX.Left, (shared,))
    ordered(graph, EX.Right, (shared,))
    ordered(graph, EX.Root, (EX.Left, EX.Right))
    view = inspect_vocabulary(graph)
    calls = []

    def counted(
        graph: Graph, head: Node, *, max_items: int, budget: Budget | None = None
    ) -> Iterator[Node]:
        calls.append(head)
        return iter_rdf_list(graph, head, max_items=max_items, budget=budget)

    monkeypatch.setattr(navigation, "iter_rdf_list", counted)
    result = expand_collection(view, str(EX.Root))
    assert result.report.verified, result.report.to_dict()
    assert result.sequence == (EX.A, EX.B, EX.A, EX.B)
    assert len(calls) == len(set(calls)) == 4
    a_paths = [item.path for item in result.members if item.member == EX.A]
    assert len(a_paths) == 2 and a_paths[0][0].member != a_paths[1][0].member
    assert isinstance(a_paths[0][1].member, BNode)
    owned = a_paths[0][1].member
    for root in (owned, BNode(str(owned))):
        with pytest.raises(InputError):
            expand_collection(view, root)


def test_cycles_malformed_children_and_unknown_roles_retain_proved_members() -> None:
    graph = Graph()
    concepts(graph, EX.A)
    ordered(graph, EX.Root, (EX.A, EX.Child))
    ordered(graph, EX.Child, (EX.Root,))
    result = expand_collection(inspect_vocabulary(graph), str(EX.Root))
    assert result.concepts == frozenset((EX.A,)) and not result.report.complete
    assert result.sequence is None and result.ordering == "incomplete"
    assert any(
        issue.code == "terms.collection_containment_cycle" and issue.evidence
        for issue in result.report.issues
    )
    child_head = next(graph.objects(EX.Child, SKOS.memberList))
    graph.remove((child_head, RDF.rest, None))
    retained = expand_collection(
        inspect_vocabulary(graph), str(EX.Root), nested="retain"
    )
    assert retained.report.verified and retained.sequence == (EX.A, EX.Child)
    malformed = expand_collection(inspect_vocabulary(graph), str(EX.Root))
    assert not malformed.report.complete and malformed.concepts == frozenset((EX.A,))
    assert any(
        issue.code == "terms.collection_list_malformed"
        for issue in malformed.report.issues
    )
    for unknown in (EX.Untyped, Literal("invalid")):
        modified = Graph()
        concepts(modified, EX.A)
        ordered(modified, EX.Root, (EX.A, unknown))
        selected = expand_collection(inspect_vocabulary(modified), str(EX.Root))
        assert (
            not selected.report.complete and selected.members[-1].kind == "unresolved"
        )
        assert selected.concepts == frozenset((EX.A,))


def test_empty_lists_depth_and_shared_output_limits_are_explicit() -> None:
    graph = Graph()
    ordered(graph, EX.Empty, ())
    empty = expand_collection(inspect_vocabulary(graph), str(EX.Empty))
    assert empty.report.verified and empty.sequence == () and not empty.concepts
    concepts(graph, EX.A)
    ordered(graph, EX.Child, (EX.A,))
    ordered(graph, EX.Root, (EX.A, EX.Child))
    view = inspect_vocabulary(graph)
    shallow = expand_collection(view, str(EX.Root), max_depth=0)
    assert shallow.report.outcome == "exhausted" and shallow.concepts == frozenset(
        (EX.A,)
    )
    assert shallow.members[-1].kind == "collection"
    budget = Budget(Limits(max_states=20))
    bounded = expand_collection(view, str(EX.Root), budget=budget)
    assert not bounded.report.complete and bounded.report.outcome == "exhausted"
    assert bounded.members and bounded.members[0].member == EX.A
    long = Graph()
    concepts(long, EX.A)
    ordered(long, EX.Root, (EX.A,) * 30)
    prefix = expand_collection(
        inspect_vocabulary(long), str(EX.Root), budget=Budget(Limits(max_work=725))
    )
    assert prefix.report.outcome == "exhausted" and prefix.members
    assert prefix.concepts == frozenset((EX.A,))
    ordered(graph, EX.Grandchild, (EX.A,))
    graph.remove((EX.Child, SKOS.memberList, None))
    ordered(graph, EX.Child, (EX.Grandchild,))
    shared_depth = expand_collection(
        inspect_vocabulary(graph),
        str(EX.Root),
        max_depth=10,
        budget=Budget(Limits(max_depth=1)),
    )
    assert shared_depth.report.outcome == "exhausted"
    assert shared_depth.members[-1].member == EX.Grandchild
    for mode in (None, "other", 1):
        with pytest.raises(InputError):
            expand_collection(view, str(EX.Root), nested=mode)  # type: ignore[arg-type]
    for depth in (True, -1):
        with pytest.raises(InputError):
            expand_collection(view, str(EX.Root), max_depth=depth)
