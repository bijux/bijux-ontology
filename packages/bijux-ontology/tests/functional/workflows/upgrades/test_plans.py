"""Ordered real migrations retain exact final truth and transactional ownership."""

import threading
from dataclasses import replace

import pytest
from rdflib import Dataset, Graph, Literal, Namespace
from rdflib.namespace import RDF, XSD

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.migrations.candidates import (
    MigrationCandidate,
    MigrationCheck,
    finish_migration,
)
from bijux_ontology.rdf.migrations.lists import ListReorder, ListTarget, migrate_list
from bijux_ontology.rdf.migrations.values import (
    LiteralConversion,
    ValueSelection,
    migrate_values,
)
from bijux_ontology.rdf.patches.planning import plan_rdf_patch
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.rdf.structure import read_rdf_list
from bijux_ontology.rdf.transforms import remap_iris
from bijux_ontology.workflows.upgrades.plans import (
    MigrationStep,
    execute_migration_plan,
    export_migration_plan,
)

E = Namespace("urn:plan:")


def _check(source: PreparedSource) -> Report:
    """An actual nonempty assertion inventory, independent of expected output."""
    size = len(source.graph())
    return Report("assertions", size, requested=max(1, size))


CHECK = (MigrationCheck("assertions", _check),)


def _source() -> PreparedSource:
    graph = Graph()
    for triple in (
        (E.old, E.amount, Literal("01")),
        (E.old, E.items, E.head),
        (E.head, RDF.first, E.a),
        (E.head, RDF.rest, E.tail),
        (E.tail, RDF.first, E.b),
        (E.tail, RDF.rest, RDF.nil),
    ):
        graph.add(triple)
    return prepare_source(graph)


def _iri(source: PreparedSource, budget: Budget) -> MigrationCandidate:
    """Compose the existing exact IRI transformation and ordinary patch checker."""
    transformed = remap_iris(source, {str(E.old): str(E.current)}, budget=budget)
    if transformed.source is None:
        return MigrationCandidate(None, None, (), (), transformed.report)
    planned = plan_rdf_patch(source, transformed.source, budget=budget)
    return finish_migration(
        source,
        transformed,
        planned.patch,
        before_checks=CHECK,
        after_checks=CHECK,
        budget=Operation(None, budget, 10, None),
    )


def _value(source: PreparedSource, budget: Budget) -> MigrationCandidate:
    return migrate_values(
        source,
        ValueSelection((str(E.amount),)),
        LiteralConversion(str(XSD.string), str(XSD.integer)),
        before_checks=CHECK,
        after_checks=CHECK,
        budget=budget,
    )


def _list(source: PreparedSource, budget: Budget) -> MigrationCandidate:
    return migrate_list(
        source,
        ListTarget(E.current, E.items, E.head),
        ListReorder((1, 0)),
        expected_before=(E.a, E.b),
        expected_after=(E.b, E.a),
        before_checks=CHECK,
        after_checks=CHECK,
        budget=budget,
    )


def _steps() -> tuple[MigrationStep, ...]:
    return tuple(
        MigrationStep(name, fn, CHECK, CHECK)
        for name, fn in (
            ("identifiers", _iri),
            ("values", _value),
            ("list-order", _list),
        )
    )


def _final(source: PreparedSource) -> Report:
    graph = source.graph()
    value = graph.value(E.current, E.amount)
    head = graph.value(E.current, E.items)
    actual = (
        str(value),
        getattr(value, "datatype", None),
        read_rdf_list(graph, head) if head is not None else None,
    )
    expected = ("01", XSD.integer, (E.b, E.a))
    return Report(
        "actual-final",
        1,
        ()
        if actual == expected
        else (
            Issue(
                "example.final_mismatch",
                "Final identifier, literal or list differs",
            ),
        ),
    )


FINAL = (MigrationCheck("actual-final", _final),)


def test_iri_value_and_list_plan_exports_only_exact_final_state() -> None:
    source = _source()
    before = set(source.graph())
    result = execute_migration_plan(source, _steps(), final_checks=FINAL)
    assert result.report.verified and result.source is not None, result.report.to_dict()
    assert len(result.steps) == 3 and all(step.report.verified for step in result.steps)
    assert set(source.graph()) == before
    assert export_migration_plan(result, format="trig").content is None
    exported = export_migration_plan(result, accept=True, format="trig")
    assert exported.report.verified and exported.content is not None
    assert _final(
        prepare_source(
            RdfSource(exported.content, format="trig", graph_name="@default")
        )
    ).verified
    with pytest.raises((InputError, TypeError)):
        replace(result, source=source)
    result.source.is_dataset = True
    altered = export_migration_plan(result, accept=True, format="trig")
    assert altered.content is None and not altered.report.verified


def test_midway_precondition_postcondition_and_final_failures_block_acceptance() -> (
    None
):
    source = _source()
    before = set(source.graph())
    failure = (
        MigrationCheck(
            "consumer",
            lambda current: Report(
                "actual-failure",
                1,
                (Issue("example.failed", "Consumer obligation failed"),),
            ),
        ),
    )
    for steps, final in (
        ((_steps()[0], replace(_steps()[1], preconditions=failure)), FINAL),
        ((_steps()[0], replace(_steps()[1], postconditions=failure)), FINAL),
        (_steps(), failure),
    ):
        result = execute_migration_plan(source, steps, final_checks=final)
        assert result.source is None and not result.report.verified
        assert any(i.code == "example.failed" for i in result.report)
        assert export_migration_plan(result, accept=True, format="trig").content is None
        assert set(source.graph()) == before
    missing = execute_migration_plan(source, _steps(), final_checks=())
    assert missing.source is None and missing.report.outcome == "unverified"


def test_stale_patch_and_callback_scope_changes_cannot_authorize_a_new_base() -> None:
    source = _source()
    stale = _iri(source, Budget())
    repeated = MigrationStep("stale", lambda current, budget: stale, CHECK, CHECK)
    failed = execute_migration_plan(source, (_steps()[0], repeated), final_checks=FINAL)
    assert failed.source is None and not failed.report.verified
    assert failed.steps[0].report.verified and failed.steps[-1].name == "stale"
    dataset = Dataset()
    dataset.graph(E.selected).add((E.old, E.amount, Literal("01")))
    dataset.graph(E.hidden).add((E.old, E.amount, Literal("hidden")))
    owned = prepare_source(dataset)

    def narrowed(current: PreparedSource, budget: Budget) -> MigrationCandidate:
        current.graph_name = str(E.selected)
        return _iri(current, budget)

    narrowed_step = MigrationStep(
        "narrowed",
        narrowed,
        (
            MigrationCheck(
                "inventory", lambda current: Report("inventory", len(current.dataset()))
            ),
        ),
        CHECK,
    )
    rejected = execute_migration_plan(owned, (narrowed_step,), final_checks=CHECK)
    assert rejected.source is None and not rejected.report.verified
    assert owned.graph_name is None and len(owned.dataset()) == 2
    inventory = (
        MigrationCheck(
            "inventory", lambda current: Report("inventory", len(current.dataset()))
        ),
    )

    def unchanged(current: PreparedSource, budget: Budget) -> MigrationCandidate:
        planned = plan_rdf_patch(current, current, budget=budget)
        return MigrationCandidate(current, planned.patch, (), (), planned.report)

    complete = execute_migration_plan(
        owned,
        (MigrationStep("unchanged", unchanged, inventory, inventory),),
        final_checks=inventory,
    )
    assert complete.report.verified and complete.source is not None
    assert export_migration_plan(complete, accept=True, format="trig").report.verified
    complete.source.graph_name = str(E.selected)
    narrowed_export = export_migration_plan(complete, accept=True, format="trig")
    assert narrowed_export.content is None
    assert any(i.code == "upgrade.plan_final_changed" for i in narrowed_export.report)


def test_limits_preserve_failed_prefix_and_cancellation_allows_fresh_recovery() -> None:
    source = _source()
    findings = (
        Issue("example.first", "Known failure"),
        Issue("example.second", "Other failure"),
    )
    failure = MigrationCheck("consumer", lambda current: Report("failed", 1, findings))
    step = replace(_steps()[0], preconditions=(failure,))
    result = execute_migration_plan(
        source, (step,), final_checks=FINAL, limits=Limits(max_issues=1)
    )
    assert result.source is None and result.report.outcome == "exhausted"
    assert any(i.code == "example.first" for i in result.report)
    single = replace(
        step,
        preconditions=(
            MigrationCheck("single", lambda current: Report("failed", 1, findings[:1])),
        ),
    )
    once = execute_migration_plan(
        source, (single,), final_checks=FINAL, limits=Limits(max_issues=1)
    )
    assert once.report.outcome == "disagreement"
    cancel = threading.Event()
    cancel.set()
    stopped = execute_migration_plan(
        source, _steps(), final_checks=FINAL, cancel=cancel
    )
    assert stopped.source is None and stopped.report.outcome == "exhausted"
    assert execute_migration_plan(source, _steps(), final_checks=FINAL).report.verified
