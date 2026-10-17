"""TriG inventory retains exact terms, empty declarations and blank graph roles."""

import pytest
import rdflib
from rdflib import BNode, Literal, URIRef
from rdflib.graph import DATASET_DEFAULT_GRAPH_ID
from rdflib.namespace import XSD

from bijux_ontology import compare_rdf, export_rdf, prepare_source
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Limits

BASE = "https://trig.example/"


def test_source_labels_share_blank_graph_subject_and_object_through_export() -> None:
    normalization = rdflib.NORMALIZE_LITERALS
    source = prepare_source(
        """@base <https://trig.example/> .
        @prefix : <https://trig.example/> .
        @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
        :empty {} # :phantom { }
        _:empty {} _:populated { _:empty :value 01, "1"^^xsd:integer . }
        _:empty :related _:populated .
        :record :note "quoted { } GRAPH <urn:phantom> {}" .""",
        format="trig",
        require_complete_graphs=True,
    )
    dataset = source.dataset()
    subject, _, populated = next(
        dataset.default_graph.triples((None, URIRef(BASE + "related"), None))
    )
    assert isinstance(subject, BNode) and isinstance(populated, BNode)
    assert not dataset.graph(subject) and len(dataset.graph(populated)) == 2
    assert {
        (str(value), value.datatype)
        for value in dataset.graph(populated).objects()
        if isinstance(value, Literal)
    } == {
        ("01", XSD.integer),
        ("1", XSD.integer),
    }
    assert len(list(dataset.graphs())) == 4
    exported = export_rdf(source, format="trig")
    assert exported.content is not None and exported.report.verified, (
        exported.report.to_dict()
    )
    reloaded = prepare_source(
        exported.content, format="trig", require_complete_graphs=True
    )
    assert compare_rdf(source, reloaded).verified
    assert rdflib.NORMALIZE_LITERALS is normalization
    assert {
        str(value)
        for value in reloaded.graph(union=True).objects()
        if isinstance(value, Literal) and value.datatype == XSD.integer
    } == {"01", "1"}


def test_empty_blank_graph_used_only_as_object_keeps_its_identity() -> None:
    source = prepare_source("_:g {} <urn:s> <urn:p> _:g .", format="trig")
    exported = export_rdf(source, format="trig")
    assert exported.content is not None and exported.report.verified
    reloaded = prepare_source(exported.content, format="trig")
    assert compare_rdf(source, reloaded).verified
    node = next(reloaded.graph("@default").objects())
    assert isinstance(node, BNode) and not reloaded.graph(node)


def test_anonymous_graphs_and_empty_only_documents_preserve_exact_inventory() -> None:
    for payload, count, empty in (
        ("[] {} [] { <urn:s> <urn:p> 01 } [] {}", 3, 2),
        ("_:a {} _:b {}", 2, 2),
        ("[] {}", 1, 1),
        ("", 0, 0),
    ):
        source = prepare_source(payload, format="trig")
        dataset = source.dataset()
        graphs = [
            g for g in dataset.graphs() if g.identifier != DATASET_DEFAULT_GRAPH_ID
        ]
        assert len(graphs) == count and sum(not graph for graph in graphs) == empty
        exported = export_rdf(source, format="trig")
        assert exported.content is not None and exported.report.verified
        assert compare_rdf(
            source, prepare_source(exported.content, format="trig")
        ).verified


def test_base_prefixes_quoted_braces_and_graph_inventory_have_separate_scope() -> None:
    source = prepare_source(
        "@base <https://trig.example/> . @prefix ex: <nested/> . "
        'GRAPH <empty> {} ex:é {} <data> { <s> <p> """{ } # {}""" } '
        '<default> <p> "value" .',
        format="trig",
    )
    assert not source.graph(BASE + "empty") and not source.graph(BASE + "nested/é")
    assert len(source.graph(BASE + "data")) == 1
    missing = prepare_source('<default> <p> "value" .', format="trig", base=BASE)
    assert compare_rdf(source, missing).outcome == "disagreement"
    assert compare_rdf(source.graph("@default"), missing.graph("@default")).verified
    only_empty = prepare_source("<urn:g> {}", format="trig")
    absent = prepare_source("", format="trig")
    assert compare_rdf(only_empty, absent).outcome == "disagreement"
    assert compare_rdf(only_empty, absent, ignore_empty_graphs=True).verified


@pytest.mark.parametrize("suffix", ("{}", "{ <urn:s> <urn:p> <urn:o> }"))
def test_named_reserved_default_identifier_is_explicitly_unsupported(
    suffix: str,
) -> None:
    with pytest.raises(InputError, match="reserved default"):
        prepare_source(f"<{DATASET_DEFAULT_GRAPH_ID}> {suffix}", format="trig")


def test_inventory_syntax_nesting_and_graph_resource_bounds_are_not_success() -> None:
    with pytest.raises(InputError):
        prepare_source("<urn:g> {", format="trig")
    with pytest.raises(LimitExceeded, match="max_resources"):
        prepare_source(
            "<urn:a>{} <urn:b>{} <urn:c>{}",
            format="trig",
            limits=Limits(max_resources=3),
        )
    with pytest.raises(LimitExceeded, match="max_depth"):
        prepare_source(
            "<urn:g>{<urn:s> <urn:p> [<urn:p> [<urn:p> <urn:o>]]}",
            format="trig",
            limits=Limits(max_depth=1),
        )
    bounded = export_rdf(
        prepare_source("_:a{} _:b{}", format="trig"),
        format="trig",
        limits=Limits(max_bytes=10),
    )
    assert bounded.content is None and bounded.report.outcome == "exhausted"


@pytest.mark.parametrize(
    "payload,subject,graph",
    (
        (
            "@base <urn:example:x> . <relative> <urn:p> <urn:o> .",
            "urn:relative",
            "@default",
        ),
        (
            "@base <urn:example:x> . <g> { <s> <urn:p> <urn:o> }",
            "urn:s",
            "urn:g",
        ),
        (
            "@base <urn:example:/a/b> . <g> { <s> <urn:p> <urn:o> }",
            "urn:example:/a/s",
            "urn:example:/a/g",
        ),
    ),
)
def test_unsupported_metadata_base_retains_primary_quads_without_full_inventory(
    payload: str,
    subject: str,
    graph: str,
) -> None:
    source = prepare_source(payload, format="trig")
    assert source.graph_fidelity == "nonempty-only"
    assert (URIRef(subject), URIRef("urn:p"), URIRef("urn:o")) in source.graph(graph)
    assert compare_rdf(source.graph(graph), source.graph(graph)).verified
    assert compare_rdf(source, source).outcome == "unverified"
    assert export_rdf(source, format="trig").content is None
    with pytest.raises(InputError, match="cannot establish complete graph inventory"):
        prepare_source(payload, format="trig", require_complete_graphs=True)
