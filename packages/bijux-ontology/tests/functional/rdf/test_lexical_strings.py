"""Exact string datatypes survive owned storage and supported backend operations."""

import copy
import json
import pickle
from concurrent.futures import ThreadPoolExecutor

import pytest
import rdflib
from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, SH, XSD

from bijux_ontology.documents.export import export_rdf
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.rdf.canonical.export import canonicalize_rdf
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.queries.access import query_rdf
from bijux_ontology.rdf.queries.types import QueryScope
from bijux_ontology.rdf.terms import literal_term, term_key
from bijux_ontology.shacl.checking import validate_shapes

EX = Namespace("https://literal.example/")
RAW = " a  b\tc\n "


@pytest.mark.parametrize("datatype", [XSD.token, XSD.normalizedString])
def test_owned_terms_preserve_identity_metadata_and_python_transport(
    datatype: URIRef,
) -> None:
    exact = literal_term(RAW, datatype=str(datatype))
    normalized = literal_term(RAW, datatype=str(datatype), preserve=False)
    assert exact != normalized and len({exact, normalized}) == 2
    for cloned in (
        copy.copy(exact),
        copy.deepcopy(exact),
        pickle.loads(pickle.dumps(exact)),
    ):
        assert str(cloned) == RAW and term_key(cloned) == term_key(exact)
        assert hash(cloned) == hash(exact)
    with pytest.raises(AttributeError, match="immutable"):
        exact._datatype = XSD.string
    left = Graph().add((EX.s, EX.p, exact))
    right = Graph().add((EX.s, EX.p, normalized))
    assert compare_rdf(left, right).outcome == "disagreement"


@pytest.mark.parametrize("syntax", ["turtle", "nt", "xml", "trig", "nquads", "json-ld"])
def test_exact_export_and_canonical_output_retain_both_lexical_terms(
    syntax: str,
) -> None:
    graph = Graph()
    for value in (" a  b ", "a b"):
        graph.add((EX.s, EX.p, literal_term(value, datatype=str(XSD.token))))
    source = prepare_source(graph)
    result = export_rdf(source, format=syntax)
    assert result.report.verified and result.content is not None
    loaded = prepare_source(result.content, format=syntax)
    selected = loaded.graph("@default") if loaded.is_dataset else loaded.graph()
    assert {term_key(value) for value in selected.objects()} == {
        term_key(value) for value in graph.objects()
    }
    canonical = canonicalize_rdf(source)
    assert canonical.report.verified and canonical.content is not None
    assert b'" a  b "' in canonical.content and b'"a b"' in canonical.content


@pytest.mark.parametrize(
    "body",
    [
        'SELECT ?v WHERE { VALUES ?v { " a  b "^^x:token } }',
        'ASK { VALUES (?v ?n) { (" a  b "^^x:token 1) } FILTER(STR(?v)="a b") }',
        'ASK { ?s ?p " a  b "^^x:token }',
        'SELECT (x:normalizedString(" a\\tb ") AS ?v) WHERE {}',
        'SELECT (STRDT(" a  b ", x:token) AS ?v) WHERE {}',
        'SELECT (STRDT(" a  b ", ?d) AS ?v) WHERE { VALUES ?d { x:token } }',
    ],
)
def test_query_literal_construction_is_an_explicit_conversion_boundary(
    body: str,
) -> None:
    result = query_rdf(
        Graph(),
        "PREFIX x: <http://www.w3.org/2001/XMLSchema#> " + body,
        scope=QueryScope("default"),
    )
    assert result.answer is None and not result.rows and not result.report.complete
    assert any(
        issue.code == "rdf.literal_conversion_unverified"
        for issue in result.report.issues
    )


def test_actual_query_and_shacl_workers_preserve_data_identity_and_findings() -> None:
    raw = literal_term(" a  b ", datatype=str(XSD.token))
    normalized = literal_term("a b", datatype=str(XSD.token))
    graph = Graph().add((EX.s, EX.p, raw)).add((EX.s, EX.p, normalized))
    result = query_rdf(
        graph, "SELECT DISTINCT ?v WHERE {?s ?p ?v}", scope=QueryScope("default")
    )
    assert result.report.verified and {str(row[0]) for row in result.rows} == {
        " a  b ",
        "a b",
    }
    graph.remove((EX.s, EX.p, normalized))
    shapes = (
        Graph()
        .add((EX.Shape, RDF.type, SH.NodeShape))
        .add((EX.Shape, SH.targetNode, EX.s))
    )
    prop = BNode()
    shapes.add((EX.Shape, SH.property, prop)).add((prop, SH.path, EX.p)).add(
        (prop, SH.hasValue, raw)
    )
    assert validate_shapes(graph, shapes).report.verified
    shapes.set((prop, SH.hasValue, normalized))
    assert validate_shapes(graph, shapes).conforms is False
    shapes.remove((prop, SH.hasValue, None))
    shapes.add((prop, SH.datatype, XSD.integer))
    finding = validate_shapes(graph, shapes).root_findings[0]
    assert isinstance(finding.value, Literal)
    assert str(finding.value) == " a  b " and finding.value.datatype == XSD.token
    query = BNode()
    shapes.remove((EX.Shape, SH.property, prop))
    shapes.add((EX.Shape, SH.sparql, query)).add(
        (
            query,
            SH.select,
            Literal(
                'SELECT $this WHERE { VALUES ?v { " a  b "^^'
                "<http://www.w3.org/2001/XMLSchema#token> } }"
            ),
        )
    )
    unsupported = validate_shapes(graph, shapes, trusted_sparql=True)
    assert unsupported.conforms is None
    assert any(
        issue.code == "rdf.literal_conversion_unverified"
        for issue in unsupported.report.issues
    )


def test_concurrent_preparation_never_changes_global_literal_normalization() -> None:
    normalization = rdflib.NORMALIZE_LITERALS

    def prepare(index: int) -> str:
        """Exercise independent parsing with distinct noncanonical string terms."""
        lexical = f" value  {index}\tend "
        source = prepare_source(
            f"<urn:s> <urn:p> {json.dumps(lexical)}^^<{XSD.token}> .", format="nt"
        )
        assert rdflib.NORMALIZE_LITERALS is normalization
        return str(next(source.graph().objects()))

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(prepare, range(8))) == [
            f" value  {i}\tend " for i in range(8)
        ]
    assert rdflib.NORMALIZE_LITERALS is normalization
