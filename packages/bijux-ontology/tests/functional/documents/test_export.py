"""Real serializer round trips, deliberate dataset scope and caller IO ownership."""

from io import BytesIO
from pathlib import Path

import pytest
from rdflib import BNode, Dataset, Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD

from bijux_ontology.documents.export import export_rdf, write_export
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.errors import InputError, ResourceError
from bijux_ontology.limits import Limits
from bijux_ontology.rdf.snapshots import PreparedSource

EX = "https://example.org/"
S, P, G = (URIRef(EX + value) for value in ("subject", "predicate", "graph"))


@pytest.mark.parametrize("format", ("turtle", "nt", "xml", "trig", "nquads", "json-ld"))
def test_supported_serializers_retain_literals_lists_and_caller_handles(
    format: str, tmp_path: Path
) -> None:
    graph = Graph()
    head = BNode()
    graph.add((S, P, head))
    graph.add((head, RDF.first, Literal("Colour", lang="en-GB")))
    graph.add((head, RDF.rest, RDF.nil))
    graph.bind("ex", URIRef(EX))
    exported = export_rdf(prepare_source(graph), format=format)
    assert exported.content is not None, exported.report.to_dict()
    assert exported.report.verified
    stream = BytesIO()
    write_export(exported, stream)
    assert not stream.closed
    assert stream.getvalue() == exported.content
    destination = tmp_path / "result.rdf"
    write_export(exported, destination)
    assert destination.read_bytes() == exported.content
    with pytest.raises(ResourceError):
        write_export(exported, destination)
    assert destination.read_bytes() == exported.content
    stream.close()
    with pytest.raises(ResourceError, match="closed"):
        write_export(exported, stream)
    bounded = export_rdf(
        prepare_source(graph), format=format, limits=Limits(max_work=1)
    )
    assert bounded.content is None
    assert bounded.report.outcome == "exhausted"


def test_dataset_export_requires_explicit_selection_and_empty_graph_policy() -> None:
    dataset = Dataset()
    dataset.graph(G).add((S, P, Literal("Named")))
    dataset.default_graph.add((S, P, Literal("Default")))
    source = prepare_source(dataset)
    with pytest.raises(InputError, match="graph name"):
        export_rdf(source, format="turtle")
    for format in ("trig", "nquads", "json-ld"):
        exported = export_rdf(source, format=format)
        assert exported.content is not None, exported.report.to_dict()
    selected = export_rdf(source, format="turtle", graph_name=str(G))
    assert selected.content is not None
    assert b"Named" in selected.content and b"Default" not in selected.content
    union = export_rdf(source, format="turtle", union=True)
    assert union.content is not None
    assert b"Named" in union.content and b"Default" in union.content
    unknown_nonempty = PreparedSource(
        dataset,
        is_dataset=True,
        document_iri=None,
        effective_url=None,
        literal_fidelity="source_terms",
        graph_fidelity="nonempty-only",
    )
    unverified = export_rdf(unknown_nonempty, format="trig")
    assert unverified.content is None
    assert unverified.report.outcome == "unverified"
    assert (
        export_rdf(unknown_nonempty, format="trig", preserve_graphs=False).content
        is not None
    )
    dataset.graph(URIRef(EX + "empty"))
    complete = prepare_source(dataset)
    preserved = export_rdf(complete, format="trig")
    assert preserved.content is not None and preserved.report.verified
    reloaded = prepare_source(preserved.content, format="trig")
    assert len(reloaded.graph(EX + "empty")) == 0
    rejected = export_rdf(complete, format="nquads")
    assert rejected.content is None
    assert "rdf.export_empty_graph_loss" in {
        issue.code for issue in rejected.report.issues
    }
    allowed = export_rdf(complete, format="trig", preserve_graphs=False)
    assert allowed.content is not None
    assert allowed.report.exclusions == ("empty-named-graphs",)
    unknown = PreparedSource(
        dataset,
        is_dataset=True,
        document_iri=None,
        effective_url=None,
        literal_fidelity="source_terms",
        graph_fidelity="nonempty-only",
    )
    assert export_rdf(unknown, format="trig").report.outcome == "unverified"
    assert export_rdf(unknown, format="trig", preserve_graphs=False).content is not None
    assert len(complete.graph(EX + "empty")) == 0
    with pytest.raises(InputError):
        write_export(rejected, BytesIO())


def test_export_refuses_serializer_lexical_loss_without_global_normalisation() -> None:
    graph = Graph().add((S, P, Literal("01", datatype=XSD.integer, normalize=False)))
    source = prepare_source(graph)
    faithful = export_rdf(source, format="nt")
    assert faithful.content is not None
    assert b'"01"' in faithful.content
    jsonld = export_rdf(source, format="json-ld")
    assert jsonld.content is None
    assert jsonld.report.exit_code() == 1
    assert str(next(source.graph().objects())) == "01"

    no_qname = prepare_source(Graph().add((S, URIRef("urn:1"), Literal("Text"))))
    unrepresentable = export_rdf(no_qname, format="xml")
    assert unrepresentable.content is None
    assert unrepresentable.report.outcome == "unverified"
