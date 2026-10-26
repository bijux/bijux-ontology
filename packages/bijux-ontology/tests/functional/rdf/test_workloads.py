"""Real maintained local engines over explicit owned finite consumer workloads."""

import threading
from dataclasses import FrozenInstanceError
from typing import cast

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import XSD

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.queries.native.capture import capture_selected_input
from bijux_ontology.rdf.queries.native.observation import observe_query
from bijux_ontology.rdf.queries.native.types import NativeQueryInput
from bijux_ontology.rdf.queries.types import QueryScope
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.workloads import (
    EngineConfig,
    QueryBinding,
    WorkloadQuery,
    qualify_workload,
)

EX = Namespace("https://example.org/workload/")
SCOPE = QueryScope("dataset")
PREFIX = f"PREFIX ex: <{EX}> "


def _source() -> PreparedSource:
    """Authored default, named data and an explicitly present empty graph."""
    dataset = Dataset(default_union=False)
    for subject in (EX.A, EX.B):
        dataset.default_graph.add(
            (subject, EX.number, Literal("1", datatype=XSD.integer, normalize=False))
        )
    dataset.default_graph.add((EX.A, EX.knows, EX.B))
    dataset.default_graph.add((EX.B, EX.label, Literal("B", lang="en")))
    dataset.graph(EX.named).add(
        (EX.C, EX.number, Literal("1", datatype=XSD.integer, normalize=False))
    )
    dataset.graph(EX.named).add((EX.A, EX.marker, Literal("named")))
    dataset.graph(EX.empty)
    return prepare_rdf(dataset)


def test_common_consumer_joins_named_optional_numeric_bags_and_ties() -> None:
    queries = (
        WorkloadQuery(
            "join",
            PREFIX
            + "SELECT ?s ?label WHERE {?s ex:knows ?target . ?target ex:label ?label}",
        ),
        WorkloadQuery(
            "named", PREFIX + "SELECT ?g ?value WHERE {GRAPH ?g {?s ex:marker ?value}}"
        ),
        WorkloadQuery(
            "optional",
            PREFIX + "SELECT ?s ?missing WHERE {?s ex:number ?n "
            "OPTIONAL {?s ex:absent ?missing}}",
        ),
        WorkloadQuery(
            "numeric",
            PREFIX
            + "SELECT (SUM(?n) AS ?sum) (COUNT(?s) AS ?count) WHERE {?s ex:number ?n}",
        ),
        WorkloadQuery(
            "ties", PREFIX + "SELECT ?s ?n WHERE {?s ex:number ?n} ORDER BY ?n"
        ),
    )
    result = qualify_workload(_source(), queries, scope=SCOPE)
    assert result.report.verified, result.report.to_dict()
    assert [item.status for item in result.queries] == ["agreeing"] * 5
    for item in result.queries:
        assert item.report.regime == "answer-bag"
        assert [answer.config.engine for answer in item.engines] == [
            "rdflib",
            "pyoxigraph",
        ]
        assert all(
            answer.version
            and answer.python_version
            and answer.answer is not None
            and answer.answer.external_complete is True
            for answer in item.engines
        )
    optional = result.queries[2].engines[0].answer
    numeric = result.queries[3].engines[1].answer
    assert (
        optional is not None
        and len(optional.rows) == 2
        and all(row[1] is None for row in optional.rows)
    )
    assert numeric is not None and [
        [str(cell) for cell in row] for row in numeric.rows
    ] == [["2", "2"]]


def test_deliberate_default_configuration_and_duplicate_changes_are_exposed() -> None:
    query = PREFIX + "SELECT ?n WHERE {?s ex:number ?n}"
    result = qualify_workload(
        _source(),
        (WorkloadQuery("duplicates", query),),
        scope=SCOPE,
        right=EngineConfig("pyoxigraph", "union"),
    )
    assert result.report.complete and not result.report.verified
    assert result.queries[0].status == "disagreeing"
    before, after = (item.answer for item in result.queries[0].engines)
    assert before is not None and after is not None
    assert len(before.rows) == 2 and len(after.rows) == 3
    explicit_set = qualify_workload(
        _source(),
        (WorkloadQuery("values", query, "set"),),
        scope=SCOPE,
        right=EngineConfig("pyoxigraph", "union"),
    )
    assert explicit_set.report.verified
    named = qualify_workload(
        _source(),
        (WorkloadQuery("default", PREFIX + "SELECT ?x WHERE {?s ex:marker ?x}"),),
        scope=SCOPE,
        right=EngineConfig("pyoxigraph", "union"),
    )
    assert named.queries[0].status == "disagreeing"


def test_nondeterminism_extensions_retrieval_and_failures_stay_unqualified() -> None:
    queries = (
        WorkloadQuery("random", "SELECT (RAND() AS ?x) WHERE {}"),
        WorkloadQuery("uuid", "SELECT (UUID() AS ?x) WHERE {}"),
        WorkloadQuery(
            "slice", PREFIX + "SELECT ?s WHERE {?s ?p ?o} ORDER BY ?s LIMIT 1"
        ),
        WorkloadQuery(
            "extension", "SELECT (<urn:unavailable:function>(1) AS ?x) WHERE {}"
        ),
        WorkloadQuery(
            "retrieval", "ASK {SERVICE <https://unavailable.example/sparql> {?s ?p ?o}}"
        ),
        WorkloadQuery("syntax", "SELECT WHERE {"),
    )
    result = qualify_workload(_source(), queries, scope=SCOPE)
    assert not result.report.verified and not result.report.complete
    assert [item.status for item in result.queries] == ["unsupported"] * 4 + [
        "failed"
    ] * 2
    assert all(
        engine.answer is None for item in result.queries for engine in item.engines
    )


def test_native_parameter_policies_are_explicit_and_real_differences_preserved() -> (
    None
):
    binding = (QueryBinding("x", Literal("1", datatype=XSD.integer, normalize=False)),)
    queries = (
        WorkloadQuery("bound", PREFIX + "SELECT ?s ?x WHERE {?s ex:number ?x}"),
        WorkloadQuery("assignment", "SELECT ?x WHERE {BIND(2 AS ?x)}"),
        WorkloadQuery("unselected", PREFIX + "SELECT ?s WHERE {?s ex:number ?x}"),
    )
    result = qualify_workload(_source(), queries, scope=SCOPE, bindings=binding)
    assert [item.status for item in result.queries] == [
        "agreeing",
        "disagreeing",
        "unsupported",
    ]
    assert [engine.binding_policy for engine in result.queries[0].engines] == [
        "rdflib-initBindings",
        "SEP-0007",
    ]


def test_actual_storage_fidelity_blank_scope_empty_graph_and_physical_projection() -> (
    None
):
    graph = Graph()
    blank = BNode()
    graph.add((EX.A, EX.part, blank))
    graph.add((blank, EX.label, Literal("shared")))
    result = qualify_workload(
        prepare_rdf(graph),
        (
            WorkloadQuery(
                "blank",
                PREFIX + "SELECT ?part ?name WHERE {ex:A ex:part ?part . "
                "?part ex:label ?name}",
            ),
        ),
        scope=QueryScope("default"),
    )
    assert result.report.verified, result.report.to_dict()
    graph.add((EX.A, EX.number, Literal("01", datatype=XSD.integer, normalize=False)))
    changed = qualify_workload(
        prepare_rdf(graph),
        (WorkloadQuery("lexical", PREFIX + "SELECT ?n WHERE {ex:A ex:number ?n}"),),
        scope=QueryScope("default"),
    )
    assert changed.queries[0].status == "unsupported"
    assert changed.queries[0].engines[0].answer is not None
    assert changed.queries[0].engines[1].answer is None
    empty = qualify_workload(
        _source(),
        (WorkloadQuery("empty", PREFIX + "ASK {GRAPH ex:empty {}}"),),
        scope=SCOPE,
    )
    assert empty.report.verified
    projected = qualify_workload(
        _source(),
        (WorkloadQuery("isolation", PREFIX + "ASK {GRAPH ex:named {?s ?p ?o}}"),),
        scope=QueryScope("default"),
    )
    assert projected.report.verified
    assert all(
        engine.answer is not None and engine.answer.answer is False
        for engine in projected.queries[0].engines
    )


def test_shared_budgets_cancellation_and_truncation_never_publish_certificates() -> (
    None
):
    query = (WorkloadQuery("rows", PREFIX + "SELECT ?s WHERE {?s ex:number ?n}"),)
    truncated = qualify_workload(_source(), query, scope=SCOPE, max_rows=1)
    assert truncated.queries[0].status == "exhausted" and not truncated.report.verified
    budget = Budget(Limits(max_work=1000))
    stopped = qualify_workload(_source(), query, scope=SCOPE, budget=budget)
    assert not stopped.report.verified and any(
        issue.kind == "exhausted" for issue in stopped.report.issues
    )
    small = Graph(bind_namespaces="none")
    small.add((EX.A, EX.label, Literal("one")))
    retained = qualify_workload(
        prepare_rdf(small),
        (WorkloadQuery("known", "SELECT ?s WHERE {?s ?p ?o}"),),
        scope=QueryScope("default"),
        left=EngineConfig("rdflib", entailment="rdfs"),
        budget=Budget(Limits(max_work=40_000)),
    )
    assert any(issue.code == "workload.unsupported" for issue in retained.report.issues)
    assert any(issue.kind == "exhausted" for issue in retained.report.issues)
    one_issue = qualify_workload(
        prepare_rdf(small),
        (WorkloadQuery("random", "SELECT (RAND() AS ?x) WHERE {}"),),
        scope=QueryScope("default"),
        budget=Budget(Limits(max_issues=1)),
    )
    assert any(
        issue.code == "workload.unsupported" for issue in one_issue.report.issues
    )
    assert any(issue.kind == "exhausted" for issue in one_issue.report.issues)
    cancelled = threading.Event()
    cancelled.set()
    cancelled_result = qualify_workload(_source(), query, scope=SCOPE, cancel=cancelled)
    assert not cancelled_result.report.verified and any(
        issue.kind == "exhausted" for issue in cancelled_result.report.issues
    )


def test_empty_corpora_and_invalid_public_shapes_cannot_qualify() -> None:
    empty = qualify_workload(_source(), (), scope=SCOPE)
    assert not empty.report.verified and not empty.report.complete
    with pytest.raises(InputError):
        qualify_workload(cast(PreparedSource, Graph()), (), scope=SCOPE)
    with pytest.raises(InputError):
        qualify_workload(_source(), cast(tuple[WorkloadQuery, ...], []), scope=SCOPE)
    with pytest.raises(InputError):
        QueryBinding("blank", cast(URIRef, BNode()))


def test_native_filter_loss_keeps_the_capable_engine_observation() -> None:
    """Unsupported execution cannot be published as a disagreeing completed answer."""
    result = qualify_workload(
        _source(),
        (
            WorkloadQuery("lost-filter", "ASK {FILTER(false)}"),
            WorkloadQuery("expression", "ASK {FILTER(1=2)}"),
        ),
        scope=SCOPE,
    )
    assert [item.status for item in result.queries] == ["unsupported", "agreeing"]
    assert not result.report.verified
    limited, capable = result.queries[0].engines
    assert limited.config.engine == "rdflib" and limited.status == "unsupported"
    assert limited.answer is None and limited.report.outcome == "unverified"
    assert capable.config.engine == "pyoxigraph" and capable.status == "completed"
    assert capable.report.verified and capable.answer is not None
    assert capable.answer.answer is False and capable.answer.external_complete is True
    assert all(
        engine.answer is not None and engine.answer.answer is False
        for engine in result.queries[1].engines
    )


def test_frozen_native_input_keeps_selected_duplicates_and_unbound_rows() -> None:
    source = _source()
    operation = Operation(None, None, 30, None)
    selected = capture_selected_input(source, QueryScope("default"), (), operation)
    with pytest.raises(FrozenInstanceError):
        selected.scope = QueryScope("union")  # type: ignore[misc]
    with pytest.raises(InputError, match="capture_selected_input"):
        NativeQueryInput()
    # A later public declaration is a different selection, not an instruction to
    # reinterpret the already captured native operation's physical input.
    source.graph_name = str(EX.named)
    source.union = True
    query = (
        PREFIX
        + "SELECT ?missing WHERE {?s ex:number ?n OPTIONAL {?s ex:absent ?missing}}"
    )
    for config in (EngineConfig("rdflib"), EngineConfig("pyoxigraph")):
        observed = observe_query(
            "captured-default", query, config, selected, 10, operation
        )
        assert observed.status == "completed", observed.report.to_dict()
        assert observed.version and observed.python_version
        assert observed.answer is not None and observed.answer.external_complete is True
        assert observed.answer.variables == ("missing",)
        assert observed.answer.rows == ((None,), (None,))
