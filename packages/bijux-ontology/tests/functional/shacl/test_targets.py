"""Actual maintained target selection, parameter guards and bounded coverage."""

from threading import Event
from typing import Any

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace
from rdflib.namespace import RDF, SH, XSD
from rdflib.query import Result

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.shacl.checking import validate_shapes
from bijux_ontology.shacl.coverage import ShaclCoverageContract
from bijux_ontology.shacl.functions import (
    ShaclFunctionCatalog,
    ShaclFunctionParameter,
    ShaclSparqlFunction,
)
from bijux_ontology.shacl.session import ShaclSession
from bijux_ontology.shacl.targets.execution import materialize_targets
from bijux_ontology.shacl.targets.preparation import prepare_targets

EX = Namespace("https://example.org/")
PREFIX = (
    "@prefix ex:<https://example.org/>. "
    "@prefix sh:<http://www.w3.org/ns/shacl#>. "
    "@prefix xsd:<http://www.w3.org/2001/XMLSchema#>. "
)
QUERY = (
    "SELECT ?this WHERE {?this <https://example.org/kind> <https://example.org/Wanted>}"
)


def shapes(query: str = QUERY) -> Graph:
    """Create one actual target and one required ordinary property constraint."""
    return Graph().parse(
        data=PREFIX
        + """ex:S a sh:NodeShape;sh:target ex:T;
    sh:property [sh:path ex:code;sh:minCount 1].
    ex:T a sh:SPARQLTarget;sh:select """
        + Literal(query).n3()
        + ".",
        format="turtle",
    )


def data(*, valid: bool = True) -> Graph:
    """Keep an unrelated nonconforming record outside the intended target."""
    graph = Graph().add((EX.a, EX.kind, EX.Wanted)).add((EX.b, EX.kind, EX.Other))
    if valid:
        graph.add((EX.a, EX.code, Literal("ok")))
    return graph


def test_direct_targets_have_actual_coverage_and_no_unrelated_af_activation() -> None:
    pytest.importorskip("pyshacl")
    graph = shapes()
    graph.add((EX.Unselected, RDF.type, SH.NodeShape)).add(
        (EX.Unselected, SH.target, EX.Bad)
    )
    graph.add((EX.Bad, RDF.type, SH.JSTarget)).add(
        (EX.Bad, SH.jsFunctionName, Literal("never"))
    )
    before = frozenset(graph)
    coverage = ShaclCoverageContract(pairs=((str(EX.S), str(EX.a)),), minimum_focus=1)
    for valid in (True, False):
        result = validate_shapes(
            data(valid=valid),
            graph,
            trusted_targets=True,
            use_shapes=(str(EX.S),),
            coverage=coverage,
        )
        assert result.conforms is valid and result.report.verified is valid
        assert result.coverage is not None and result.coverage.report.verified
        assert [
            (target.shape, target.focus_node) for target in result.coverage.targets
        ] == [(EX.S, EX.a)]
    assert frozenset(graph) == before
    assert not validate_shapes(data(), shapes(), coverage=coverage).report.verified
    assert not validate_shapes(data(), shapes(), trusted_sparql=True).report.verified
    paired = validate_shapes(
        data(),
        graph,
        trusted_targets=True,
        use_shapes=(str(EX.Unselected),),
        focus_nodes=(str(EX.a),),
        mode="pairs",
    )
    assert paired.report.verified
    # Target trust does not grant constraint query trust, even on the same node.
    dual = shapes()
    dual.add((EX.S, SH.sparql, EX.T))
    assert not validate_shapes(data(), dual, trusted_targets=True).report.verified


def test_parameterized_target_uses_maintained_required_optional_core_guards() -> None:
    pytest.importorskip("pyshacl")
    graph = Graph().parse(
        data=PREFIX
        + '''
      ex:Country a ex:Kind. ex:Wanted a ex:Country.
      ex:Type a sh:SPARQLTargetType;
        sh:parameter [sh:path ex:kind;sh:class ex:Country;sh:nodeKind sh:IRI];
        sh:parameter [sh:path ex:minimum;sh:datatype xsd:integer;sh:optional true];
        sh:select """SELECT ?this WHERE {
          ?this <https://example.org/kind> $kind.
          FILTER(!BOUND($minimum) || $minimum < 3)}""".
      ex:T a ex:Type;ex:kind ex:Wanted;ex:minimum 2.
      ex:S a sh:NodeShape;sh:target ex:T;sh:property [sh:path ex:code;sh:minCount 1].
      ex:Ambient a sh:SPARQLTargetType;sh:select "invalid ambient query".
    ''',
        format="turtle",
    )
    coverage = ShaclCoverageContract(minimum_focus=1)
    assert validate_shapes(
        data(), graph, trusted_targets=True, use_shapes=(str(EX.S),), coverage=coverage
    ).report.verified
    graph.remove((EX.T, EX.minimum, None))
    assert validate_shapes(
        data(), graph, trusted_targets=True, use_shapes=(str(EX.S),), coverage=coverage
    ).report.verified
    graph.add((EX.T, EX.minimum, Literal("wrong")))
    assert not validate_shapes(
        data(), graph, trusted_targets=True, use_shapes=(str(EX.S),)
    ).report.verified
    graph.remove((EX.T, EX.minimum, None))
    graph.set((EX.T, EX.kind, EX.Other))
    assert not validate_shapes(
        data(), graph, trusted_targets=True, use_shapes=(str(EX.S),)
    ).report.verified
    graph.set((EX.T, EX.kind, EX.Wanted)).add((EX.T, EX.kind, EX.Other))
    assert not validate_shapes(
        data(), graph, trusted_targets=True, use_shapes=(str(EX.S),)
    ).report.verified
    shadowed = graph + Graph().add(
        (
            EX.T,
            SH.select,
            Literal(
                "SELECT ?this WHERE {SERVICE <https://example.org/remote> "
                "{?this ?p ?o}}"
            ),
        )
    )
    assert not validate_shapes(
        data(), shadowed, trusted_targets=True, use_shapes=(str(EX.S),)
    ).report.verified
    graph.remove((EX.T, EX.kind, None))
    missing = validate_shapes(
        data(), graph, trusted_targets=True, use_shapes=(str(EX.S),)
    )
    assert not missing.report.verified and missing.conforms is None
    graph.add((EX.T, EX.kind, BNode()))
    assert not validate_shapes(
        data(), graph, trusted_targets=True, use_shapes=(str(EX.S),)
    ).report.verified


def test_target_functions_execute_once_and_session_jobs_stay_independent() -> None:
    pytest.importorskip("pyshacl")
    query = "SELECT ?this WHERE {?this <https://example.org/kind> ?kind. FILTER(<https://example.org/wanted>(?kind))}"
    graph = shapes(query)
    catalog = ShaclFunctionCatalog.from_functions(
        (
            ShaclSparqlFunction(
                str(EX.wanted),
                (ShaclFunctionParameter(str(EX.value)),),
                ask="ASK {FILTER($value = <https://example.org/Wanted>)}",
            ),
        ),
        trusted=True,
    )
    assert not validate_shapes(data(), graph, trusted_targets=True).report.verified
    with ShaclSession(
        graph,
        trusted_targets=True,
        functions=catalog,
        coverage=ShaclCoverageContract(minimum_focus=1),
    ) as session:
        for valid in (True, False, True):
            result = session.validate(data(valid=valid))
            assert result.conforms is valid and result.report.verified is valid
            assert dict(result.function_calls) == {str(EX.wanted): 2}

        changed = (
            Graph().add((EX.c, EX.kind, EX.Wanted)).add((EX.c, EX.code, Literal("ok")))
        )
        changed.add((EX.b, EX.kind, EX.Other))
        fresh = session.validate(changed)
        assert fresh.report.verified and fresh.coverage is not None
        assert {target.focus_node for target in fresh.coverage.targets} == {EX.c}
        restored = session.validate(data())
        assert restored.report.verified and restored.coverage is not None
        assert {target.focus_node for target in restored.coverage.targets} == {EX.a}

    # A maintained target query itself runs once, not again for coverage.
    class CountingGraph(Graph):
        calls = 0

        def query(self, *args: Any, **kwargs: Any) -> Result:
            self.calls += 1
            return super().query(*args, **kwargs)

    counted = CountingGraph()
    for triple in data():
        counted.add(triple)
    ordinary = shapes()
    materialize_targets(counted, ordinary, None, Limits())
    assert counted.calls == 1 and not list(ordinary.objects(EX.S, SH.target))
    assert set(ordinary.objects(EX.S, SH.targetNode)) == {EX.a}
    blank = BNode()
    native = (
        Graph().add((blank, EX.kind, EX.Wanted)).add((blank, EX.code, Literal("ok")))
    )
    resolved = shapes()
    materialize_targets(native, resolved, None, Limits())
    assert next(resolved.objects(EX.S, SH.targetNode)) is blank
    result = validate_shapes(
        native,
        shapes(),
        trusted_targets=True,
        coverage=ShaclCoverageContract(minimum_focus=1),
    )
    assert result.report.verified and result.coverage is not None
    assert len(result.coverage.targets) == 1
    assert isinstance(result.coverage.targets[0].focus_node, BNode)


def test_query_forms_retrieval_unknown_kinds_and_scopes_never_conform() -> None:
    pytest.importorskip("pyshacl")
    for query in (
        "ASK {}",
        "SELECT * WHERE {?this ?p ?o}",
        "SELECT ?this ?other WHERE {?this ?p ?other}",
        "SELECT ?this FROM <https://example.org/retrieve> WHERE {?this ?p ?o}",
        "SELECT ?this WHERE {SERVICE <https://example.org/retrieve> {?this ?p ?o}}",
    ):
        result = validate_shapes(data(), shapes(query), trusted_targets=True)
        assert not result.report.verified and result.conforms is None
    for query in (Literal(QUERY, lang="en"), Literal(QUERY, datatype=XSD.anyURI)):
        graph = shapes()
        graph.set((EX.T, SH.select, query))
        assert not validate_shapes(data(), graph, trusted_targets=True).report.verified
        graph = shapes().add((EX.T, SH.ask, query))
        assert not validate_shapes(data(), graph, trusted_targets=True).report.verified
    for kind in (SH.JSTarget, EX.Unknown):
        graph = shapes()
        graph.remove((EX.T, SH.select, None)).set((EX.T, RDF.type, kind))
        assert not validate_shapes(data(), graph, trusted_targets=True).report.verified
    source = Dataset()
    source.graph(EX.g).add((EX.a, EX.kind, EX.Wanted)).add(
        (EX.a, EX.code, Literal("ok"))
    )
    source.graph(EX.h).add((EX.bad, EX.kind, EX.Wanted))
    prepared = prepare_rdf(source)
    assert validate_shapes(
        prepared, shapes(), graph_name=EX.g, trusted_targets=True
    ).report.verified
    assert not validate_shapes(
        prepared, shapes(), graph_name=EX.h, trusted_targets=True
    ).report.verified
    assert not validate_shapes(
        prepared, shapes(), union=True, trusted_targets=True
    ).report.verified
    with pytest.raises(InputError):
        validate_shapes(data(), shapes(), trusted_targets=1)  # type: ignore[arg-type]


def test_target_limits_deadline_and_cancel_cannot_publish_partial_conformance() -> None:
    pytest.importorskip("pyshacl")
    many = Graph()
    for index in range(100):
        many.add((EX[str(index)], EX.kind, EX.Wanted)).add(
            (EX[str(index)], EX.code, Literal("ok"))
        )
    result = validate_shapes(
        many,
        shapes(),
        trusted_targets=True,
        limits=Limits(max_states=50),
        coverage=ShaclCoverageContract(minimum_focus=100),
    )
    assert not result.report.verified and result.conforms is None
    limited = prepare_targets(shapes(), (), enabled=True, limits=Limits(max_work=2))
    assert any(issue.kind == "exhausted" for issue in limited.issues)
    expired = validate_shapes(data(), shapes(), trusted_targets=True, timeout=0.001)
    assert not expired.report.verified and expired.conforms is None
    with ShaclSession(shapes(), trusted_targets=True) as session:
        event = Event()
        event.set()
        cancelled = session.validate(data(), cancel=event)
        assert not cancelled.report.verified and cancelled.conforms is None
        assert session.validate(data()).report.verified
    # Empty maintained results remain complete, but cannot satisfy coverage.
    empty = validate_shapes(
        data(),
        shapes("SELECT ?this WHERE {}"),
        trusted_targets=True,
        coverage=ShaclCoverageContract(minimum_focus=1),
    )
    assert not empty.report.verified and empty.conforms is True
    assert empty.coverage is not None and empty.coverage.targets == ()
