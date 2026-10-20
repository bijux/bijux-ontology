"""Authored collection positions remain distinct from unordered membership."""

from collections.abc import Sequence

import pytest
from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.collection import Collection
from rdflib.namespace import RDF, SKOS

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Limits
from bijux_ontology.rdf.structure import read_rdf_list
from bijux_ontology.skos.collections import check_collection_sequence
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.browsing.navigation import collection_members

EX = Namespace("https://example.org/")


def _graph(members: Sequence[URIRef]) -> tuple[Graph, URIRef | BNode]:
    """Construct only authored positions; no additional SKOS facts are inferred."""
    graph = Graph()
    head = BNode() if members else RDF.nil
    if members:
        Collection(graph, head, list(members))
    graph.add((EX.Ordered, SKOS.memberList, head))
    return graph, head


def test_positions_duplicates_and_unordered_members_stay_separate() -> None:
    graph, _ = _graph((EX.A, EX.B, EX.A))
    graph.add((EX.Ordered, SKOS.member, EX.Extra))
    view = inspect_vocabulary(graph)
    positions = collection_members(view, str(EX.Ordered), ordered=True)
    assert [(item.member, item.position) for item in positions.items] == [
        (EX.A, 0),
        (EX.B, 1),
        (EX.A, 2),
    ]
    assert positions.report.complete
    direct = collection_members(view, str(EX.Ordered))
    assert [(item.member, item.position) for item in direct.items] == [(EX.Extra, None)]
    assert check_collection_sequence(
        view,
        str(EX.Ordered),
        expected=(EX.A, EX.B, EX.A),
        minimum_size=3,
        maximum_size=3,
    ).verified
    duplicate = check_collection_sequence(view, str(EX.Ordered), unique_members=True)
    assert [issue.evidence for issue in duplicate.issues] == [
        {"first_position": 0, "position": 2},
    ]
    assert duplicate.issues[0].code == "skos.policy.collection_duplicate_member"
    assert duplicate.outcome == "disagreement"
    graph.set((EX.Ordered, SKOS.member, EX.Other))
    assert check_collection_sequence(
        inspect_vocabulary(graph),
        str(EX.Ordered),
        expected=(EX.A, EX.B, EX.A),
    ).verified


def test_order_is_checked_only_when_selected_and_size_counts_positions() -> None:
    graph, _ = _graph((EX.B, EX.A))
    view = inspect_vocabulary(graph)
    assert check_collection_sequence(
        view,
        str(EX.Ordered),
        minimum_size=2,
        maximum_size=2,
        unique_members=True,
    ).verified
    changed = check_collection_sequence(view, str(EX.Ordered), expected=(EX.A, EX.B))
    assert changed.complete and len(changed.issues) == 2
    assert {issue.code for issue in changed.issues} == {
        "skos.policy.collection_sequence_mismatch",
    }
    for expected in ((EX.B,), (EX.B, EX.A, EX.C)):
        assert not check_collection_sequence(
            view,
            str(EX.Ordered),
            expected=expected,
        ).verified
    for minimum, maximum in ((3, None), (None, 1)):
        size = check_collection_sequence(
            view, str(EX.Ordered), minimum_size=minimum, maximum_size=maximum
        )
        assert size.issues[0].code == "skos.policy.collection_size_mismatch"


def test_nil_is_known_empty_but_absent_and_multiple_heads_are_unknown() -> None:
    graph, _ = _graph(())
    assert check_collection_sequence(
        inspect_vocabulary(graph),
        str(EX.Ordered),
        expected=(),
        maximum_size=0,
    ).verified
    assert (
        check_collection_sequence(
            inspect_vocabulary(graph),
            str(EX.Ordered),
            minimum_size=1,
        ).outcome
        == "disagreement"
    )
    for heads in ((), (RDF.nil, EX.AnotherHead)):
        graph = Graph()
        for head in heads:
            graph.add((EX.Ordered, SKOS.memberList, head))
        report = check_collection_sequence(
            inspect_vocabulary(graph),
            str(EX.Ordered),
            expected=(EX.A,),
        )
        assert report.outcome == "unverified" and not report.complete
        assert report.checked == 0
        assert report.issues[0].code == "terms.collection_list_ambiguous"


@pytest.mark.parametrize("tail", ["cycle", "branched", "missing", "literal"])
def test_malformed_tails_retain_proved_prefix_and_failures(tail: str) -> None:
    graph, head = _graph((EX.B, EX.A))
    last = next(graph.objects(head, RDF.rest))
    if tail == "cycle":
        graph.set((last, RDF.rest, head))
    elif tail == "branched":
        graph.add((last, RDF.first, EX.C))
    elif tail == "missing":
        graph.remove((last, RDF.rest, None))
    else:
        graph.set((last, RDF.rest, Literal("invalid link")))
    view = inspect_vocabulary(graph)
    selected = collection_members(view, str(EX.Ordered), ordered=True)
    assert selected.items[0].member == EX.B and selected.items[0].position == 0
    assert not selected.report.complete
    assert any(
        issue.code == "terms.collection_list_malformed"
        for issue in selected.report.issues
    )
    report = check_collection_sequence(view, str(EX.Ordered), expected=(EX.A, EX.B))
    assert not report.complete and report.outcome == "disagreement"
    assert report.issues[0].code == "skos.policy.collection_sequence_mismatch"
    assert any(
        issue.code == "terms.collection_list_malformed" for issue in report.issues
    )
    with pytest.raises(InputError):
        read_rdf_list(graph, head)


def test_exhausted_tail_preserves_prefix_failure() -> None:
    graph, _ = _graph((EX.B, EX.A, EX.A, EX.A, EX.A))
    view = inspect_vocabulary(graph)
    # The view already owns its input. Select a smaller navigation allowance,
    # without changing the immutable graph or rebuilding it under that bound.
    view.limits = Limits(max_work=150)
    report = check_collection_sequence(view, str(EX.Ordered), expected=(EX.A,))
    assert report.outcome == "exhausted" and not report.complete
    assert any(
        issue.code == "skos.policy.collection_sequence_mismatch"
        for issue in report.issues
    )
    assert not any(issue.actual == "0" for issue in report.issues)
    view.limits = Limits(max_work=5)
    selected = collection_members(view, str(EX.Ordered), ordered=True)
    assert selected.items and not selected.report.complete
    assert [item.position for item in selected.items] == list(
        range(len(selected.items))
    )
    with pytest.raises(LimitExceeded):
        read_rdf_list(
            graph, next(graph.objects(EX.Ordered, SKOS.memberList)), max_items=1
        )


def test_bounded_expected_capture_and_owned_view_do_not_guess_blank_identity() -> None:
    graph, _ = _graph((EX.A,))
    view = inspect_vocabulary(graph)
    graph.remove((None, RDF.first, None))
    assert check_collection_sequence(view, str(EX.Ordered), expected=(EX.A,)).verified
    for expected in ((BNode("node"),), (Literal(str(EX.A)),), ("relative",)):
        with pytest.raises(InputError):
            check_collection_sequence(view, str(EX.Ordered), expected=expected)
    view.limits = Limits(max_states=1)
    exhausted = check_collection_sequence(view, str(EX.Ordered), expected=(EX.A, EX.B))
    assert exhausted.outcome == "exhausted" and not exhausted.complete


def test_duplicate_and_maximum_findings_survive_exhaustion() -> None:
    graph, _ = _graph((EX.A, EX.A, EX.B, EX.C, EX.D))
    view = inspect_vocabulary(graph)
    view.limits = Limits(max_work=252)
    report = check_collection_sequence(
        view,
        str(EX.Ordered),
        unique_members=True,
        maximum_size=1,
    )
    assert report.outcome == "exhausted" and not report.complete
    assert {issue.code for issue in report.issues} == {
        "skos.policy.collection_duplicate_member",
        "skos.policy.collection_size_mismatch",
        "execution.limit_exceeded",
    }
    size = next(
        issue
        for issue in report.issues
        if issue.code == "skos.policy.collection_size_mismatch"
    )
    assert size.expected == "1" and size.actual == "2"
    view.limits = Limits(max_work=7)
    minimum = check_collection_sequence(view, str(EX.Ordered), minimum_size=10)
    assert minimum.outcome == "exhausted"
    assert not any(issue.kind == "finding" for issue in minimum.issues)


def test_incomplete_head_selection_cannot_fabricate_unambiguous_positions() -> None:
    graph, _ = _graph((EX.A,))
    graph.add((EX.Ordered, SKOS.memberList, RDF.nil))
    view = inspect_vocabulary(graph)
    view.limits = Limits(max_states=1)
    selected = collection_members(view, str(EX.Ordered), ordered=True)
    assert not selected.items and selected.report.outcome == "exhausted"
    assert (
        check_collection_sequence(
            view,
            str(EX.Ordered),
            minimum_size=1,
        ).outcome
        == "exhausted"
    )
