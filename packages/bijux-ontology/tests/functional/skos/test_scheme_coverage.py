"""Root coverage finds orphan subtrees without conflating topology and integrity."""

from typing import Any

import pytest
from rdflib import BNode, Graph, Namespace
from rdflib.namespace import RDF, SKOS

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.skos.checking import check_skos
from bijux_ontology.skos.publication import check_scheme_coverage
from bijux_ontology.skos.xl import XL
from bijux_ontology.vocabulary.inspection import inspect_vocabulary

EX = Namespace("https://example.org/coverage/")


def source() -> Graph:
    graph = Graph().add((EX.Root, SKOS.topConceptOf, EX.Scheme))
    for member in (EX.A, EX.B):
        graph.add((member, RDF.type, SKOS.Concept))
        graph.add((member, SKOS.inScheme, EX.Scheme))
    graph.add((EX.A, SKOS.broader, EX.B))
    return graph


def test_connected_orphan_subtree_and_actual_inverse_witnesses() -> None:
    graph = source()
    assert check_skos(
        graph, concepts=(str(EX.A), str(EX.B)), rules=("isolated",)
    ).verified
    missing = check_scheme_coverage(inspect_vocabulary(graph), str(EX.Scheme))
    assert missing.report.outcome == "disagreement" and missing.report.complete
    assert missing.unreachable == ((EX.A, EX.B),)
    assert missing.witnesses[0].concept == EX.Root
    assert missing.report.issues[0].evidence
    graph.add((EX.B, SKOS.broader, EX.Root))
    complete = check_scheme_coverage(inspect_vocabulary(graph), str(EX.Scheme))
    assert complete.report.verified, complete.report.to_dict()
    assert complete.unreachable == ()
    paths = {item.concept: item.steps for item in complete.witnesses}
    assert [(step.source, step.target) for step in paths[EX.A]] == [
        (EX.Root, EX.B),
        (EX.B, EX.A),
    ]
    assert paths[EX.A][0].assertion.subject == EX.B
    assert paths[EX.A][0].assertion.predicate == SKOS.broader
    assert paths[EX.A][0].assertion.object == EX.Root
    graph.remove((EX.B, SKOS.broader, EX.Root))
    graph.add((EX.Root, SKOS.narrower, EX.B))
    inverse = check_scheme_coverage(inspect_vocabulary(graph), str(EX.Scheme))
    assert inverse.report.verified
    assert {
        item.steps[0].assertion.predicate for item in inverse.witnesses if item.steps
    } == {SKOS.narrower}


def test_cycles_are_opt_in_and_bidirectional_walk_does_not_invent_one() -> None:
    graph = source()
    graph.add((EX.B, SKOS.broader, EX.Root))
    view = inspect_vocabulary(graph)
    acyclic = check_scheme_coverage(
        view, str(EX.Scheme), direction="both", cycles="forbid"
    )
    assert acyclic.report.verified and acyclic.cycles == ()
    graph.add((EX.B, SKOS.broader, EX.A))
    view = inspect_vocabulary(graph)
    legal = check_scheme_coverage(view, str(EX.Scheme))
    assert legal.report.verified and legal.cycles is None
    forbidden = check_scheme_coverage(view, str(EX.Scheme), cycles="forbid")
    assert forbidden.report.outcome == "disagreement" and forbidden.cycles
    witness = forbidden.cycles[0]
    assert witness[0].source == witness[-1].target
    assert all(step.assertion.predicate == SKOS.broader for step in witness)
    assert any(
        issue.code == "skos.publication.hierarchy_cycle"
        for issue in forbidden.report.issues
    )


def test_explicit_cross_scheme_bridges_and_selected_concept_roles() -> None:
    graph = source()
    graph.add((EX.Root, SKOS.narrower, EX.Outside))
    graph.add((EX.Outside, SKOS.narrower, EX.B))
    graph.add((EX.Outside, SKOS.inScheme, EX.OtherScheme))
    graph.add((EX.Label, RDF.type, XL.Label))
    graph.add((EX.Label, SKOS.inScheme, EX.Scheme))
    view = inspect_vocabulary(graph)
    bounded = check_scheme_coverage(view, str(EX.Scheme))
    assert bounded.unreachable == ((EX.A, EX.B),)
    included = check_scheme_coverage(view, str(EX.Scheme), cross_scheme="include")
    assert included.report.verified and included.concepts == (EX.A, EX.B, EX.Root)
    path = next(item.steps for item in included.witnesses if item.concept == EX.B)
    assert [step.target for step in path] == [EX.Outside, EX.B]
    ancestors = check_scheme_coverage(
        view, str(EX.Scheme), direction="ancestors", cross_scheme="include"
    )
    assert ancestors.unreachable == ((EX.A, EX.B),)


def test_partial_traversal_retains_reached_proofs_and_independent_failure() -> None:
    graph = source()
    graph.add((EX.B, SKOS.broader, EX.Root))
    view = inspect_vocabulary(graph)
    result = check_scheme_coverage(
        view,
        str(EX.Scheme),
        roots=(str(EX.Root), str(EX.Missing)),
        budget=Budget(Limits(max_depth=1)),
    )
    assert result.report.outcome == "exhausted" and result.unreachable is None
    assert {item.concept for item in result.witnesses} == {EX.Root, EX.B}
    assert any(
        issue.code == "skos.publication.root_not_published"
        for issue in result.report.issues
    )
    assert not any(
        issue.code == "skos.publication.unreachable_component"
        for issue in result.report.issues
    )
    for maximum in (1, 10):
        exhausted = check_scheme_coverage(
            view, str(EX.Scheme), budget=Budget(Limits(max_work=maximum))
        )
        assert exhausted.report.outcome == "exhausted" and exhausted.unreachable is None
        assert not any(
            issue.code == "skos.publication.unreachable_component"
            for issue in exhausted.report.issues
        )


def test_finite_typed_selection_empty_scope_and_issue_limits() -> None:
    view = inspect_vocabulary(source())
    invalid: tuple[Any, ...] = ("roots", iter(()), [{}], [BNode("blank")], 1)
    for roots in invalid:
        with pytest.raises(InputError):
            check_scheme_coverage(view, str(EX.Scheme), roots=roots)
    for key, value in (("direction", "other"), ("cross_scheme", None), ("cycles", 1)):
        arguments: dict[str, Any] = {key: value}
        with pytest.raises(InputError):
            check_scheme_coverage(view, str(EX.Scheme), **arguments)
    empty = inspect_vocabulary(Graph())
    result = check_scheme_coverage(empty, str(EX.Scheme))
    assert result.report.outcome == "unverified" and not result.report.complete
    assert result.unreachable is None
    limited = check_scheme_coverage(
        empty,
        str(EX.Scheme),
        roots=(str(EX.Missing),),
        budget=Budget(Limits(max_issues=1)),
    )
    assert limited.report.outcome == "exhausted"
    assert any(
        issue.code == "skos.publication.root_not_published"
        for issue in limited.report.issues
    )
