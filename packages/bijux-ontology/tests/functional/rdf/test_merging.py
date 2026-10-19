"""Actual common-base merge decisions preserve exact structure and acceptance."""

import threading
from dataclasses import replace
from typing import Literal as TypeLiteral
from typing import cast

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace
from rdflib.namespace import RDF, XSD

from bijux_ontology.declarations.path_fields import PathFieldDeclaration
from bijux_ontology.declarations.types import ValueDeclaration
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.models.path_fields import PathFieldUsage, check_path_fields
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.merging import (
    MergeCorrespondence,
    MergeResolution,
    RdfMergePlan,
    RdfMergeResult,
    apply_rdf_merge,
    plan_rdf_merge,
)
from bijux_ontology.rdf.migrations.candidates import MigrationCheck
from bijux_ontology.rdf.patches.types import BlankNodePair
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import RdfSource

EX = Namespace("urn:merge:")


def _checks() -> tuple[MigrationCheck, ...]:
    """A real RDF-side contract requires the original record to stay present."""

    def exists(source: PreparedSource) -> Report:
        graph = source.graph(
            "@default" if source.is_dataset and source.graph_name is None else None
        )
        return Report(
            "record",
            1,
            ()
            if (EX.s, RDF.type, EX.Record) in graph
            else (
                Issue(
                    "merge.record_missing", "The required original record disappeared"
                ),
            ),
        )

    return (MigrationCheck("record", exists),)


def _source(*triples: tuple[object, object, object]) -> PreparedSource:
    """Own a native graph with an explicit application record baseline."""
    graph = Graph().add((EX.s, RDF.type, EX.Record))
    for s, p, o in triples:
        graph.add((s, p, o))  # type: ignore[arg-type]
    return prepare_rdf(graph)


def test_disjoint_and_identical_edits_preserve_exact_terms() -> None:
    """Different properties merge; exact 01 is not silently normalized to 1."""
    original = (EX.s, EX.p, Literal("01", datatype=XSD.integer, normalize=False))
    base = _source(original)
    left = _source((EX.s, EX.p, Literal("1", datatype=XSD.integer, normalize=False)))
    right = _source(original, (EX.s, EX.q, EX.extra))
    before = frozenset(base.graph())
    plan = plan_rdf_merge(base, left, right)
    assert plan.report.verified and not plan.conflicts
    merged = apply_rdf_merge(
        base, plan, before_checks=_checks(), after_checks=_checks()
    )
    assert merged.structural_report.verified and merged.report.verified
    assert merged.candidate is not None and merged.candidate.source is not None
    graph = merged.candidate.source.graph()
    assert (EX.s, EX.q, EX.extra) in graph
    assert str(graph.value(EX.s, EX.p)) == "1"
    assert frozenset(base.graph()) == before
    identical = plan_rdf_merge(base, left, left)
    assert not identical.conflicts and identical.report.verified
    result = apply_rdf_merge(
        base, identical, before_checks=_checks(), after_checks=_checks()
    )
    assert result.report.verified


def test_property_conflict_requires_explicit_component_resolution() -> None:
    """Choosing one conflict never loses unrelated branch changes or raw history."""
    base = _source((EX.s, EX.p, EX.old))
    left = _source((EX.s, EX.p, EX.left), (EX.s, EX.leftOnly, EX.keep))
    right = _source((EX.s, EX.p, EX.right), (EX.s, EX.rightOnly, EX.keep))
    plan = plan_rdf_merge(base, left, right)
    assert len(plan.conflicts) == 1 and plan.conflicts[0].kind == "property"
    assert plan.report.outcome == "disagreement"
    unresolved = apply_rdf_merge(base, plan)
    assert unresolved.candidate is None and unresolved.report.outcome == "unverified"
    for choice, expected in (("left", EX.left), ("right", EX.right), ("base", EX.old)):
        result = apply_rdf_merge(
            base,
            plan,
            resolutions=(
                MergeResolution(0, cast(TypeLiteral["base", "left", "right"], choice)),
            ),
            before_checks=_checks(),
            after_checks=_checks(),
        )
        assert result.report.verified and result.plan.report.outcome == "disagreement"
        assert result.candidate is not None and result.candidate.source is not None
        graph = result.candidate.source.graph()
        assert set(graph.objects(EX.s, EX.p)) == {expected}
        assert (EX.s, EX.leftOnly, EX.keep) in graph and (
            EX.s,
            EX.rightOnly,
            EX.keep,
        ) in graph
    with pytest.raises(InputError):
        apply_rdf_merge(
            base,
            plan,
            resolutions=(MergeResolution(0, "left"), MergeResolution(0, "right")),
        )
    with pytest.raises(InputError):
        RdfMergePlan()
    with pytest.raises(TypeError):
        replace(plan, conflicts=())


def test_different_list_cells_shared_tail_and_graph_inventory_conflict() -> None:
    """Linked cells form one overlap even when branches edit different positions."""

    def chain(first: str, last: str, *, repoint: bool = False) -> PreparedSource:
        return _source(
            (EX.s, EX.items, EX.tail if repoint else EX.head),
            (EX.head, RDF.first, Literal(first)),
            (EX.head, RDF.rest, EX.tail),
            (EX.tail, RDF.first, Literal(last)),
            (EX.tail, RDF.rest, RDF.nil),
            (EX.other, EX.items, EX.tail),
        )

    base, left, right = chain("a", "b"), chain("A", "b"), chain("a", "B")
    plan = plan_rdf_merge(base, left, right)
    assert len(plan.conflicts) == 1 and plan.conflicts[0].kind == "list"
    assert apply_rdf_merge(base, plan).candidate is None
    repointed = plan_rdf_merge(base, chain("a", "b", repoint=True), right)
    assert len(repointed.conflicts) == 1 and repointed.conflicts[0].kind == "list"
    resolved = apply_rdf_merge(
        base,
        repointed,
        resolutions=(MergeResolution(0, "right"),),
        before_checks=_checks(),
        after_checks=_checks(),
    )
    assert resolved.report.verified
    assert resolved.candidate is not None and resolved.candidate.source is not None
    assert resolved.candidate.source.graph().value(EX.s, EX.items) == EX.head

    a, b, c = Dataset(), Dataset(), Dataset()
    for dataset in (a, b, c):
        dataset.default_graph.add((EX.s, RDF.type, EX.Record))
        dataset.graph(EX.Empty)
    a.graph(EX.G).add((EX.s, EX.p, EX.old))
    c.graph(EX.G).add((EX.s, EX.p, EX.new))
    b.default_graph.add((EX.s, EX.unrelated, EX.keep))
    owned = prepare_rdf(a)
    graphplan = plan_rdf_merge(owned, prepare_rdf(b), prepare_rdf(c))
    assert len(graphplan.conflicts) == 1 and graphplan.conflicts[0].kind == "graph"
    chosen = apply_rdf_merge(
        owned,
        graphplan,
        resolutions=(MergeResolution(0, "right"),),
        before_checks=_checks(),
        after_checks=_checks(),
    )
    assert (
        chosen.report.verified
        and chosen.candidate is not None
        and chosen.candidate.source is not None
    )
    output = chosen.candidate.source
    assert (EX.s, EX.unrelated, EX.keep) in output.graph("@default")
    assert (EX.s, EX.p, EX.new) in output.graph(str(EX.G))
    assert not output.graph(str(EX.Empty))
    same = plan_rdf_merge(owned, prepare_rdf(b), prepare_rdf(b))
    assert same.report.verified and not same.conflicts


def test_blank_ownership_cosmetic_relabel_and_affected_ambiguity() -> None:
    """Parser labels never choose among symmetric affected original resources."""

    def graph(first: str, second: str, changed: bool = False) -> PreparedSource:
        return prepare_rdf(
            f"@prefix e:<{EX}>. e:s a e:Record;e:p _:{first},_:{second}. "
            f'_:{first} e:value "{"changed" if changed else "same"}". '
            f'_:{second} e:value "same".',
            format="turtle",
        )

    base, cosmetic, changed = graph("a", "b"), graph("x", "y"), graph("j", "k", True)
    right_graph = Graph()
    for triple in cosmetic.graph():
        right_graph.add(triple)
    right_graph.add((EX.s, EX.ground, EX.keep))
    right = prepare_rdf(right_graph)
    plan = plan_rdf_merge(base, cosmetic, right)
    assert plan.report.verified and not plan.conflicts
    assert apply_rdf_merge(
        base, plan, before_checks=_checks(), after_checks=_checks()
    ).report.verified
    unknown = plan_rdf_merge(base, changed, cosmetic)
    assert (
        unknown.report.outcome == "unverified"
        and apply_rdf_merge(base, unknown).candidate is None
    )
    old = tuple(cast(BNode, node) for node in base.graph().objects(EX.s, EX.p))
    new = tuple(cast(BNode, node) for node in changed.graph().objects(EX.s, EX.p))
    declared = MergeCorrespondence(
        tuple(BlankNodePair(a, b) for a, b in zip(old, new, strict=True))
    )
    explicit = plan_rdf_merge(base, changed, cosmetic, left_correspondence=declared)
    assert explicit.report.verified
    assert apply_rdf_merge(
        base, explicit, before_checks=_checks(), after_checks=_checks()
    ).report.verified
    independent = graph("independentA", "independentB")
    ambiguous = apply_rdf_merge(
        independent, explicit, before_checks=_checks(), after_checks=_checks()
    )
    assert ambiguous.candidate is None and ambiguous.report.outcome == "unverified"
    shared_base = prepare_rdf(
        f"@prefix e:<{EX}>. e:s a e:Record. e:G{{}} e:H{{}}",
        format="trig",
        require_complete_graphs=True,
    )
    shared_left = prepare_rdf(
        f"@prefix e:<{EX}>. e:s a e:Record. "
        "e:G {e:s e:child _:x.} e:H {e:other e:child _:x.} "
        '_:x {_:x e:label "owned".}',
        format="trig",
        require_complete_graphs=True,
    )
    shared_right = prepare_rdf(
        f"@prefix e:<{EX}>. e:s a e:Record. e:G{{}} e:H{{e:other e:ground e:extra.}}",
        format="trig",
        require_complete_graphs=True,
    )
    shared_plan = plan_rdf_merge(shared_base, shared_left, shared_right)
    shared_result = apply_rdf_merge(
        shared_base, shared_plan, before_checks=_checks(), after_checks=_checks()
    )
    assert shared_result.report.verified
    assert (
        shared_result.candidate is not None
        and shared_result.candidate.source is not None
    )
    shared_output = shared_result.candidate.source
    node = shared_output.graph(str(EX.G)).value(EX.s, EX.child)
    assert isinstance(node, BNode)
    assert shared_output.graph(str(EX.H)).value(EX.other, EX.child) == node
    assert node in {g.identifier for g in shared_output.dataset().graphs()}
    assert (EX.other, EX.ground, EX.extra) in shared_output.graph(str(EX.H))
    foreign = MergeCorrespondence((BlankNodePair(BNode(str(old[0])), new[0]),))
    assert (
        plan_rdf_merge(
            base, changed, cosmetic, left_correspondence=foreign
        ).report.outcome
        == "unverified"
    )


def test_pinned_scope_current_base_after_contracts_and_shared_limits() -> None:
    """Required checks and exact current base remain distinct from structural merge."""
    base, left, right = (
        _source((EX.s, EX.p, EX.old)),
        _source((EX.s, EX.p, EX.new)),
        _source((EX.s, EX.p, EX.old), (EX.s, EX.q, EX.keep)),
    )
    plan = plan_rdf_merge(base, left, right)
    missing = apply_rdf_merge(base, plan)
    assert (
        missing.candidate is not None
        and missing.structural_report.verified
        and not missing.report.verified
    )
    failure = MigrationCheck(
        "actual-count",
        lambda source: Report(
            "actual-count",
            1,
            ()
            if len(source.graph()) == 1
            else (
                Issue("merge.actual_count", "Actual candidate has too many assertions"),
            ),
        ),
    )
    failed = apply_rdf_merge(
        base, plan, before_checks=_checks(), after_checks=(failure,)
    )
    assert failed.candidate is not None and failed.report.outcome == "disagreement"
    assert any(i.code == "merge.actual_count" for i in failed.report.issues)
    assert apply_rdf_merge(right, plan).candidate is None
    for limit in (Limits(max_work=1), Limits(max_states=1), Limits(max_bytes=100)):
        stopped = plan_rdf_merge(base, left, right, limits=limit)
        assert stopped.report.outcome == "exhausted"
        assert apply_rdf_merge(base, stopped).candidate is None
    cancel = threading.Event()
    cancel.set()
    assert (
        plan_rdf_merge(base, left, right, cancel=cancel).report.outcome == "exhausted"
    )
    budget = Budget(Limits(max_work=100000))
    shared = plan_rdf_merge(base, left, right, budget=budget)
    assert shared.report.verified and budget.remaining("work") < budget.limits.max_work
    budget.consume("work", budget.remaining("work"))
    assert apply_rdf_merge(base, shared, budget=budget).report.outcome == "exhausted"
    sources = []
    for hidden in (EX.old, EX.new, EX.third):
        dataset = Dataset()
        dataset.graph(EX.G).add((EX.s, RDF.type, EX.Record))
        dataset.graph(EX.hidden).add((EX.s, EX.p, hidden))
        sources.append(prepare_rdf(RdfSource(dataset, graph_name=str(EX.G))))
    scoped = plan_rdf_merge(*sources)
    assert scoped.report.verified and not scoped.conflicts
    actual = apply_rdf_merge(
        sources[0], scoped, before_checks=_checks(), after_checks=_checks()
    )
    assert (
        actual.report.verified
        and actual.candidate is not None
        and actual.candidate.source is not None
    )
    assert actual.candidate.source.graph_name == str(EX.G)
    assert EX.hidden not in {
        g.identifier for g in actual.candidate.source.dataset().graphs()
    }


def test_native_gate_paid_finding_survives_completed_callback_boundary() -> None:
    """A real native comparison survives later shared-budget handoff exhaustion."""
    base = _source((EX.s, EX.p, Literal("bad")))
    left = _source((EX.s, EX.p, Literal("bad")), (EX.s, EX.q, EX.keep))
    plan = plan_rdf_merge(base, left, base)
    field = PathFieldDeclaration(
        "p", EX.p, (ValueDeclaration("literal", datatype=str(XSD.integer)),)
    )
    observed: list[int] = []

    def run(cap: int, *, baseline: bool = False) -> RdfMergeResult:
        operation = Budget(Limits(max_work=cap))

        def actual(source: PreparedSource) -> Report:
            native = check_path_fields(
                source, (PathFieldUsage(field, EX.s),), budget=operation
            )
            observed.append(cap - operation.remaining("work"))
            return native

        actual_checks = (MigrationCheck("actual", actual),)
        return apply_rdf_merge(
            base,
            plan,
            before_checks=actual_checks if baseline else _checks(),
            after_checks=_checks() if baseline else actual_checks,
            budget=operation,
        )

    complete = run(100000)
    assert complete.report.outcome == "disagreement"
    native_end = observed[-1]
    boundary = run(native_end)
    assert boundary.report.outcome == "exhausted"
    assert any(
        issue.code == "emission.value_mismatch" for issue in boundary.report.issues
    )
    baseline = run(100000, baseline=True)
    assert baseline.report.verified
    baseline_end = observed[-1]
    stopped = run(baseline_end, baseline=True)
    assert stopped.report.outcome == "exhausted" and stopped.candidate is not None
    assert any(
        issue.code == "emission.value_mismatch"
        for child in stopped.candidate.before
        for issue in child.issues
    )
    assert not any(
        issue.code == "emission.value_mismatch" for issue in stopped.report.issues
    )
    for before in (False, True):
        huge = MigrationCheck(
            "unqualified",
            lambda _: Report("unqualified", 1, (Issue("merge.huge", "x" * 100000),)),
        )
        invalid = apply_rdf_merge(
            base,
            plan,
            before_checks=(huge,) if before else _checks(),
            after_checks=_checks() if before else (huge,),
            limits=Limits(max_bytes=10000),
        )
        assert invalid.report.outcome == "exhausted"
        assert invalid.candidate is not None
        assert all(
            len(issue.message) < 10000
            for child in (
                *invalid.candidate.before,
                *invalid.candidate.after,
                invalid.report,
            )
            for issue in child.issues
        )
    first = plan_rdf_merge(
        _source((EX.s, EX.p, EX.old), (EX.s, EX.q, EX.old)),
        _source((EX.s, EX.p, EX.left), (EX.s, EX.q, EX.left)),
        _source((EX.s, EX.p, EX.right), (EX.s, EX.q, EX.right)),
        limits=Limits(max_issues=1),
    )
    assert first.report.outcome == "exhausted"
    assert len(first.conflicts) >= 1
    assert any(issue.code == "rdf.merge_conflict" for issue in first.report.issues)
