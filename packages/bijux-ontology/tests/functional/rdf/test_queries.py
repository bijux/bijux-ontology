"""Scoped maintained SPARQL preserves RDF terms, retrieval policy and bounds."""

import json
import subprocess
import sys
import threading
import time
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import Any

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.diagnostics.errors import InputError, ResourceError
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.queries import QueryScope, query_rdf
from bijux_ontology.rdf.queries.protocol import term_data
from bijux_ontology.rdf.queries.results import adopt_result
from bijux_ontology.rdf.stores.preparation import snapshot_rdf
from bijux_ontology.rdf.stores.queries import query_snapshot

EX = Namespace("https://queries.example/")


def _dataset() -> Dataset:
    dataset = Dataset()
    dataset.default_graph.add(
        (EX.default, EX.value, Literal("01", datatype=XSD.integer, normalize=False))
    )
    dataset.graph(EX.local).add((EX.Cell, RDF.type, OWL.Class))
    dataset.graph(EX.imports).add((EX.Cell, RDFS.label, Literal("Cellule", lang="FR")))
    dataset.graph(EX.empty)
    return dataset


def test_explicit_local_import_dataset_join_default_named_union_and_empty_graphs() -> (
    None
):
    source = prepare_source(_dataset())
    query = (
        f"SELECT ?term ?label WHERE {{ GRAPH <{EX.local}> "
        f"{{?term a <{OWL.Class}>}} GRAPH <{EX.imports}> "
        f"{{?term <{RDFS.label}> ?label}} }}"
    )
    joined = query_rdf(source, query, scope=QueryScope("dataset"))
    assert joined.report.verified and joined.variables == ("term", "label")
    assert joined.rows == ((EX.Cell, Literal("Cellule", lang="FR")),)
    assert joined.rows[0][1].language == "FR"
    local = f"ASK {{ <{EX.Cell}> a <{OWL.Class}> }}"
    assert query_rdf(source, local, scope=QueryScope("default")).answer is False
    assert query_rdf(source, local, scope=QueryScope("dataset")).answer is False
    assert query_rdf(source, local, scope=QueryScope("union")).answer is True
    assert query_rdf(source, local, scope=QueryScope("named", EX.local)).answer is True
    assert (
        query_rdf(source, local, scope=QueryScope("named", EX.imports)).answer is False
    )
    assert (
        query_rdf(
            source, f"ASK {{ GRAPH <{EX.empty}> {{}} }}", scope=QueryScope("dataset")
        ).answer
        is True
    )
    inaccessible = query_rdf(
        source,
        f"ASK {{ GRAPH <{EX.local}> {{?s ?p ?o}} }}",
        scope=QueryScope("named", EX.imports),
    )
    assert inaccessible.report.verified and inaccessible.answer is False


def test_pinned_scope_and_owned_blank_graph_names() -> None:
    from bijux_ontology.rdf.sources.types import RdfSource

    pinned = prepare_source(RdfSource(_dataset(), graph_name=str(EX.imports)))
    assert query_rdf(
        pinned, "ASK {?s ?p ?o}", scope=QueryScope("named", EX.imports)
    ).report.verified
    for scope in (
        QueryScope("dataset"),
        QueryScope("default"),
        QueryScope("union"),
        QueryScope("named", EX.local),
    ):
        result = query_rdf(pinned, "ASK {}", scope=scope)
        assert result.report.outcome == "input_error" and result.answer is None
    dataset = Dataset()
    dataset.graph(BNode("authored")).add((EX.Cell, EX.value, Literal(1)))
    prepared = prepare_source(dataset)
    owned = next(
        graph.identifier
        for graph in prepared.dataset().graphs()
        if isinstance(graph.identifier, BNode)
    )
    assert isinstance(owned, BNode)
    assert (
        query_rdf(prepared, "ASK {?s ?p ?o}", scope=QueryScope("named", owned)).answer
        is True
    )
    rejected = query_rdf(
        prepared, "ASK {}", scope=QueryScope("named", BNode(str(owned)))
    )
    assert rejected.report.outcome == "input_error"


def test_exact_noncanonical_literals_result_local_blanks_and_unbound_solutions() -> (
    None
):
    blank = BNode("caller")
    supplied = (
        Literal("01", datatype=XSD.integer, normalize=False),
        Literal("01.00", datatype=XSD.decimal, normalize=False),
        Literal("Bonjour\n世界", lang="FR"),
        Literal("a custom lexical", datatype=EX.Custom, normalize=False),
    )
    graph = Graph()
    for value in supplied:
        graph.add((blank, EX.value, value))
    before = frozenset(graph)
    result = query_rdf(
        graph, f"SELECT ?s ?v WHERE {{?s <{EX.value}> ?v}}", scope=QueryScope("default")
    )
    assert result.report.verified and len(result.rows) == 4
    assert {
        (type(value), str(value), value.datatype, value.language)
        for _, value in result.rows
        if isinstance(value, Literal)
    } == {
        (type(value), str(value), value.datatype, value.language) for value in supplied
    }
    blanks = [row[0] for row in result.rows]
    assert all(isinstance(node, BNode) for node in blanks)
    assert all(node is blanks[0] for node in blanks)
    assert blanks[0] is not blank and str(blanks[0]) != str(blank)
    assert frozenset(graph) == before
    exact = query_rdf(
        graph,
        f"SELECT ?v WHERE {{?s <{EX.value}> ?v "
        f'FILTER(sameTerm(?v, "01"^^<{XSD.integer}>))}}',
        scope=QueryScope("default"),
    )
    assert exact.report.verified and str(exact.rows[0][0]) == "01"
    unbound = query_rdf(
        Graph(), "SELECT (?absent AS ?value) WHERE {}", scope=QueryScope("default")
    )
    assert unbound.report.verified and unbound.rows == ((None,),)


def test_false_ask_and_completed_empty_select_are_distinct_from_truncated_select() -> (
    None
):
    graph = Graph()
    for index in range(4):
        graph.add((EX[str(index)], EX.value, Literal(index)))
    false = query_rdf(
        graph, f"ASK {{?s <{EX.absent}> ?v}}", scope=QueryScope("default")
    )
    assert false.report.verified and false.answer is False and false.kind == "ASK"
    empty = query_rdf(
        graph, f"SELECT ?s WHERE {{?s <{EX.absent}> ?v}}", scope=QueryScope("default")
    )
    assert empty.report.verified and empty.rows == ()
    query = f"SELECT ?s WHERE {{?s <{EX.value}> ?v}} ORDER BY ?s"
    truncated = query_rdf(graph, query, scope=QueryScope("default"), max_rows=3)
    assert truncated.kind == "SELECT" and len(truncated.rows) == 3
    assert not truncated.report.complete and truncated.report.outcome == "exhausted"
    exact = query_rdf(graph, query, scope=QueryScope("default"), max_rows=4)
    assert exact.report.verified and len(exact.rows) == 4


@pytest.mark.parametrize(
    "query",
    (
        "ASK { SERVICE <https://offline.invalid/> {?s ?p ?o}}",
        "SELECT ?s WHERE {{SELECT ?s WHERE {"
        "SERVICE SILENT <https://offline.invalid/> {?s ?p ?o}}}}",
        "ASK { FILTER EXISTS {SERVICE <https://offline.invalid/> {?s ?p ?o}}}",
        "SELECT ?s FROM <https://offline.invalid/data> WHERE {?s ?p ?o}",
        "ASK FROM NAMED <file:///not-a-local-input> {GRAPH ?g {?s ?p ?o}}",
        "SELECT ?s WHERE {unresolved:term ?p ?s}",
        "SELECT * WHERE {",
        "CONSTRUCT {?s ?p ?o} WHERE {?s ?p ?o}",
        "INSERT DATA {<urn:s> <urn:p> <urn:o>}",
        "SELECT ?x WHERE {?s <urn:a> ?x. BIND(<urn:replacement> AS ?x)}",
        "SELECT ?x WHERE {BIND(<urn:a> AS ?x) BIND(<urn:b> AS ?x)}",
        "SELECT ?s (COUNT(?o) AS ?n) WHERE {?s ?p ?o}",
        "SELECT * WHERE {VALUES (?a ?b) {(<urn:a> <urn:b> <urn:c>)}}",
        "SELECT * WHERE {VALUES (?a ?b) {(<urn:a>)}}",
        "SELECT * WHERE {_:b <urn:p> ?x OPTIONAL {_:b <urn:q> ?y}}",
        "ASK {?s <urn:a> ?x. BIND(<urn:replacement> AS ?x)}",
    ),
)
def test_actual_parser_rejects_retrieval_everywhere_and_invalid_query_forms(
    query: str,
) -> None:
    result = query_rdf(Graph(), query, scope=QueryScope("dataset"))
    assert result.report.outcome == "input_error" and not result.report.complete
    assert result.kind is None and result.answer is None and not result.rows


def test_valid_variable_binding_grouping_values_and_query_blank_scope() -> None:
    graph = Graph().add(
        (EX.one, EX.value, Literal("01", datatype=XSD.integer, normalize=False))
    )
    graph.add((EX.one, RDFS.label, Literal("Bonjour", lang="FR")))
    bound = query_rdf(
        graph,
        f"SELECT ?v ?bound WHERE {{?s <{EX.value}> ?v BIND(?s AS ?bound)}}",
        scope=QueryScope("default"),
    )
    assert bound.report.verified and bound.rows == (
        (Literal("01", datatype=XSD.integer, normalize=False), EX.one),
    )
    grouped = query_rdf(
        graph,
        f"SELECT ?s (COUNT(?v) AS ?n) WHERE {{?s <{EX.value}> ?v}} GROUP BY ?s",
        scope=QueryScope("default"),
    )
    assert grouped.report.verified and grouped.rows == ((EX.one, Literal(1)),)
    values = query_rdf(
        graph,
        f"SELECT ?label WHERE {{VALUES (?s ?p) {{(<{EX.one}> <{RDFS.label}>)}} "
        "?s ?p ?label}",
        scope=QueryScope("default"),
    )
    assert values.report.verified and values.rows == ((Literal("Bonjour", lang="FR"),),)
    scoped = query_rdf(
        graph,
        f"ASK {{_:a <{EX.value}> ?v OPTIONAL {{_:b <{RDFS.label}> ?label}}}}",
        scope=QueryScope("default"),
    )
    assert scoped.report.verified and scoped.answer is True


def test_keywords_in_literals_are_allowed_and_work_and_output_caps_are_truthful() -> (
    None
):
    literal = query_rdf(
        Graph(),
        'SELECT ("SERVICE FROM FROM NAMED" AS ?text) WHERE {}',
        scope=QueryScope("default"),
    )
    assert literal.report.verified and literal.rows == (
        (Literal("SERVICE FROM FROM NAMED"),),
    )
    exhausted = query_rdf(
        Graph(), "ASK {}", scope=QueryScope("default"), limits=Limits(max_work=1)
    )
    assert exhausted.report.outcome == "exhausted" and exhausted.answer is None
    graph = Graph().add((EX.one, EX.value, Literal("x" * 1000)))
    expanded = query_rdf(
        graph,
        f"SELECT (CONCAT({','.join(['?v'] * 20)}) AS ?big) "
        f"WHERE {{?s <{EX.value}> ?v}}",
        scope=QueryScope("default"),
        limits=Limits(max_total_bytes=10000),
    )
    assert not expanded.report.complete and expanded.report.outcome == "exhausted"
    assert expanded.rows == ()


def test_real_query_deadline_cancellation_and_clean_following_execution() -> None:
    graph = Graph()
    for index in range(100):
        graph.add((EX[str(index)], EX.number, Literal(index)))
    expensive = (
        f"SELECT (COUNT(*) AS ?count) WHERE {{?a <{EX.number}> ?x . "
        f"?b <{EX.number}> ?y . ?c <{EX.number}> ?z . "
        "FILTER(?x + ?y + ?z < 0)}"
    )
    timed = query_rdf(graph, expensive, scope=QueryScope("default"), timeout=0.4)
    assert timed.report.outcome == "exhausted" and not timed.report.complete
    cancel = threading.Event()
    timer = threading.Timer(0.4, cancel.set)
    timer.start()
    try:
        cancelled = query_rdf(
            graph, expensive, scope=QueryScope("default"), timeout=5, cancel=cancel
        )
    finally:
        timer.cancel()
    assert cancelled.report.outcome == "exhausted"
    assert "cancelled" in cancelled.report.issues[0].message
    assert (
        query_rdf(graph, "ASK {?s ?p ?o}", scope=QueryScope("default")).answer is True
    )
    assert len(graph) == 100


@pytest.mark.parametrize(
    "program,outcome",
    (
        ("import os; os._exit(23)", "input_error"),
        (
            "import sys,json; print(json.dumps(dict(status='result',kind='ASK',"
            "answer=True,complete=True))); sys.exit(23)",
            "input_error",
        ),
        ("import sys; sys.stdout.write('[]')", "input_error"),
        ("print('{\"status\":[]}')", "input_error"),
        ('print(\'{"status":"result","kind":[],"complete":true}\')', "input_error"),
        ("import sys; sys.stdout.write('x'*20000); sys.stdout.flush()", "exhausted"),
    ),
)
def test_failed_or_malformed_owned_peer_never_returns_empty_success(
    program: str,
    outcome: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = subprocess.Popen
    first = True

    def launch(args: Sequence[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        nonlocal first
        if first:
            first = False
            return original([sys.executable, "-c", program], **kwargs)
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", launch)
    failed = query_rdf(
        Graph(),
        "ASK {}",
        scope=QueryScope("default"),
        limits=Limits(max_total_bytes=5000),
    )
    assert failed.report.outcome == outcome and not failed.report.complete
    assert failed.answer is None and failed.kind is None
    recovered = query_rdf(Graph(), "ASK {}", scope=QueryScope("default"))
    assert recovered.report.verified and recovered.answer is True


def test_named_prepared_selection_bounds_empty_graph_inventory() -> None:
    dataset = Dataset()
    for index in range(100):
        dataset.graph(EX[f"empty{index}"])
    source = prepare_source(dataset)
    result = query_rdf(
        source,
        "ASK {}",
        scope=QueryScope("named", EX.absent),
        limits=Limits(max_work=10),
    )
    assert result.report.outcome == "exhausted" and result.answer is None


def test_deadline_expiring_at_capture_end_is_exhausted() -> None:
    class DelayedNamespaces(Graph):
        def namespaces(self) -> Generator[tuple[str, URIRef], None, None]:
            yield from super().namespaces()
            time.sleep(0.02)

    result = query_rdf(
        DelayedNamespaces(), "ASK {}", scope=QueryScope("default"), timeout=0.005
    )
    assert result.report.outcome == "exhausted" and result.answer is None


@pytest.mark.parametrize(
    ("body", "outcome", "code"),
    [
        (
            "SELECT (<urn:extension>(1) AS ?v) WHERE {}",
            "unverified",
            "rdf.query_unverified",
        ),
        (
            'SELECT (x:normalizedString(" a  b ") AS ?v) WHERE {}',
            "unverified",
            "rdf.literal_conversion_unverified",
        ),
        (
            'SELECT (x:normalizedString(" a  b ") AS ?v) '
            "WHERE { BIND(1 AS ?n) BIND(2 AS ?n) }",
            "input_error",
            "input.invalid",
        ),
        ("SELECT (x:string(1) AS ?v) WHERE {}", "agreement", None),
    ],
)
def test_native_function_capability_is_distinct_from_invalid_query_scope(
    body: str, outcome: str, code: str | None
) -> None:
    result = query_rdf(
        Graph(),
        "PREFIX x: <http://www.w3.org/2001/XMLSchema#> " + body,
        scope=QueryScope("default"),
    )
    assert result.report.outcome == outcome
    if code is not None:
        assert result.answer is None and not result.rows
        assert any(issue.code == code for issue in result.report.issues)
    else:
        assert result.rows == ((Literal("1", datatype=XSD.string),),)


def test_query_annotation_and_transform_share_exact_physical_selection() -> None:
    """A hidden named assertion stays hidden through independent public consumers."""
    from bijux_ontology.rdf.transforms import extract_neighbourhood
    from bijux_ontology.vocabulary.annotations.graphs import (
        AnnotationStatement,
        inspect_annotation_graph,
    )

    label = Literal("Hidden", lang="en")
    dataset = Dataset()
    dataset.graph(EX.hidden).add((EX.Hidden, RDFS.label, label))
    dataset.graph(EX.empty)
    source = prepare_source(dataset)
    statement = AnnotationStatement(EX.Hidden, RDFS.label, label)
    text = f"ASK {{ <{EX.Hidden}> <{RDFS.label}> ?label }}"
    for graph in (None, EX.empty, EX.hidden):
        expected = graph == EX.hidden
        scope = QueryScope("default") if graph is None else QueryScope("named", graph)
        queried = query_rdf(source, text, scope=scope)
        assert queried.report.verified and queried.answer is expected
        annotated = inspect_annotation_graph(source, (statement,), graph=graph)
        assert annotated.report.verified is expected
        transformed = extract_neighbourhood(
            source,
            (str(EX.Hidden),),
            predicates=(str(RDFS.label),),
            graph_name="@default" if graph is None else str(graph),
        )
        assert transformed.source is not None
        selected = transformed.source.graph()
        assert ((EX.Hidden, RDFS.label, label) in selected) is expected
    assert query_rdf(source, text, scope=QueryScope("union")).answer is True
    empty = query_rdf(
        source, f"ASK {{ GRAPH <{EX.empty}> {{}} }}", scope=QueryScope("dataset")
    )
    assert empty.report.verified and empty.answer is True


def test_required_filters_are_unknown_only_for_lossy_native_execution(
    tmp_path: Path,
) -> None:
    """RDFLib limitations cannot prohibit valid queries in a capable native store."""
    path = tmp_path / "input.nt"
    path.write_text("<urn:filter:s> <urn:filter:p> <urn:filter:o> .\n")
    stored = snapshot_rdf(path, tmp_path / "snapshot", format="nt")
    assert stored.report.verified and stored.snapshot is not None
    scope = QueryScope("default")
    for query, answer, rows in (
        ("ASK {FILTER(false)}", False, ()),
        ("ASK {FILTER(0)}", False, ()),
        ('ASK {FILTER("")}', False, ()),
        ("ASK {{FILTER(false)} FILTER(1=1)}", False, ()),
        (
            "SELECT ?x WHERE {{BIND(1 AS ?x) FILTER(false)} UNION {BIND(2 AS ?x)}}",
            None,
            ((Literal(2),),),
        ),
    ):
        limited = query_rdf(Graph(), query, scope=scope)
        assert limited.report.outcome == "unverified"
        assert limited.answer is None and limited.kind is None and limited.rows == ()
        native = query_snapshot(stored.snapshot, query, scope=scope)
        assert (
            native.report.verified and native.answer is answer and native.rows == rows
        )
    for body, expected in (
        ("FILTER(true)", True),
        ("FILTER(1=2)", False),
        ("FILTER(false) FILTER(true)", False),
    ):
        completed = query_rdf(Graph(), f"ASK {{{body}}}", scope=scope)
        assert completed.report.verified and completed.answer is expected


@pytest.mark.parametrize("unbound", (False, True))
def test_native_solution_bags_pay_rows_even_without_bound_cells(unbound: bool) -> None:
    """Maintained SELECT bags retain empty rows and stop with a paid prefix."""
    projection = "(?absent AS ?value)" if unbound else "*"
    graph = Graph()
    for index in range(256):
        graph.add((EX[f"row{index}"], EX.value, EX.object))
    query = f"SELECT {projection} WHERE {{[] <{EX.value}> <{EX.object}>}}"
    scope = QueryScope("default")
    complete = query_rdf(graph, query, scope=scope, max_rows=256)
    expected = (None,) if unbound else ()
    assert complete.report.verified and complete.rows == (expected,) * 256
    stopped = query_rdf(
        graph, query, scope=scope, max_rows=256, limits=Limits(max_states=200)
    )
    assert stopped.report.outcome == "exhausted" and not stopped.report.complete
    assert 0 < len(stopped.rows) < 256
    assert stopped.rows == (expected,) * len(stopped.rows)
    assert stopped.variables == complete.variables
    assert query_rdf(Graph(), "SELECT * WHERE {}", scope=scope).rows == ((),)


def test_result_admission_keeps_only_paid_rows_and_exact_shared_terms() -> None:
    """Native answers face the same row fee at independent host admission."""
    scope = QueryScope("default")
    for projection, states, expected in (
        ("*", 1, ((),)),
        ("(?absent AS ?x)", 5, ((None,),) * 2),
    ):
        actual = query_rdf(
            Graph(),
            f"SELECT {projection} WHERE {{ {{}} UNION {{}} UNION {{}} }}",
            scope=scope,
        )
        assert actual.report.verified and len(actual.rows) == 3
        response = {
            "status": "result",
            "kind": "SELECT",
            "variables": list(actual.variables),
            "rows": [[term_data(term) for term in row] for row in actual.rows],
            "complete": True,
            "message": "",
        }
        budget = Budget(Limits(max_states=states))
        prefix = adopt_result(
            json.dumps(response).encode(), scope, budget.limits, 3, budget=budget
        )
        assert prefix.rows == expected and prefix.report.outcome == "exhausted"
        assert not prefix.report.complete and budget._counts["states"] == states
    node = BNode("original")
    graph = Graph()
    for value in ("01", "02", "03"):
        graph.add(
            (node, EX.value, Literal(value, datatype=XSD.integer, normalize=False))
        )
    actual = query_rdf(
        graph, f"SELECT ?s ?v WHERE {{?s <{EX.value}> ?v}} ORDER BY ?v", scope=scope
    )
    assert actual.report.verified and len(actual.rows) == 3
    response = {
        "status": "result",
        "kind": "SELECT",
        "variables": list(actual.variables),
        "rows": [[term_data(term) for term in row] for row in actual.rows],
        "complete": True,
        "message": "",
    }
    budget = Budget(Limits(max_states=8))
    prefix = adopt_result(
        json.dumps(response).encode(), scope, budget.limits, 3, budget=budget
    )
    assert prefix.report.outcome == "exhausted" and len(prefix.rows) == 2
    assert prefix.rows[0][0] is prefix.rows[1][0] and prefix.rows[0][0] is not node
    assert tuple(str(row[1]) for row in prefix.rows) == ("01", "02")
    response["rows"] = [[term_data(actual.rows[0][0])]]
    with pytest.raises(ResourceError, match="row width"):
        adopt_result(json.dumps(response).encode(), scope, Limits(), 3)
    malformed = {
        "kind": "literal",
        "value": "01",
        "datatype": str(XSD.integer),
        "language": None,
        "extra": "x" * 10000,
    }
    response["rows"] = [[malformed, None]]
    with pytest.raises(InputError, match="literal query term fields"):
        adopt_result(json.dumps(response).encode(), scope, Limits(max_bytes=64), 3)
    malformed["kind"] = "unsupported"
    with pytest.raises(InputError, match="Unsupported RDF query term kind"):
        adopt_result(json.dumps(response).encode(), scope, Limits(max_bytes=64), 3)
