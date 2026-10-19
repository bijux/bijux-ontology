"""Slim assignment preserves fine annotations and qualifies every selected root."""

from collections.abc import Sequence
from dataclasses import replace
from typing import Any, cast

import pytest
from rdflib import BNode, Dataset, Graph, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.relation_paths.types import (
    RelationRepeat,
    RelationSequence,
    RelationStep,
)
from bijux_ontology.vocabulary.restrictions.projection import project_restrictions
from bijux_ontology.vocabulary.slims import SlimAnnotation, assign_slim
from bijux_ontology.vocabulary.sources.loading import load_vocabulary

EX = Namespace("https://example.org/slims/")


def _graph() -> Graph:
    graph = Graph()
    for term in (EX.Fine, EX.Middle, EX.Left, EX.Right, EX.Tie, EX.Isolated):
        graph.add((term, RDF.type, OWL.Class))
    for child, parent in (
        (EX.Fine, EX.Left),
        (EX.Fine, EX.Middle),
        (EX.Middle, EX.Right),
        (EX.Tie, EX.Left),
        (EX.Tie, EX.Right),
    ):
        graph.add((child, RDFS.subClassOf, parent))
    return graph


def test_nearest_all_ties_original_occurrences_and_distinct_record_counts() -> None:
    graph = _graph()
    before = set(graph)
    fine = URIRef(str(EX.Fine))
    annotations = [
        SlimAnnotation("Record", fine),
        SlimAnnotation("Record", fine),
        SlimAnnotation("record", fine),
        SlimAnnotation("Record", str(EX.Tie)),
    ]
    view = inspect_vocabulary(graph)
    roots = [str(EX.Left), str(EX.Right)]
    nearest = assign_slim(view, annotations, roots=roots, policy="nearest")
    assert nearest.report.verified and nearest.report.checked == 4
    assert [match.root for match in nearest.assignments[0].classification.selected] == [
        EX.Left
    ]
    assert [
        match.distance for match in nearest.assignments[0].classification.applicable
    ] == [
        1,
        2,
    ]
    assert len(nearest.assignments[3].classification.selected) == 2
    assert nearest.multiple == 4 and nearest.uncovered == nearest.unknown == 0
    assert [(item.annotations, item.records) for item in nearest.counts] == [
        (4, 2),
        (1, 1),
    ]
    assert nearest.assignments[0].annotation.term is fine
    assert (
        nearest.assignments[0].classification is nearest.assignments[1].classification
    )
    assert [item.index for item in nearest.assignments] == list(range(4))
    all_roots = assign_slim(view, annotations, roots=roots, policy="all")
    assert all_roots.report.verified
    assert [(item.annotations, item.records) for item in all_roots.counts] == [
        (4, 2),
        (4, 2),
    ]
    assert set(graph) == before and roots == [str(EX.Left), str(EX.Right)]


def test_uncovered_unknown_empty_and_no_inferred_mode_claim() -> None:
    view = inspect_vocabulary(_graph())
    roots = [str(EX.Left)]
    result = assign_slim(
        view,
        [SlimAnnotation("r", str(EX.Isolated)), SlimAnnotation("r", str(EX.Missing))],
        roots=roots,
        policy="all",
    )
    assert [item.classification.status for item in result.assignments] == [
        "uncovered",
        "unknown",
    ]
    assert result.uncovered == result.unknown == 1 and not result.report.verified
    assert any(issue.code == "terms.slim_uncovered" for issue in result.report.issues)
    missing_root = assign_slim(
        view,
        [SlimAnnotation("r", str(EX.Fine))],
        roots=[*roots, str(EX.Absent)],
        policy="all",
    )
    assert missing_root.unknown == 1
    assert missing_root.assignments[0].classification.applicable
    assert not missing_root.assignments[0].classification.selected
    empty = assign_slim(view, [], roots=roots, policy="nearest")
    assert empty.report.complete and empty.report.outcome == "not_applicable"
    assert (
        not empty.assignments
        and empty.counts[0].annotations == empty.counts[0].records == 0
    )
    assert assign_slim(view, [], roots=[], policy="all").report.outcome == "input_error"
    inferred = assign_slim(
        view,
        [SlimAnnotation("r", str(EX.Fine))],
        roots=roots,
        policy="all",
        relation_view="inferred",
    )
    assert inferred.unknown == 1 and not inferred.report.verified
    assert any(
        issue.code == "terms.slim_view_unverified" for issue in inferred.report.issues
    )


def test_explicit_existential_language_and_incomplete_projection_are_required() -> None:
    graph = _graph()
    for owner, quantifier in (
        (EX.Component, OWL.someValuesFrom),
        (EX.Universal, OWL.allValuesFrom),
    ):
        node = BNode()
        for triple in (
            (owner, RDFS.subClassOf, node),
            (node, RDF.type, OWL.Restriction),
            (node, OWL.onProperty, EX.part_of),
            (node, quantifier, EX.Fine),
        ):
            graph.add(triple)
    graph.add((EX.Raw, EX.part_of, EX.Fine))
    view = inspect_vocabulary(graph)
    path = RelationSequence(
        (
            RelationStep(str(EX.part_of), "existential"),
            RelationRepeat(RelationStep(str(RDFS.subClassOf), "class")),
        )
    )
    annotations = [
        SlimAnnotation("r", str(term)) for term in (EX.Component, EX.Universal, EX.Raw)
    ]
    result = assign_slim(
        view, annotations, roots=[str(EX.Left)], policy="all", path=path
    )
    assert [item.classification.status for item in result.assignments] == [
        "covered",
        "uncovered",
        "uncovered",
    ]
    witness = result.assignments[0].classification.selected[0].path.steps[0].evidence
    assert witness.quantifier == OWL.someValuesFrom  # type: ignore[union-attr]
    actual = project_restrictions(view, str(EX.Component))
    incomplete = replace(
        actual,
        report=Report(
            "selected_projection",
            1,
            (Issue("example.incomplete", "Projection incomplete", kind="unverified"),),
            complete=False,
        ),
    )
    unknown = assign_slim(
        view,
        annotations[:1],
        roots=[str(EX.Left)],
        policy="all",
        path=path,
        projections=incomplete,
    )
    assert unknown.unknown == 1 and not unknown.assignments[0].classification.selected
    assert any(issue.code == "example.incomplete" for issue in unknown.report.issues)
    skos = inspect_vocabulary(Graph().add((EX.Fine, SKOS.broader, EX.Left)))
    assert (
        assign_slim(
            skos,
            annotations[:0] + [SlimAnnotation("r", str(EX.Fine))],
            roots=[str(EX.Left)],
            policy="all",
        ).uncovered
        == 1
    )
    assert assign_slim(
        skos,
        [SlimAnnotation("r", str(EX.Fine))],
        roots=[str(EX.Left)],
        policy="all",
        path=RelationStep(str(SKOS.broader), "skos"),
    ).report.verified


def test_selected_graph_exact_iris_and_actual_query_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bijux_ontology.vocabulary.slims.checking as checking

    dataset = Dataset()
    selected = dataset.graph(EX.G)
    selected.add((EX.Fine, RDF.type, OWL.Class))
    selected.add((EX.Left, RDF.type, OWL.Class))
    dataset.graph(EX.H).add((EX.Fine, RDFS.subClassOf, EX.Left))
    view = inspect_vocabulary(load_vocabulary(dataset, graph_name=str(EX.G)))
    annotations = [SlimAnnotation("r", str(EX.Fine)), SlimAnnotation("r", EX.Fine)]
    result = assign_slim(view, annotations, roots=[str(EX.Left)], policy="nearest")
    assert result.uncovered == 2 and result.report.outcome == "disagreement"
    from bijux_ontology.vocabulary.relation_paths.checking import find_relation_path

    actual = find_relation_path
    calls = []

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return actual(*args, **kwargs)

    monkeypatch.setattr(checking, "find_relation_path", counted)
    union = inspect_vocabulary(load_vocabulary(dataset, union=True))
    assert assign_slim(
        union, annotations, roots=[str(EX.Left), str(EX.Left)], policy="all"
    ).report.verified
    assert len(calls) == 1
    graph = Graph().add(
        (EX.Fine, RDFS.subClassOf, URIRef("http://example.org/slims/Left"))
    )
    graph.add((EX.Left, RDF.type, OWL.Class))
    assert (
        assign_slim(
            inspect_vocabulary(graph), annotations, roots=[str(EX.Left)], policy="all"
        ).uncovered
        == 2
    )


def test_capture_limits_paid_prefix_and_fresh_recovery() -> None:
    view = inspect_vocabulary(_graph())
    annotations = [
        SlimAnnotation("r", str(EX.Isolated)),
        SlimAnnotation("r", str(EX.Fine)),
    ]
    roots = [str(EX.Left)]
    baseline_budget = Budget()
    baseline = assign_slim(
        view, annotations, roots=roots, policy="nearest", budget=baseline_budget
    )
    assert baseline.uncovered == 1 and baseline.report.outcome == "disagreement"
    work = baseline_budget.limits.max_work - baseline_budget.remaining("work")
    paid_late = 0
    for cap in range(work // 2, work, max(1, work // 40)):
        result = assign_slim(
            view,
            annotations,
            roots=roots,
            policy="nearest",
            budget=Budget(Limits(max_work=cap)),
        )
        assert result.report.outcome == "exhausted"
        if result.uncovered:
            assert any(
                issue.code == "terms.slim_uncovered" for issue in result.report.issues
            )
            paid_late += 1
    assert paid_late > 0
    giant = assign_slim(
        view,
        [SlimAnnotation("r" * 100000, str(EX.Fine))],
        roots=roots,
        policy="nearest",
        budget=Budget(Limits(max_bytes=1000)),
    )
    assert giant.report.outcome == "exhausted" and not giant.assignments
    malformed = assign_slim(
        view,
        [SlimAnnotation("r", cast(str, BNode("urn:looks:named")))],
        roots=roots,
        policy="all",
    )
    assert malformed.report.outcome == "input_error"

    class CapturedOnce(Sequence[SlimAnnotation]):
        def __init__(self) -> None:
            self.reads = 0

        def __len__(self) -> int:
            self.reads += 1
            assert self.reads == 1
            return 1

        def __getitem__(self, index: Any) -> Any:
            assert index == 0
            return annotations[1]

    dynamic = CapturedOnce()
    assert assign_slim(view, dynamic, roots=roots, policy="all").report.verified
    assert dynamic.reads == 1
    assert assign_slim(view, annotations[1:], roots=roots, policy="all").report.verified
