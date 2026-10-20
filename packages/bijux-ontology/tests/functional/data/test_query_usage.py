"""Actual parser role evidence remains separate from dynamic dependency coverage."""

import json
import subprocess
import sys
from dataclasses import replace
from threading import Event
from types import FrameType
from typing import Literal as EventKind

import pytest
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF

from bijux_ontology.data.query_usage import (
    QueryUsage,
    check_query_usage,
    read_query_usage,
)
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import BUDGET_KINDS, Budget, BudgetKind, Limits
from bijux_ontology.execution.workers import execute_worker

EX = Namespace("urn:query-use:")
PREFIX = "PREFIX e:<urn:query-use:> "


def _roles(usage: QueryUsage, role: str) -> set[str]:
    return {ref.iri for ref in usage.references if ref.role == role}


def _vocabulary() -> Graph:
    graph = Graph()
    graph.add((EX.C, RDF.type, OWL.Class))
    for iri in (EX.p, EX.q, EX.label):
        graph.add((iri, RDF.type, RDF.Property))
    return graph


def test_static_roles_paths_and_real_missing_deprecated_checks() -> None:
    text = (
        PREFIX
        + """SELECT * WHERE {
        e:Data a e:C; e:p/^e:q ?o .
        FILTER(e:function(?o) && ?o="urn:query-use:fake")
    } # e:comment <urn:query-use:ignored>"""
    )
    usage = read_query_usage(text, source="consumer.rq")
    assert usage.report.verified, usage.report.to_dict()
    assert _roles(usage, "class") == {str(EX.C)}
    assert {str(EX.p), str(EX.q)} <= _roles(usage, "predicate")
    assert _roles(usage, "function") == {str(EX.function)}
    assert _roles(usage, "data") == {str(EX.Data)}
    assert str(EX.fake) not in {ref.iri for ref in usage.references}
    assert str(EX.ignored) not in {ref.iri for ref in usage.references}
    assert any(ref.iri == str(EX.q) and ref.reverse for ref in usage.references)
    assert all(
        ref.position and ref.location.startswith("consumer.rq#algebra/")
        for ref in usage.references
    )
    vocabulary = _vocabulary()
    vocabulary.add((EX.q, OWL.deprecated, Literal(True)))
    deprecated = check_query_usage(usage, vocabulary)
    assert any(
        issue.code == "query.vocabulary_deprecated" and issue.actual == str(EX.q)
        for issue in deprecated.issues
    )
    vocabulary.remove((EX.p, None, None))
    removed = check_query_usage(usage, vocabulary)
    assert any(
        issue.code == "vocabulary.term_missing" and issue.actual == str(EX.p)
        for issue in removed.issues
    )
    assert not any(
        issue.actual in (str(EX.Data), str(EX.function)) for issue in removed.issues
    )


def test_real_prefix_base_namespace_and_update_resolution() -> None:
    usage = read_query_usage(
        "BASE <https://example.org/base/> PREFIX e:<../schema/> "
        "CONSTRUCT {?s e:out ?o} WHERE {?s <relative> ?o}"
    )
    assert usage.report.verified, usage.report.to_dict()
    assert _roles(usage, "predicate") == {
        "https://example.org/schema/out",
        "https://example.org/base/relative",
    }
    supplied = read_query_usage(
        "SELECT * WHERE {?s e:p ?o}", namespaces=(("e", str(EX)),)
    )
    assert supplied.report.verified and _roles(supplied, "predicate") == {str(EX.p)}
    shifted = read_query_usage("SELECT * WHERE {?s <p> ?o}", base="urn:root:")
    assert not shifted.report.verified  # A nonhierarchical base cannot resolve p.
    changed = read_query_usage(
        "SELECT * WHERE {?s <p> ?o}", base="https://example.org/other/"
    )
    assert changed.report.verified and _roles(changed, "predicate") == {
        "https://example.org/other/p"
    }
    update = read_query_usage(
        "PREFIX e:<urn:old:> DELETE {?s e:p ?o} INSERT {?s e:q e:V} "
        "WHERE {?s e:p ?o}; PREFIX e:<urn:new:> "
        "INSERT DATA {e:S a e:C; e:r e:O}",
        kind="update",
    )
    assert update.report.verified, update.report.to_dict()
    assert {"urn:old:p", "urn:old:q", "urn:new:r"} <= _roles(update, "predicate")
    assert _roles(update, "class") == {"urn:new:C"}
    assert {"urn:old:V", "urn:new:S", "urn:new:O"} <= _roles(update, "data")


def test_values_bindings_are_qualified_by_real_role_and_scope() -> None:
    usage = read_query_usage(
        PREFIX + "SELECT * WHERE { VALUES ?p {e:p e:q} VALUES ?type {e:C} "
        "VALUES ?data {e:Data} ?s ?p ?o; a ?type }"
    )
    assert usage.report.verified, usage.report.to_dict()
    assert {str(EX.p), str(EX.q)} <= _roles(usage, "predicate")
    assert _roles(usage, "class") == {str(EX.C)}
    assert str(EX.Data) not in _roles(usage, "predicate")
    assert all(
        ref.binding_position
        for ref in usage.references
        if ref.iri in (str(EX.p), str(EX.q)) and ref.role == "predicate"
    )
    for text in (
        "SELECT * WHERE { VALUES ?p {e:p UNDEF} ?s ?p ?o }",
        "SELECT * WHERE { OPTIONAL {VALUES ?p {e:p}} ?s ?p ?o }",
        "SELECT * WHERE { VALUES ?p {e:p} {SELECT ?s WHERE {?s ?p ?o}} }",
    ):
        partial = read_query_usage(PREFIX + text)
        assert partial.report.outcome == "unverified" and not partial.report.complete
        assert str(EX.p) not in _roles(partial, "predicate")
        assert any(scope.role == "predicate" for scope in partial.dynamic)


def test_literal_datatypes_and_functions_have_distinct_positions() -> None:
    usage = read_query_usage(
        PREFIX + "PREFIX xsd:<http://www.w3.org/2001/XMLSchema#> "
        'SELECT (xsd:integer(?v) AS ?n) (STRDT("x",e:Datatype) AS ?s) '
        'WHERE {?r e:p "01"^^xsd:integer}'
    )
    assert usage.report.verified, usage.report.to_dict()
    assert str(EX.Datatype) in _roles(usage, "datatype")
    assert "http://www.w3.org/2001/XMLSchema#integer" in _roles(usage, "datatype")
    assert str(EX.Datatype) not in _roles(usage, "data")
    assert not _roles(usage, "class")


def test_dynamic_negative_and_malformed_programs_never_certify_empty_inventory() -> (
    None
):
    dynamic = read_query_usage(
        PREFIX + "SELECT * WHERE {?s e:label ?name . "
        'BIND(IRI(CONCAT("urn:property:",?name)) AS ?p) ?s ?p ?o}'
    )
    assert str(EX.label) in _roles(dynamic, "predicate")
    assert not dynamic.report.complete and dynamic.report.outcome == "unverified"
    negative = read_query_usage(PREFIX + "SELECT * WHERE {?s !(e:p|^e:q) ?o}")
    assert str(EX.p) in _roles(negative, "predicate")
    assert not negative.report.complete and negative.dynamic
    for text in ("SELECT WHERE {", "SELECT * WHERE {?s missing:p ?o}"):
        failed = read_query_usage(text)
        assert not failed.report.complete and failed.report.outcome == "input_error"
    unloaded = read_query_usage(
        "LOAD <urn:source> INTO GRAPH <urn:target>", kind="update"
    )
    assert unloaded.report.outcome == "unverified" and unloaded.dynamic
    for text, role in (
        ("SELECT * WHERE {?s a []}", "class"),
        ('SELECT (STRDT("x",COALESCE(?dtype,<urn:Known>)) AS ?v) WHERE {}', "datatype"),
    ):
        unresolved = read_query_usage(text)
        assert (
            unresolved.report.outcome == "unverified" and not unresolved.report.complete
        )
        assert any(scope.role == role for scope in unresolved.dynamic)
    inverse_type = read_query_usage(PREFIX + "SELECT * WHERE {e:C ^a ?s}")
    assert inverse_type.report.verified
    assert str(EX.C) in _roles(inverse_type, "class")
    assert not check_query_usage(inverse_type, Graph()).verified
    for path in ("a+", "e:p/a"):
        composite = read_query_usage(PREFIX + f"SELECT * WHERE {{?s {path} e:C}}")
        assert (
            composite.report.outcome == "unverified" and not composite.report.complete
        )
        assert str(EX.C) in _roles(composite, "class")
        assert any(scope.role == "class" for scope in composite.dynamic)
    assert read_query_usage("ASK {}").report.verified


def test_static_findings_and_dynamic_coverage_both_survive_checking() -> None:
    usage = read_query_usage(PREFIX + "SELECT * WHERE {?s e:missing ?o . ?s ?p ?x}")
    checked = check_query_usage(usage, _vocabulary())
    assert not checked.complete and not checked.verified
    assert any(issue.code == "vocabulary.term_missing" for issue in checked.issues)
    assert any(
        issue.code == "query.dependencies_unverified" for issue in checked.issues
    )
    assert checked.children[0] is usage.report
    with pytest.raises(InputError):
        QueryUsage()


def test_limits_cancellation_and_shared_allowances_cannot_pass() -> None:
    text = PREFIX + "SELECT * WHERE {?s e:p ?o}"
    for field in ("max_work", "max_states", "max_total_bytes"):
        limited = read_query_usage(text, limits=replace(Limits(), **{field: 1}))
        assert limited.report.outcome == "exhausted" and not limited.report.complete
    one_unknown = read_query_usage(
        "SELECT * WHERE {?s ?p ?o}", limits=Limits(max_issues=1)
    )
    assert one_unknown.report.outcome == "unverified"
    assert len(one_unknown.report.issues) == 1
    large = read_query_usage("SELECT * WHERE {?s <urn:large:" + "p" * 512 + "> ?o}")
    assert large.report.verified
    guarded = check_query_usage(large, Graph(), limits=Limits(max_bytes=128))
    assert guarded.outcome == "exhausted" and not guarded.complete
    budget = Budget()
    first = read_query_usage(text, budget=budget)
    consumed = budget.remaining("work")
    second = read_query_usage(text, budget=budget)
    assert first.report.verified and second.report.verified
    assert budget.remaining("work") < consumed
    cancel = Event()
    cancel.set()
    assert read_query_usage(text, cancel=cancel).report.outcome == "exhausted"
    assert read_query_usage(text, timeout=0.001).report.outcome == "exhausted"
    assert read_query_usage(text).report.verified


def test_parsed_dependencies_drive_actual_upgrade_checks_without_source_paths() -> None:
    """A removed used predicate breaks the consumer; dynamic scope stays required."""
    from bijux_ontology.workflows.upgrades.assessment import assess_upgrade
    from bijux_ontology.workflows.upgrades.uses import index_uses

    usage = read_query_usage(
        PREFIX + "SELECT * WHERE {?s a e:C; e:p ?v}", source="consumer.rq"
    )
    indexed = index_uses(query_usages=(usage,))
    assert indexed.report.verified
    assert all(use.path is None for use in indexed.entries)
    assert any(
        use.iri == str(EX.p) and use.role == "predicate" for use in indexed.entries
    )
    before = _vocabulary()
    after = Graph() + before
    after.remove((EX.p, None, None))
    result = assess_upgrade(before, after, query_usages=(usage,))
    assert result.verdict == "demonstrated-break", result.report.to_dict()
    assert any(
        impact.use.iri == str(EX.p)
        and impact.use.location.startswith("consumer.rq#algebra/")
        for impact in result.affected
    )
    dynamic = read_query_usage(PREFIX + "SELECT * WHERE {?s e:p ?v . ?s ?p ?o}")
    partial = index_uses(query_usages=(dynamic,))
    assert not partial.report.complete
    assert any(use.iri == str(EX.p) for use in partial.entries)
    assert partial.report.children[0] is dynamic.report
    unchanged = assess_upgrade(before, before, query_usages=(dynamic,))
    assert not unchanged.report.verified and unchanged.verdict == "unverified"


@pytest.mark.parametrize("phase", ("complete", "invalid", "admission"))
def test_native_parser_terminal_costs_and_findings_survive_inherited_cancellation(
    phase: str,
) -> None:
    """Settle actual parsing work without refunding a worker stopped without receipt."""
    text = "SELECT WHERE {" if phase == "invalid" else "SELECT ?s WHERE {?s <urn:p> ?o}"
    limits = Limits()
    baseline = read_query_usage(text, limits=limits)
    assert baseline.report.outcome == (
        "input_error" if phase == "invalid" else "agreement"
    )
    cancellation = Event()
    ledger = Budget(limits)
    parent = Operation(limits, ledger, 30, cancellation)
    native: list[bytes] = []
    processes: list[subprocess.Popen[bytes]] = []
    before: dict[BudgetKind, int] = {}
    previous = sys.getprofile()

    def observe(
        frame: FrameType,
        event: EventKind["call", "return", "c_call", "c_return", "c_exception"],
        returned: object,
    ) -> None:
        if previous is not None:
            previous(frame, event, returned)
        if event != "return":
            return
        process = frame.f_locals.get("self")
        if (
            frame.f_code is subprocess.Popen.__init__.__code__
            and isinstance(process, subprocess.Popen)
            and isinstance(process.args, list)
            and "bijux_ontology.data.query_usage.worker" in process.args
        ):
            processes.append(process)
            if phase == "admission":
                cancellation.set()
        if (
            frame.f_code is execute_worker.__code__
            and frame.f_locals.get("module") == "bijux_ontology.data.query_usage.worker"
            and isinstance(returned, bytes)
        ):
            native.append(returned)
            before.update((kind, ledger.remaining(kind)) for kind in BUDGET_KINDS)
            cancellation.set()

    sys.setprofile(observe)
    try:
        stopped = read_query_usage(text, budget=parent)
    finally:
        sys.setprofile(previous)
    assert cancellation.is_set()
    assert len(processes) == 1 and processes[0].returncode is not None
    if phase == "admission":
        assert not native and stopped.report.outcome == "exhausted"
        assert all(ledger.remaining(kind) == 0 for kind in BUDGET_KINDS)
    else:
        assert len(native) == 1
        assert stopped.report == baseline.report
        assert stopped.references == baseline.references
        consumed = json.loads(native[0])["consumed"]
        assert all(
            ledger.remaining(kind) == before[kind] - consumed[kind]
            for kind in BUDGET_KINDS
        )
    cancellation.clear()
    recovered = read_query_usage(text, budget=parent)
    if phase == "admission":
        assert recovered.report.outcome == "exhausted"
        assert read_query_usage(text, limits=limits).report.verified
    else:
        assert recovered.report == baseline.report
