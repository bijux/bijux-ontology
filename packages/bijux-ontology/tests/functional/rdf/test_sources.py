"""Owned RDF ingestion preserves scope, literal terms and caller ownership."""

from io import BytesIO, StringIO
from pathlib import Path
from typing import Any

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, XSD

from bijux_ontology import prepare_source
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.snapshots import copy_graph
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.rdf.terms import literal_term

EX = Namespace("https://example.org/")
TURTLE = "@prefix ex:<https://example.org/>. ex:Image a <http://www.w3.org/2002/07/owl#Class>."


def test_explicit_format_paths_text_bytes_streams_and_caller_handles(
    tmp_path: Path,
) -> None:
    path = tmp_path / "misleading.xml"
    path.write_text(TURTLE)
    stream = BytesIO(TURTLE.encode())
    text_stream = StringIO(TURTLE)
    sources = (path, TURTLE, TURTLE.encode(), stream, text_stream)
    for source in sources:
        prepared = prepare_source(source, format="turtle")
        assert (EX.Image, RDF.type, OWL.Class) in prepared.graph()
        assert str(dict(prepared.graph().namespaces())["ex"]) == str(EX)
    unicode_source = prepare_source(
        '<https://example.org/Image> <https://example.org/label> "café"@fr .',
        format="nt",
    )
    label = next(unicode_source.graph().objects())
    assert isinstance(label, Literal) and label.language == "fr"
    assert not stream.closed and not text_stream.closed
    with pytest.raises(InputError, match="explicit format"):
        prepare_source(TURTLE)
    with pytest.raises(InputError, match="Unsupported RDF format"):
        prepare_source(RdfSource(path, format="unsupported"))


@pytest.mark.parametrize("syntax", ["turtle", "nt"])
def test_literal_lexical_terms_are_preserved_or_loss_is_explicit(syntax: str) -> None:
    source = (
        '<https://example.org/s> <https://example.org/p> "01"^^'
        '<http://www.w3.org/2001/XMLSchema#integer>, "1"^^'
        "<http://www.w3.org/2001/XMLSchema#integer> ."
    )
    if syntax == "nt":
        source = (
            '<https://example.org/s> <https://example.org/p> "01"^^'
            "<http://www.w3.org/2001/XMLSchema#integer> .\n"
            '<https://example.org/s> <https://example.org/p> "1"^^'
            "<http://www.w3.org/2001/XMLSchema#integer> ."
        )
    prepared = prepare_source(source, format=syntax)
    assert {
        (str(value), value.datatype)
        for value in prepared.graph().objects()
        if isinstance(value, Literal)
    } == {("01", XSD.integer), ("1", XSD.integer)}
    token = (
        '<https://example.org/s> <https://example.org/p> " a  b "^^'
        "<http://www.w3.org/2001/XMLSchema#token> ."
    )
    with pytest.raises(InputError, match="lexical preservation"):
        prepare_source(token, format=syntax)
    allowed = prepare_source(token, format=syntax, preserve_literals=False)
    assert allowed.literal_fidelity == "normalisation_allowed"
    assert {str(value) for value in allowed.graph().objects()} == {"a b"}
    assert prepare_source(prepared.graph()).literal_fidelity == "supplied_terms"


def test_rdfxml_loads_without_allowing_document_entities() -> None:
    xml = (
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
        'xml:base="https://example.org/"><rdf:Description rdf:about="Image">'
        '<rdf:type rdf:resource="http://www.w3.org/2002/07/owl#Class"/>'
        "</rdf:Description></rdf:RDF>"
    )
    assert (EX.Image, RDF.type, OWL.Class) in prepare_source(xml, format="xml").graph()
    payload = '<!DOCTYPE rdf:RDF [<!ENTITY example SYSTEM "file:///never-read">]>' + xml
    with pytest.raises(InputError, match="entities"):
        prepare_source(payload, format="xml")


def test_supplied_graph_terms_obey_absolute_iri_boundaries() -> None:
    invalid = (
        (URIRef("relative"), EX.p, EX.object),
        (EX.subject, URIRef("relative"), EX.object),
        (EX.subject, EX.p, URIRef("https://bad host/path")),
        (EX.subject, EX.p, Literal("value", datatype=URIRef("relative"))),
    )
    for triple in invalid:
        graph = Graph().add(triple)
        with pytest.raises(InputError, match="absolute IRI"):
            prepare_source(graph)
        with pytest.raises(InputError, match="absolute IRI"):
            copy_graph(graph)
    for populated in (False, True):
        dataset = Dataset()
        invalid_graph = dataset.graph(URIRef("relative"))
        if populated:
            invalid_graph.add((EX.subject, EX.p, EX.object))
        with pytest.raises(InputError, match="absolute IRI"):
            prepare_source(dataset)
    valid = Graph().add((URIRef("urn:example:subject"), EX.p, BNode()))
    valid.add((URIRef("https://example.org/é"), EX.p, Literal("café", lang="fr")))
    assert len(prepare_source(valid).graph()) == 2


def test_caller_graph_roles_can_preserve_deliberate_blank_node_identity() -> None:
    record = BNode("shared-record")
    data = Graph().add((record, EX.name, Literal("record")))
    shapes = Graph().add((EX.shape, EX.targetNode, record))
    owned_data = copy_graph(data, standardize_apart=False)
    owned_shapes = copy_graph(shapes, standardize_apart=False)
    assert (record, EX.name, Literal("record")) in owned_data
    assert (EX.shape, EX.targetNode, record) in owned_shapes
    owned_data.remove((record, None, None))
    assert len(data) == 1
    assert (record, EX.name, Literal("record")) not in copy_graph(data)


def test_shared_budget_supplies_source_limits_and_rejects_conflicting_limits() -> None:
    budget = Budget(Limits(max_bytes=4))
    with pytest.raises(LimitExceeded, match="max_bytes"):
        prepare_source("<urn:a> <urn:p> <urn:b> .", format="nt", budget=budget)
    with pytest.raises(InputError, match="conflict"):
        prepare_source("", format="nt", budget=budget, limits=Limits(max_bytes=8))


def test_source_metadata_rejects_invalid_runtime_types_with_typed_errors() -> None:
    values: list[dict[str, Any]] = [
        {"format": 123},
        {"format": " "},
        {"document_iri": 123},
        {"document_iri": "relative"},
        {"effective_url": "https://bad host/"},
    ]
    for metadata in values:
        with pytest.raises(InputError):
            RdfSource("", **metadata)
    invalid_content: Any = object()
    with pytest.raises(InputError, match="content"):
        RdfSource(invalid_content, format="turtle")


def test_trig_empty_graph_inventory_is_complete() -> None:
    prepared = prepare_source("<urn:g> {}", format="trig", require_complete_graphs=True)
    assert prepared.graph_fidelity == "complete"
    assert len(prepared.graph("urn:g")) == 0
    with pytest.raises(InputError, match="absent"):
        prepared.graph("urn:missing")


def test_literal_construction_shares_exact_fidelity_and_role_validation() -> None:
    assert str(literal_term("01", datatype=str(XSD.integer))) == "01"
    assert literal_term("name", language="en").language == "en"
    with pytest.raises(InputError, match="rdf:langString"):
        literal_term("name", language="en", datatype=str(XSD.integer))
    with pytest.raises(InputError, match="lexical preservation"):
        literal_term(" a  b ", datatype=str(XSD.token))
    assert str(literal_term(" a  b ", datatype=str(XSD.token), preserve=False)) == "a b"


def test_stream_failures_and_invalid_unicode_are_typed_without_closing_callers() -> (
    None
):
    class Unreadable(BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            raise OSError("unavailable input")

    source = Unreadable()
    with pytest.raises(ResourceError, match="stream"):
        prepare_source(source, format="turtle")
    assert not source.closed
    closed = BytesIO()
    closed.close()
    with pytest.raises(ResourceError, match="stream"):
        prepare_source(closed, format="turtle")
    for supplied in ("\ud800", StringIO("\ud800")):
        with pytest.raises(InputError, match="UTF-8"):
            prepare_source(supplied, format="turtle")


@pytest.mark.parametrize(
    ("syntax", "payload"),
    (
        (
            "turtle",
            '<item> <https://example.org/name> "01"'
            "^^<http://www.w3.org/2001/XMLSchema#integer> .",
        ),
        (
            "json-ld",
            '{"@id":"item","https://example.org/name":{"@value":"01",'
            '"@type":"http://www.w3.org/2001/XMLSchema#integer"}}',
        ),
    ),
)
def test_public_dispatch_preserves_identity_and_charges_one_source(
    syntax: str,
    payload: str,
) -> None:
    budget = Budget(Limits(max_resources=1))
    source = RdfSource(
        payload,
        format=syntax,
        document_iri=str(EX.requested),
        effective_url=str(EX) + "retrieved/",
    )
    prepared = prepare_source(source, budget=budget)
    assert prepared.document_iri == str(EX.requested)
    assert prepared.effective_url == str(EX) + "retrieved/"
    assert list(prepared.graph()) == [
        (
            URIRef(str(EX) + "retrieved/item"),
            EX.name,
            literal_term("01", datatype=str(XSD.integer)),
        )
    ]
    with pytest.raises(LimitExceeded, match="max_resources"):
        prepare_source(source, budget=budget)


def test_resource_selection_retains_dataset_and_rejects_conflicting_choices() -> None:
    source = RdfSource(
        "<urn:Default> <urn:p> <urn:o>. <urn:graph> { <urn:Named> <urn:p> <urn:o>. }",
        format="trig",
        graph_name="urn:graph",
    )
    prepared = prepare_source(source)
    assert set(prepared.graph().subjects()) == {URIRef("urn:Named")}
    assert set(prepared.dataset().default_graph.subjects()) == {URIRef("urn:Default")}
    assert set(prepared.dataset().graph(URIRef("urn:graph")).subjects()) == {
        URIRef("urn:Named")
    }
    assert set(prepared.graph("urn:graph")) == set(prepared.graph())
    with pytest.raises(InputError, match="conflicts"):
        prepared.graph("@default")
    with pytest.raises(InputError, match="conflicts"):
        prepared.graph(union=True)
    invalid: list[dict[str, Any]] = [
        {"graph_name": "@default", "union": True},
        {"graph_name": "_:graph"},
        {"graph_name": BNode("graph")},
        {"union": "false"},
    ]
    for options in invalid:
        with pytest.raises(InputError):
            RdfSource(source.content, format="trig", **options)
