"""Real isolated reusable functions, capability boundaries and RDF error semantics."""

from concurrent.futures import ThreadPoolExecutor

import pyshacl
import pytest
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, SH, XSD

from bijux_ontology.execution.limits import Limits
from bijux_ontology.shacl.checking import validate_shapes
from bijux_ontology.shacl.sparql.functions import (
    ShaclFunctionCatalog,
    ShaclFunctionParameter,
    ShaclSparqlFunction,
)
from bijux_ontology.shacl.validation.contracts import ShaclConstraintRequirement

EX = Namespace("https://example.org/")


def shapes(query: str) -> Graph:
    graph = Graph().add((EX.Shape, RDF.type, SH.NodeShape))
    graph.add((EX.Shape, SH.targetNode, EX.item)).add((EX.Shape, SH.sparql, EX.Query))
    graph.add((EX.Query, SH.select, Literal("SELECT $this WHERE { " + query + " }")))
    return graph


def function(body: str, *, iri: str = str(EX.double)) -> ShaclSparqlFunction:
    return ShaclSparqlFunction(iri, (ShaclFunctionParameter(str(EX.x)),), select=body)


def catalog(*definitions: ShaclSparqlFunction) -> ShaclFunctionCatalog:
    return ShaclFunctionCatalog.from_functions(definitions, trusted=True)


@pytest.mark.parametrize("prefix", ("iri", "query", "query_default", "rdf", "binding"))
def test_double_matches_maintained_backend_and_retains_required_constraint_execution(
    prefix: str,
) -> None:
    defined = function("SELECT ($x * 2 AS ?result) WHERE {}")
    supplied = catalog(defined)
    call = (
        "<https://example.org/double>"
        if prefix == "iri"
        else ":double"
        if prefix == "query_default"
        else "ex:double"
    )
    graph = shapes(f"$this <{EX.value}> ?v . FILTER({call}(?v) != 4)")
    if prefix in ("query", "query_default"):
        graph.set(
            (
                EX.Query,
                SH.select,
                Literal(
                    (
                        "PREFIX :<https://example.org/> "
                        if prefix == "query_default"
                        else "PREFIX ex:<https://example.org/> "
                    )
                    + str(graph.value(EX.Query, SH.select))
                ),
            )
        )
    elif prefix == "rdf":
        graph.add((EX.Query, SH.prefixes, EX.Prefixes))
        graph.add((EX.Prefixes, SH.declare, EX.Declaration))
        graph.add((EX.Declaration, SH.prefix, Literal("ex")))
        graph.add((EX.Declaration, SH.namespace, Literal(str(EX), datatype=XSD.anyURI)))
    elif prefix == "binding":
        graph.bind("ex", EX)
    before = frozenset(graph), frozenset(supplied.graph())
    required = (ShaclConstraintRequirement(str(EX.Shape), str(SH.sparql)),)
    for value in (2, 3):
        data = Graph().add((EX.item, EX.value, Literal(value)))
        result = validate_shapes(
            data,
            graph,
            functions=supplied,
            trusted_sparql=True,
            required_constraints=required,
        )
        assert result.conforms is (value == 2) and result.report.verified is (
            value == 2
        )
        assert result.constraints is not None and result.constraints.verified
        assert result.registered_functions == (str(EX.double),)
        assert dict(result.function_calls) == {str(EX.double): 1}
        if prefix == "iri":
            maintained, _, _ = pyshacl.validate(
                data,
                shacl_graph=graph + defined.graph(),
                advanced=True,
                js=False,
                inference="none",
            )
            assert maintained == result.conforms
    exported = supplied.graph()
    exported.remove((None, None, None))
    assert before == (frozenset(graph), frozenset(supplied.graph()))


def test_transitive_catalog_orders_arguments_and_ignores_unselected_functions() -> None:
    subtract = ShaclSparqlFunction(
        str(EX.subtract),
        (
            ShaclFunctionParameter(str(EX.z), order=0),
            ShaclFunctionParameter(str(EX.a), order=1),
        ),
        select="SELECT ($z - $a AS ?result) WHERE {}",
    )
    outer = ShaclSparqlFunction(
        str(EX.outer),
        (ShaclFunctionParameter(str(EX.a), order=1), ShaclFunctionParameter(str(EX.z))),
        select=(
            "PREFIX :<https://example.org/> "
            "SELECT (:subtract($z, $a) * 2 AS ?result) WHERE {}"
        ),
    )
    unused = function(
        "SELECT (<https://example.org/missing>($x) AS ?result) WHERE {}",
        iri=str(EX.unused),
    )
    source = subtract.graph() + outer.graph() + unused.graph()
    source.add((EX.js, RDF.type, SH.JSFunction)).add(
        (EX.js, SH.jsFunctionName, Literal("unselected"))
    )
    selected = ShaclFunctionCatalog(source, selected=(str(EX.outer),), trusted=True)
    graph = shapes(f"$this <{EX.value}> ?v . FILTER(<{EX.outer}>(?v, 1) != 4)")
    for value in (3, 4):
        result = validate_shapes(
            Graph().add((EX.item, EX.value, Literal(value))),
            graph,
            trusted_sparql=True,
            functions=selected,
        )
        assert result.conforms is (value == 3)
        assert set(result.registered_functions) == {str(EX.outer), str(EX.subtract)}
        assert dict(result.function_calls) == {str(EX.outer): 1, str(EX.subtract): 1}


def test_optional_omission_zero_arguments_and_unbound_are_sparql_values_or_errors() -> (
    None
):
    optional = ShaclSparqlFunction(
        str(EX.optional),
        (
            ShaclFunctionParameter(str(EX.x)),
            ShaclFunctionParameter(str(EX.y), optional=True),
        ),
        select="SELECT (COALESCE($y, $x) AS ?result) WHERE {}",
    )
    supplied = catalog(optional)
    good = shapes(
        f"BIND(99 AS ?y) BIND(<{EX.optional}>(7) AS ?actual) FILTER(?actual != 7)"
    )
    assert validate_shapes(
        Graph(), good, trusted_sparql=True, functions=supplied
    ).report.verified
    omitted = shapes(f"BIND(<{EX.optional}>() AS ?actual) FILTER(!BOUND(?actual))")
    missing = validate_shapes(Graph(), omitted, trusted_sparql=True, functions=supplied)
    assert missing.conforms is False
    zero = ShaclSparqlFunction(str(EX.zero), select='SELECT ("ok" AS ?result) WHERE {}')
    assert validate_shapes(
        Graph(),
        shapes(f'FILTER(<{EX.zero}>() != "ok")'),
        trusted_sparql=True,
        functions=catalog(zero),
    ).report.verified
    for body in (
        "SELECT ?result WHERE { FILTER(1 = 0) }",
        "SELECT (?missing AS ?result) WHERE {}",
    ):
        unbound = catalog(function(body))
        checked = validate_shapes(
            Graph(),
            shapes(f"BIND(<{EX.double}>(1) AS ?actual) FILTER(!BOUND(?actual))"),
            trusted_sparql=True,
            functions=unbound,
        )
        assert checked.conforms is False
        assert dict(checked.function_calls) == {str(EX.double): 1}
        filtered = validate_shapes(
            Graph(),
            shapes(f"BIND(<{EX.double}>(1) AS ?actual) FILTER(BOUND(?actual))"),
            trusted_sparql=True,
            functions=unbound,
        )
        assert filtered.conforms is True and filtered.report.verified
        assert dict(filtered.function_calls) == {str(EX.double): 1}


def test_rdf_terms_and_false_ask_results_are_not_coerced_by_return_metadata() -> None:
    identity = ShaclSparqlFunction(
        str(EX.identity),
        (ShaclFunctionParameter(str(EX.x)),),
        select="SELECT $x WHERE {}",
        return_type=str(XSD.integer),
    )
    supplied = catalog(identity)
    graph = shapes(
        f"$this <{EX.value}> ?value . BIND(<{EX.identity}>(?value) AS ?copy) "
        "FILTER(!sameTerm(?copy, ?value))"
    )
    for value in (
        URIRef(EX.resource),
        Literal("bonjour", lang="fr"),
        Literal("001", datatype=XSD.integer, normalize=False),
    ):
        result = validate_shapes(
            Graph().add((EX.item, EX.value, value)),
            graph,
            trusted_sparql=True,
            functions=supplied,
        )
        assert result.report.verified
    ask = ShaclSparqlFunction(
        str(EX.ask), (ShaclFunctionParameter(str(EX.x)),), ask="ASK { FILTER($x = 2) }"
    )
    for ask_value in (2, 3):
        result = validate_shapes(
            Graph().add((EX.item, EX.value, Literal(ask_value))),
            shapes(f"$this <{EX.value}> ?v . FILTER(!<{EX.ask}>(?v))"),
            trusted_sparql=True,
            functions=catalog(ask),
        )
        assert result.conforms is (ask_value == 2)


@pytest.mark.parametrize(
    "failure",
    (
        "body",
        "language",
        "query_language",
        "query_datatype",
        "recursive",
        "dependency",
        "projection",
        "untrusted",
    ),
)
def test_unavailable_catalogs_cannot_satisfy_required_constraints(failure: str) -> None:
    graph = function("SELECT ($x * 2 AS ?result) WHERE {}").graph()
    expected = "shacl.function_malformed"
    if failure == "body":
        graph.remove((EX.double, SH.select, None))
    elif failure == "language":
        graph.remove((EX.double, RDF.type, None))
        graph.add((EX.double, RDF.type, SH.JSFunction))
        expected = "shacl.function_unavailable"
    elif failure in ("query_language", "query_datatype"):
        body = "SELECT ($x * 2 AS ?result) WHERE {}"
        query = (
            Literal(body, lang="en")
            if failure == "query_language"
            else Literal(body, datatype=XSD.anyURI)
        )
        graph.set((EX.double, SH.select, query))
    elif failure in ("recursive", "dependency"):
        target = EX.double if failure == "recursive" else EX.missing
        graph.set(
            (
                EX.double,
                SH.select,
                Literal(f"SELECT (<{target}>($x) AS ?result) WHERE {{}}"),
            )
        )
        expected = (
            "shacl.function_recursive"
            if failure == "recursive"
            else "shacl.function_unavailable"
        )
    elif failure == "projection":
        graph.set((EX.double, SH.select, Literal("SELECT * WHERE {?s ?p ?o}")))
    else:
        expected = "shacl.function_untrusted"
    selected = ShaclFunctionCatalog(
        graph, selected=(str(EX.double),), trusted=failure != "untrusted"
    )
    result = validate_shapes(
        Graph(),
        shapes(f"FILTER(<{EX.double}>(3) != 4)"),
        trusted_sparql=True,
        functions=selected,
        required_constraints=(
            ShaclConstraintRequirement(str(EX.Shape), str(SH.sparql)),
        ),
    )
    assert result.conforms is None and not result.report.verified
    assert expected in {issue.code for issue in result.report.issues}


def test_core_ask_functions_and_other_extensions_remain_independent() -> None:
    supplied = catalog(function("SELECT $x WHERE {}"))
    core = (
        Graph()
        .add((EX.Core, RDF.type, SH.NodeShape))
        .add((EX.Core, SH.targetNode, EX.item))
    )
    core.add((EX.Core, SH.nodeKind, SH.IRI))
    result = validate_shapes(Graph(), core, functions=supplied)
    assert result.report.verified and dict(result.function_calls) == {str(EX.double): 0}
    for predicate in (SH.target, SH.rule, SH.js, SH.expression):
        extended = Graph()
        extended += core
        extended.add((EX.Core, predicate, EX.Extension))
        result = validate_shapes(
            Graph(), extended, functions=supplied, trusted_sparql=True
        )
        assert result.conforms is None and not result.report.verified
        assert any(
            issue.code == "shacl.feature_unverified" for issue in result.report.issues
        )
    query = shapes("FILTER(false)")
    assert not validate_shapes(Graph(), query, functions=supplied).report.verified
    ordinary = Graph().parse(
        data="""@prefix sh:<http://www.w3.org/ns/shacl#>.
@prefix ex:<https://example.org/>.
ex:Equal a sh:ConstraintComponent; sh:parameter [sh:path ex:mustEqual];
 sh:validator ex:Validator.
ex:Validator a sh:SPARQLAskValidator; sh:ask "ASK {FILTER($value = $mustEqual)}".
ex:Property a sh:PropertyShape; sh:targetNode ex:item; sh:path ex:value; ex:mustEqual 2.
""",
        format="turtle",
    )
    assert validate_shapes(
        Graph().add((EX.item, EX.value, Literal(2))), ordinary, trusted_sparql=True
    ).report.verified


def test_catalogs_reusing_an_iri_are_owned_by_each_independent_job() -> None:
    graph = shapes(f"$this <{EX.value}> ?v . FILTER(<{EX.double}>(?v) != 4)")
    data = Graph().add((EX.item, EX.value, Literal(2)))

    def run(multiplier: int) -> bool | None:
        return validate_shapes(
            data,
            graph,
            trusted_sparql=True,
            functions=catalog(
                function(f"SELECT ($x * {multiplier} AS ?result) WHERE {{}}")
            ),
        ).conforms

    assert run(2) is True and run(3) is False and run(2) is True
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert tuple(pool.map(run, (2, 3))) == (True, False)
    assert not validate_shapes(data, graph, trusted_sparql=True).report.verified


def test_function_call_budget_exhaustion_never_becomes_conformance() -> None:
    inner = function("SELECT $x WHERE {}", iri=str(EX.inner))
    outer = function(f"SELECT (<{EX.inner}>($x) AS ?result) WHERE {{}}")
    selected = ShaclFunctionCatalog.from_functions(
        (inner, outer), selected=(str(EX.double),), trusted=True
    )
    graph = shapes(f"$this <{EX.value}> ?v . FILTER(<{EX.double}>(?v) != -1)")
    data = Graph()
    for value in range(600):
        data.add((EX.item, EX.value, Literal(value)))
    result = validate_shapes(
        data,
        graph,
        trusted_sparql=True,
        functions=selected,
        limits=Limits(max_work=1000),
        timeout=30,
    )
    assert result.conforms is None and result.report.outcome == "exhausted"
    assert sum(dict(result.function_calls).values()) == 1000
    assert result.backend.startswith("pyshacl ")
    following = validate_shapes(
        Graph().add((EX.item, EX.value, Literal(2))),
        graph,
        trusted_sparql=True,
        functions=catalog(function("SELECT (-1 AS ?result) WHERE {}")),
    )
    assert following.report.verified and dict(following.function_calls) == {
        str(EX.double): 1
    }
