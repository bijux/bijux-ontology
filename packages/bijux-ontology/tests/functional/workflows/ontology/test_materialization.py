"""Actual maintained retraction and revision publication without stale closure."""

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Protocol, cast

import pytest
from rdflib import Dataset, Graph, Literal, Namespace
from rdflib.namespace import RDF, RDFS
from rdflib.term import Node

from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.entailment import EntailmentResult, prepare_entailment
from bijux_ontology.rdf.migrations.candidates import MigrationCheck
from bijux_ontology.rdf.patches import plan_rdf_patch
from bijux_ontology.rdf.patches.types import DatasetPatch
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.sources.types import RdfSource
from bijux_ontology.shacl.rules.types import RuleMaterialization
from bijux_ontology.workflows.ontology.materialization import (
    EntailmentPlan,
    MaterializationRevision,
    MaterializationUpdate,
    RulePlan,
    prepare_materialization,
)

E = Namespace("urn:materialization:")
DOMAIN = (E.p, RDFS.domain, E.C)
SUPPORT = (E.s, E.p, E.o)
OTHER = (E.s, E.p, E.other)
TYPE = (E.s, RDF.type, E.C)


def data(*triples: tuple[Node, Node, Node]) -> PreparedSource:
    """Use maintained native preparation, not a replacement closure producer."""
    graph = Graph()
    for triple in triples:
        graph.add(triple)
    return prepare_rdf(graph)


def patch(before: PreparedSource, after: PreparedSource) -> DatasetPatch:
    plan = plan_rdf_patch(before, after)
    assert plan.report.verified and plan.patch is not None
    return plan.patch


class _Entailment(Protocol):
    asserted: PreparedSource
    derived: PreparedSource
    effective: PreparedSource


def receipt(update: MaterializationUpdate) -> MaterializationRevision:
    assert update.revision is not None
    return update.revision


def rule_effective(update: MaterializationUpdate) -> PreparedSource:
    assert update.report.verified and isinstance(
        update.materialization, RuleMaterialization
    )
    assert update.materialization.candidate is not None
    assert update.materialization.candidate.source is not None
    return update.materialization.candidate.source


def entailment(update: MaterializationUpdate) -> _Entailment:
    assert update.report.verified and isinstance(
        update.materialization, EntailmentResult
    )
    assert update.materialization.asserted is not None
    assert update.materialization.derived is not None
    assert update.materialization.effective is not None
    return cast(_Entailment, update.materialization)


def test_sole_and_independent_supports_agree_with_fresh_materialization() -> None:
    pytest.importorskip("owlrl")
    for assertions in ((DOMAIN, SUPPORT), (DOMAIN, SUPPORT, OTHER)):
        source = data(*assertions)
        ready = prepare_materialization(source, EntailmentPlan("rdfs"))
        assert ready.session is not None and ready.result.revision is not None
        assert TYPE in entailment(ready.result).derived.graph()
        target = data(*(t for t in assertions if t != SUPPORT))
        updated = ready.session.update(
            patch(source, target), expected=ready.result.revision
        )
        native = entailment(updated)
        fresh = prepare_entailment(target, regime="rdfs")
        assert fresh.report.verified and fresh.effective is not None
        assert set(native.effective.graph()) == set(fresh.effective.graph())
        assert (TYPE in native.derived.graph()) is (OTHER in assertions)
        assert updated.previous is ready.result.revision
        assert updated.revision is ready.session.current_revision
        assert receipt(updated).number == 2
        assert updated.strategy == "complete-recomputation"
        assert set(source.graph()) == set(assertions)


def test_asserted_duplicate_survives_removed_inferred_support_and_schema_changes() -> (
    None
):
    pytest.importorskip("owlrl")
    source = data(DOMAIN, SUPPORT, TYPE)
    ready = prepare_materialization(source, EntailmentPlan("rdfs"))
    assert ready.session is not None and ready.result.revision is not None
    assert TYPE not in entailment(ready.result).derived.graph()
    target = data(DOMAIN, TYPE)
    updated = ready.session.update(
        patch(source, target), expected=ready.result.revision
    )
    native = entailment(updated)
    assert TYPE in native.asserted.graph() and TYPE in native.effective.graph()
    assert TYPE not in native.derived.graph()
    schema = data((E.p, RDFS.domain, E.D), SUPPORT)
    changed = ready.session.update(patch(target, schema), expected=receipt(updated))
    effective = entailment(changed).effective.graph()
    assert TYPE not in effective and (E.s, RDF.type, E.D) in effective


PROGRAM = (
    """@prefix e:<urn:materialization:>. @prefix sh:<http://www.w3.org/ns/shacl#>.
e:Root a sh:NodeShape; sh:targetNode e:s; sh:rule [a sh:SPARQLRule;
"""
    'sh:construct "CONSTRUCT {?this e:available true} WHERE {'
    'FILTER NOT EXISTS {?this e:blocked true}}"].'
)


def rule_plan(*, available: bool, program: str = PROGRAM) -> RulePlan:
    """The output contract checks actual derived presence and original data scope."""

    def checked(source: PreparedSource) -> Report:
        found = (E.s, E.available, Literal(True)) in source.graph()
        return Report(
            "actual-availability",
            1,
            ()
            if found is available
            else (Issue("test.wrong", "Actual absence-rule consequence is incorrect"),),
        )

    return RulePlan(
        prepare_rdf(program, format="turtle"),
        (E.Root,),
        (MigrationCheck("availability", checked),),
        trusted=True,
    )


def test_nonmonotone_rules_and_replacement_recompute_from_private_asserted_input() -> (
    None
):
    pytest.importorskip("pyshacl")
    source = data((E.s, RDF.type, E.Record))
    plan = rule_plan(available=True)
    ready = prepare_materialization(source, plan)
    assert ready.session is not None and ready.result.revision is not None
    assert ready.result.report.verified
    assert isinstance(ready.result.materialization, RuleMaterialization)
    assert ready.result.materialization.passes == 2
    target = data((E.s, RDF.type, E.Record), (E.s, E.blocked, Literal(True)))
    updated = ready.session.update(
        patch(source, target),
        expected=ready.result.revision,
        plan=rule_plan(available=False),
    )
    assert updated.report.verified and isinstance(
        updated.materialization, RuleMaterialization
    )
    assert updated.materialization.candidate is not None
    assert (
        E.s,
        E.available,
        None,
    ) not in rule_effective(updated).graph()
    replaced = ready.session.update(
        None,
        expected=receipt(updated),
        plan=rule_plan(
            available=False,
            program=PROGRAM.replace("e:available true", "e:replacement true").replace(
                "FILTER NOT EXISTS {?this e:blocked true}", "?this e:blocked true"
            ),
        ),
    )
    assert replaced.report.verified and isinstance(
        replaced.materialization, RuleMaterialization
    )
    actual = rule_effective(replaced).graph()
    assert (E.s, E.replacement, Literal(True)) in actual and (
        E.s,
        E.available,
        None,
    ) not in actual

    rejected = ready.session.update(
        None, expected=receipt(replaced), plan=rule_plan(available=True)
    )
    assert rejected.report.outcome == "disagreement"
    assert rejected.materialization is None and rejected.revision is None
    assert ready.session.current_revision is replaced.revision
    unsupported = rule_plan(
        available=True,
        program=PROGRAM.replace(
            "FILTER NOT EXISTS {?this e:blocked true}",
            "SERVICE <https://example.invalid/query> {?this ?p ?o}",
        ),
    )
    refused = ready.session.update(None, expected=receipt(replaced), plan=unsupported)
    assert not refused.report.verified and refused.materialization is None
    assert ready.session.current_revision is replaced.revision


def test_failed_cancelled_exhausted_and_stale_updates_never_publish() -> None:
    pytest.importorskip("owlrl")
    source, target = data(DOMAIN, SUPPORT), data(DOMAIN)
    ready = prepare_materialization(source, EntailmentPlan("rdfs"))
    assert ready.session is not None and ready.result.revision is not None
    session, revision = ready.session, ready.result.revision
    other = prepare_materialization(source, EntailmentPlan("rdfs"))
    assert other.result.revision is not None
    assert other.result.revision.number == revision.number
    assert (
        session.update(
            None, expected=other.result.revision, plan=EntailmentPlan("rdfs")
        ).report.outcome
        == "input_error"
    )
    delta = patch(source, target)
    cancel = threading.Event()
    cancel.set()
    for kwargs in ({"cancel": cancel}, {"limits": Limits(max_work=1)}):
        failed = session.update(delta, expected=revision, **kwargs)
        assert failed.report.outcome == "exhausted"
        assert failed.revision is None and failed.materialization is None
        assert session.current_revision is revision
    wrong = session.update(patch(target, source), expected=revision)
    assert not wrong.report.verified and wrong.materialization is None
    assert session.current_revision is revision
    accepted = session.update(delta, expected=revision)
    assert accepted.report.verified
    stale = session.update(delta, expected=revision)
    assert stale.report.outcome == "input_error" and stale.materialization is None
    assert session.current_revision is accepted.revision
    session.close()
    assert (
        session.update(
            None, expected=receipt(accepted), plan=EntailmentPlan("rdfs")
        ).report.outcome
        == "input_error"
    )
    assert session.current_revision is None


def test_owned_metadata_graph_pin_and_repeated_shared_budget() -> None:
    pytest.importorskip("owlrl")
    dataset = Dataset()
    dataset.graph(E.selected).add(DOMAIN).add(SUPPORT)
    dataset.graph(E.hidden).add((E.s, RDF.type, E.Hidden))
    source = prepare_rdf(
        RdfSource(
            dataset,
            graph_name=str(E.selected),
            document_iri=str(E.document),
            effective_url=str(E.effective),
        )
    )
    budget = Budget()
    ready = prepare_materialization(source, EntailmentPlan("rdfs"), budget=budget)
    assert ready.session is not None and ready.result.revision is not None
    result = entailment(ready.result)
    assert result.effective.graph_name == str(E.selected)
    assert result.effective.document_iri == str(E.document)
    assert (E.s, RDF.type, E.Hidden) not in result.effective.graph()
    consumed = budget.remaining("work")
    # Mutating original and returned public descriptive fields cannot poison the
    # session's privately captured asserted input or its physical graph pin.
    source.graph_name = str(E.hidden)
    result.asserted.document_iri = "urn:changed"
    result.effective.graph_name = str(E.hidden)
    refreshed = ready.session.update(
        None, expected=ready.result.revision, plan=EntailmentPlan("rdfs"), budget=budget
    )
    assert refreshed.report.verified and budget.remaining("work") < consumed
    fresh = entailment(refreshed)
    assert fresh.effective.graph_name == str(E.selected)
    assert fresh.effective.document_iri == str(E.document)
    assert TYPE in fresh.derived.graph()


def test_concurrent_expected_revision_accepts_only_one_complete_update() -> None:
    pytest.importorskip("pyshacl")
    source = data((E.s, RDF.type, E.Record))
    ready = prepare_materialization(source, rule_plan(available=True))
    assert ready.session is not None and ready.result.revision is not None
    barrier = threading.Barrier(2)

    def coordinated(source: PreparedSource) -> Report:
        barrier.wait(timeout=10)
        return Report(
            "actual-original-type",
            1,
            ()
            if (E.s, RDF.type, E.Record) in source.graph()
            else (Issue("test.wrong", "Original assertion was lost"),),
        )

    plan = replace(
        rule_plan(available=True), checks=(MigrationCheck("coordinated", coordinated),)
    )

    session, expected = ready.session, ready.result.revision

    def update() -> MaterializationUpdate:
        return session.update(None, expected=expected, plan=plan)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: update(), range(2)))
    assert sorted(r.report.outcome for r in results) == ["agreement", "input_error"]
    accepted = next(r for r in results if r.report.verified)
    assert ready.session.current_revision is accepted.revision
    cancel = threading.Event()

    def cancelled_after_actual_output(source: PreparedSource) -> Report:
        assert (E.s, E.available, Literal(True)) in source.graph()
        cancel.set()
        return Report("actual-output-before-cancellation", 1)

    cancelled = ready.session.update(
        None,
        expected=receipt(accepted),
        plan=replace(
            rule_plan(available=True),
            checks=(
                MigrationCheck("cancel-after-output", cancelled_after_actual_output),
            ),
        ),
        cancel=cancel,
    )
    assert cancelled.report.outcome == "exhausted"
    assert cancelled.materialization is None
    assert ready.session.current_revision is accepted.revision
