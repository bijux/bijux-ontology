"""Actual local invalidation and conservative full selected-target fallback."""

import importlib.util
from dataclasses import replace
from threading import Event

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace
from rdflib.namespace import RDF, RDFS, SH, XSD

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Limits
from bijux_ontology.rdf.patches.planning import plan_rdf_patch
from bijux_ontology.rdf.patches.types import (
    BlankNodePair,
    DatasetPatch,
    PatchBlankBinding,
)
from bijux_ontology.rdf.records.types import PatchBlank
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.sources.types import RdfSource
from bijux_ontology.shacl.checking import validate_shapes
from bijux_ontology.shacl.evidence.results import ShaclResult
from bijux_ontology.shacl.execution.session import ShaclSession
from bijux_ontology.shacl.incremental.checking import revalidate
from bijux_ontology.shacl.incremental.preparation import prepare_incremental

E = Namespace("urn:incremental:")
LIMITS = Limits(max_work=10000000, max_states=1000000, max_total_bytes=100000000)
BACKEND = pytest.mark.skipif(
    importlib.util.find_spec("pyshacl") is None, reason="Requires maintained SHACL"
)


def _shapes() -> Graph:
    """Select one ordinary named target and direct local Core property."""
    return (
        Graph()
        .add((E.S, RDF.type, SH.NodeShape))
        .add((E.S, SH.targetClass, E.C))
        .add((E.S, SH.property, E.P))
        .add((E.P, SH.path, E.p))
        .add((E.P, SH.minCount, Literal(1)))
        .add((E.P, SH.datatype, XSD.string))
    )


def _data() -> Graph:
    """Keep two independently targeted records with unrelated exact literal values."""
    return (
        Graph()
        .add((E.a, RDF.type, E.C))
        .add((E.a, E.p, Literal("a")))
        .add((E.b, RDF.type, E.C))
        .add((E.b, E.p, Literal("b")))
    )


def _patch(before: PreparedSource, after: PreparedSource) -> DatasetPatch:
    """Use the actual exact dataset patch planner, never invent edit bindings."""
    result = plan_rdf_patch(before, after, limits=LIMITS)
    assert result.report.verified and result.patch is not None
    return result.patch


def _keys(results: tuple[ShaclResult, ...]) -> set[tuple[str, ...]]:
    """Compare semantic result positions, excluding fresh report blank identities."""
    return {
        tuple(
            str(value)
            for value in (
                item.focus_node,
                item.path,
                item.value,
                item.constraint_component,
                item.source_shape,
            )
        )
        for result in results
        for item in result.findings
    }


@BACKEND
def test_multiroot_delta_reuses_one_worker_without_crossing_focus_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shapes, graph = _shapes(), _data()
    shapes.add((E.Other, RDF.type, SH.NodeShape))
    shapes.add((E.Other, SH.targetClass, E.D))
    shapes.add((E.Other, SH.property, E.Q))
    shapes.add((E.Q, SH.path, E.q))
    shapes.add((E.Q, SH.minCount, Literal(1)))
    graph.add((E.c, RDF.type, E.D))
    graph.add((E.c, E.q, Literal("c")))
    before = prepare_rdf(graph, limits=LIMITS)
    baseline = prepare_incremental(
        before,
        shapes,
        use_shapes=(str(E.S), str(E.Other)),
        limits=LIMITS,
    )
    assert baseline.report.verified and baseline.state is not None
    sessions: list[ShaclSession] = []

    class ObservedSession(ShaclSession):
        def __enter__(self) -> ShaclSession:
            sessions.append(self)
            return super().__enter__()

    monkeypatch.setattr(
        "bijux_ontology.shacl.incremental.preparation.ShaclSession",
        ObservedSession,
    )
    graph.remove((E.a, E.p, None))
    graph.remove((E.c, E.q, None))
    after = prepare_rdf(graph, limits=LIMITS)
    result = revalidate(baseline.state, _patch(before, after), limits=LIMITS)
    assert result.strategy == "local"
    assert result.checked_pairs == 2 and result.reused_pairs == 1
    assert len(sessions) == 1 and sessions[0].worker_launches == 1
    assert result.report.outcome == "disagreement" and result.state is not None
    assert {
        (finding.source_shape, finding.focus_node)
        for raw in result.results
        for finding in raw.findings
    } == {(E.P, E.a), (E.Q, E.c)}


@BACKEND
def test_local_delete_repair_immutable_resources_and_actual_full_parity() -> None:
    shapes, graph = _shapes(), _data()
    source = prepare_rdf(graph, limits=LIMITS)
    prepared = prepare_incremental(
        source, shapes, use_shapes=(str(E.S),), limits=LIMITS
    )
    assert prepared.state is not None and prepared.report.verified
    state = prepared.state
    assert state.local and len(state.targets) == 2
    shapes.add((E.P, SH.maxCount, Literal(0)))
    graph.remove((E.a, E.p, None))
    target = prepare_rdf(graph, limits=LIMITS)
    result = revalidate(state, _patch(source, target), limits=LIMITS)
    full = validate_shapes(target, state.shapes, use_shapes=state.roots, limits=LIMITS)
    assert result.strategy == "local" and result.checked_pairs == 1
    assert result.reused_pairs == 1 and result.total_pairs == 2
    assert result.report.outcome == "disagreement" and result.state is not None
    assert _keys(result.results) == _keys((full,))
    assert (E.a, E.p, Literal("a")) in source.graph()
    result.results[0].report = Report("mutable-consumer-view", 1)
    assert not result.state.report.verified
    graph.add((E.a, E.p, Literal("repaired")))
    repaired = prepare_rdf(graph, limits=LIMITS)
    repair = revalidate(
        result.state, _patch(result.state.source, repaired), limits=LIMITS
    )
    assert repair.strategy == "full" and repair.report.verified
    assert repair.checked_pairs == 2 and repair.reused_pairs == 0
    # A separately exposed mutable raw result cannot rewrite the frozen proof.
    if prepared.result is not None:
        prepared.result.report = Report("consumer-view", 1)
    assert state.report.verified
    with pytest.raises(InputError):
        replace(state, report=Report("forged", 1))


@BACKEND
def test_target_and_subclass_changes_refresh_complete_actual_inventory() -> None:
    shapes, graph = _shapes(), _data()
    graph.add((E.c, RDF.type, E.Child))
    source = prepare_rdf(graph, limits=LIMITS)
    prepared = prepare_incremental(
        source, shapes, use_shapes=(str(E.S),), limits=LIMITS
    )
    assert prepared.state is not None
    graph.add((E.Child, RDFS.subClassOf, E.C))
    target = prepare_rdf(graph, limits=LIMITS)
    result = revalidate(prepared.state, _patch(source, target), limits=LIMITS)
    assert result.strategy == "full" and result.checked_pairs == 3
    assert result.report.outcome == "disagreement"
    assert E.c in {
        finding.focus_node for raw in result.results for finding in raw.findings
    }
    assert result.state is not None
    graph.remove((E.Child, RDFS.subClassOf, E.C))
    graph.remove((E.b, RDF.type, E.C))
    reduced = prepare_rdf(graph, limits=LIMITS)
    removed = revalidate(
        result.state, _patch(result.state.source, reduced), limits=LIMITS
    )
    assert removed.strategy == "full" and removed.report.verified
    assert removed.checked_pairs == 1
    for targeting, term in ((SH.targetSubjectsOf, E.p), (SH.targetObjectsOf, E.link)):
        own_shapes = _shapes().remove((E.S, SH.targetClass, None))
        own_shapes.add((E.S, targeting, term))
        own_graph = _data().add((E.owner, E.link, E.a))
        before = prepare_rdf(own_graph, limits=LIMITS)
        start = prepare_incremental(
            before, own_shapes, use_shapes=(str(E.S),), limits=LIMITS
        )
        assert start.state is not None and start.report.verified
        own_graph.add(
            (
                E.owner if targeting == SH.targetObjectsOf else E.new,
                term,
                E.new if targeting == SH.targetObjectsOf else Literal("new"),
            )
        )
        after = prepare_rdf(own_graph, limits=LIMITS)
        changed = revalidate(start.state, _patch(before, after), limits=LIMITS)
        assert changed.strategy == "full"
        if targeting == SH.targetObjectsOf:
            assert changed.report.outcome == "disagreement"
        else:
            assert changed.report.verified and changed.checked_pairs == 3


@BACKEND
def test_global_uniqueness_and_complex_paths_always_use_actual_full_fallback() -> None:
    shapes, graph = _shapes(), _data()
    shapes.add((E.S, SH.sparql, E.Query))
    shapes.add(
        (
            E.Query,
            SH.select,
            Literal(
                f"SELECT $this WHERE {{ $this <{E.p}> ?v . ?other <{E.p}> ?v . "
                "FILTER (?other != $this) }"
            ),
        )
    )
    source = prepare_rdf(graph, limits=LIMITS)
    prepared = prepare_incremental(
        source, shapes, use_shapes=(str(E.S),), trusted_sparql=True, limits=LIMITS
    )
    assert prepared.state is not None and not prepared.state.local
    graph.set((E.b, E.p, Literal("a")))
    target = prepare_rdf(graph, limits=LIMITS)
    result = revalidate(prepared.state, _patch(source, target), limits=LIMITS)
    full = validate_shapes(
        target, shapes, use_shapes=(str(E.S),), trusted_sparql=True, limits=LIMITS
    )
    assert result.strategy == "full" and result.checked_pairs == 2
    assert result.report.outcome == "disagreement" and _keys(result.results) == _keys(
        (full,)
    )
    inverse = _shapes().remove((E.P, SH.path, None))
    path = BNode()
    inverse.add((E.P, SH.path, path)).add((path, SH.inversePath, E.link))
    inverse.remove((E.P, SH.datatype, None))
    graph = _data().add((E.owner, E.link, E.a)).add((E.owner, E.link, E.b))
    source = prepare_rdf(graph, limits=LIMITS)
    prepared = prepare_incremental(
        source, inverse, use_shapes=(str(E.S),), limits=LIMITS
    )
    assert prepared.state is not None and not prepared.state.local
    graph.remove((E.owner, E.link, E.b))
    result = revalidate(
        prepared.state, _patch(source, prepare_rdf(graph, limits=LIMITS)), limits=LIMITS
    )
    assert result.strategy == "full" and result.report.outcome == "disagreement"


@BACKEND
def test_physical_graph_scope_wrong_base_and_unrelated_changes() -> None:
    data = Dataset()
    data.graph(E.G).add((E.a, RDF.type, E.C)).add((E.a, E.p, Literal("a")))
    data.graph(E.H).add((E.a, E.p, Literal("support")))
    source = prepare_rdf(RdfSource(data, graph_name=str(E.G)), limits=LIMITS)
    start = prepare_incremental(
        source, _shapes(), use_shapes=(str(E.S),), limits=LIMITS
    )
    assert start.state is not None and start.report.verified
    data.graph(E.G).remove((E.a, E.p, None))
    target = prepare_rdf(RdfSource(data, graph_name=str(E.G)), limits=LIMITS)
    changed = revalidate(start.state, _patch(source, target), limits=LIMITS)
    assert changed.strategy == "local" and changed.report.outcome == "disagreement"
    assert changed.application is not None and changed.application.source is not None
    assert changed.application.source.graph_name == str(E.G)
    other = prepare_rdf(_data(), limits=LIMITS)
    wrong = plan_rdf_patch(
        other,
        prepare_rdf(_data().add((E.other, E.x, E.y)), limits=LIMITS),
        limits=LIMITS,
    )
    assert wrong.patch is not None
    rejected = revalidate(start.state, wrong.patch, limits=LIMITS)
    assert rejected.state is None and not rejected.results
    assert any(
        i.code == "rdf.patch_precondition_failed" for i in rejected.report.issues
    )
    graph = _data()
    source = prepare_rdf(graph, limits=LIMITS)
    start = prepare_incremental(
        source, _shapes(), use_shapes=(str(E.S),), limits=LIMITS
    )
    assert start.state is not None
    graph.add((E.other, E.x, Literal("unrelated")))
    changed = revalidate(
        start.state, _patch(source, prepare_rdf(graph, limits=LIMITS)), limits=LIMITS
    )
    assert (
        changed.report.verified
        and changed.checked_pairs == 0
        and changed.reused_pairs == 2
    )
    assert not changed.results


@BACKEND
def test_changed_blank_delta_requires_actual_owned_binding_not_equal_label() -> None:
    before_node, after_node = BNode(), BNode()
    graph = (
        _data()
        .add((E.a, E.link, before_node))
        .add((before_node, E.code, Literal("old")))
    )
    source = prepare_rdf(graph, limits=LIMITS)
    other = (
        _data().add((E.a, E.link, after_node)).add((after_node, E.code, Literal("new")))
    )
    target = prepare_rdf(other, limits=LIMITS)
    owned = next(source._read().objects(E.a, E.link))
    changed = next(target._read().objects(E.a, E.link))
    assert isinstance(owned, BNode) and isinstance(changed, BNode)
    planned = plan_rdf_patch(
        source, target, blank_pairs=(BlankNodePair(owned, changed),), limits=LIMITS
    )
    assert planned.patch is not None
    token = next(
        q.subject.token
        for q in planned.patch.removals
        if isinstance(q.subject, PatchBlank)
    )
    prepared = prepare_incremental(
        source, _shapes(), use_shapes=(str(E.S),), limits=LIMITS
    )
    assert prepared.state is not None
    rejected = revalidate(
        prepared.state,
        planned.patch,
        blank_bindings=(PatchBlankBinding(token, BNode(str(owned))),),
        limits=LIMITS,
    )
    assert (
        rejected.state is None and not rejected.report.verified and not rejected.results
    )
    actual = revalidate(
        prepared.state,
        planned.patch,
        blank_bindings=(PatchBlankBinding(token, owned),),
        limits=LIMITS,
    )
    assert (
        actual.report.verified
        and actual.checked_pairs == 0
        and actual.reused_pairs == 2
    )

    # A data-only patch rematerializes blanks, so a fixed anonymous constant
    # shared with data has no established preserved cross-role binding.
    joint_node = BNode()
    data_role, shape_role = Dataset(), Dataset()
    data_role.default_graph.add((E.a, RDF.type, E.C)).add((E.a, E.p, joint_node))
    shape_role.default_graph.add((E.S, RDF.type, SH.NodeShape)).add(
        (E.S, SH.targetClass, E.C)
    )
    shape_role.default_graph.add((E.S, SH.property, E.P)).add((E.P, SH.path, E.p))
    shape_role.default_graph.add((E.P, SH.hasValue, joint_node))
    joint_source = PreparedSource._from_owned(
        data_role,
        is_dataset=False,
        document_iri=None,
        effective_url=None,
        literal_fidelity="supplied_terms",
    )
    joint_shapes = PreparedSource._from_owned(
        shape_role,
        is_dataset=False,
        document_iri=None,
        effective_url=None,
        literal_fidelity="supplied_terms",
    )
    joint = prepare_incremental(
        joint_source, joint_shapes, use_shapes=(str(E.S),), limits=LIMITS
    )
    assert joint.result is not None and joint.result.report.verified
    assert joint.state is None and joint.report.outcome == "unverified"
    shape_role.default_graph.set((E.P, SH.hasValue, BNode(str(joint_node))))
    independent = PreparedSource._from_owned(
        shape_role,
        is_dataset=False,
        document_iri=None,
        effective_url=None,
        literal_fidelity="supplied_terms",
    )
    distinct = prepare_incremental(
        joint_source, independent, use_shapes=(str(E.S),), limits=LIMITS
    )
    assert distinct.state is not None and distinct.report.outcome == "disagreement"


def test_missing_backend_metadata_bounds_cancellation_and_fresh_recovery() -> None:
    source = prepare_rdf(_data(), limits=LIMITS)
    if importlib.util.find_spec("pyshacl") is None:
        result = prepare_incremental(source, _shapes(), use_shapes=(str(E.S),))
        assert result.state is None and not result.report.verified
        assert result.result is not None and result.result.backend == "unavailable"
        assert result.result.report.outcome == "unverified"
        return
    giant = prepare_incremental(
        source,
        _shapes(),
        use_shapes=("urn:" + "x" * 100000,),
        limits=Limits(max_bytes=1000),
    )
    assert giant.report.outcome == "exhausted" and giant.state is None
    inherited_metadata = PreparedSource(
        Dataset(),
        is_dataset=False,
        document_iri="urn:" + "x" * 100000,
        effective_url=None,
        literal_fidelity="supplied_terms",
    )
    bounded = prepare_incremental(
        inherited_metadata,
        _shapes(),
        use_shapes=(str(E.S),),
        limits=Limits(max_bytes=1000),
    )
    assert bounded.report.outcome == "exhausted" and bounded.state is None
    cancel = Event()
    cancel.set()
    stopped = prepare_incremental(
        source, _shapes(), use_shapes=(str(E.S),), cancel=cancel
    )
    assert stopped.report.outcome == "exhausted" and stopped.state is None
    good = prepare_incremental(source, _shapes(), use_shapes=(str(E.S),), limits=LIMITS)
    assert good.report.verified and good.state is not None
    graph = _data().remove((E.a, E.p, None))
    delta = _patch(source, prepare_rdf(graph, limits=LIMITS))
    deadline = revalidate(good.state, delta, limits=LIMITS, timeout=0.001)
    assert deadline.state is None and deadline.report.outcome == "exhausted"
    fresh = revalidate(good.state, delta, limits=LIMITS)
    assert fresh.report.outcome == "disagreement" and fresh.checked_pairs == 1
    assert (E.a, E.p, Literal("a")) in good.state.source.graph()

    graph.remove((E.b, E.p, None))
    both = _patch(source, prepare_rdf(graph, limits=LIMITS))
    limited = revalidate(
        good.state,
        both,
        limits=Limits(
            max_issues=1,
            max_work=10000000,
            max_states=1000000,
            max_total_bytes=100000000,
        ),
    )
    assert limited.state is None and limited.report.outcome == "exhausted"
    assert sum(i.code == "shacl.nonconformance" for i in limited.report.issues) == 1
