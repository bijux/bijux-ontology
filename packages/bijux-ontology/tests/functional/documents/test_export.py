"""Real serializer round trips, deliberate dataset scope and caller IO ownership."""

import os
from io import BytesIO
from pathlib import Path

import pytest
from rdflib import BNode, Dataset, Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD

from bijux_ontology.diagnostics.errors import InputError, ResourceError
from bijux_ontology.documents.export import export_rdf, write_export
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.limits import Limits
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import RdfSource

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


@pytest.mark.parametrize("format", ("trig", "nquads", "json-ld"))
def test_dataset_serializers_preserve_authoritative_resource_selection(
    format: str,
) -> None:
    """Resource graph pins cannot expand into unrelated backing graph contents."""
    dataset = Dataset()
    dataset.graph(G).add((S, P, Literal("Named")))
    dataset.default_graph.add((S, P, Literal("Default")))
    for graph_name, union in ((str(G), False), ("@default", False), (None, True)):
        source = prepare_source(RdfSource(dataset, graph_name=graph_name, union=union))
        exported = export_rdf(source, format=format)
        assert exported.content is not None and exported.report.verified
        restored = prepare_source(exported.content, format=format)
        assert compare_rdf(source, restored).verified
        assert len(tuple(restored.dataset().quads())) == (2 if union else 1)
        if graph_name == str(G):
            assert len(restored.graph(str(G))) == 1
        elif union:
            assert len(restored.graph("@default")) == 2
        else:
            assert len(restored.graph("@default")) == 1
        with pytest.raises(InputError, match="conflicts"):
            export_rdf(source, format=format, graph_name=EX + "other")

    empty = URIRef(EX + "empty")
    dataset.graph(empty)
    selected_empty = prepare_source(RdfSource(dataset, graph_name=str(empty)))
    empty_export = export_rdf(selected_empty, format=format)
    if format == "trig":
        assert empty_export.content is not None and empty_export.report.verified
        empty_after = prepare_source(empty_export.content, format=format)
        assert compare_rdf(selected_empty, empty_after).verified
        assert len(empty_after.graph(empty)) == 0
    else:
        assert empty_export.content is None and not empty_export.report.verified


@pytest.mark.parametrize("failure_point", ("fsync", "replace"))
def test_failed_atomic_publication_preserves_existing_file_and_cleans_private_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    exported = export_rdf(
        prepare_source(Graph().add((S, P, Literal("new")))), format="nt"
    )
    destination = tmp_path / "result.nt"
    destination.write_bytes(b"existing exact bytes")
    destination.chmod(0o640)
    original_mode = destination.stat().st_mode

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("Simulated filesystem publication failure")

    with monkeypatch.context() as failure:
        failure.setattr(os, failure_point, fail)
        with pytest.raises(ResourceError, match="publication failure"):
            write_export(exported, destination, overwrite=True)
    assert destination.read_bytes() == b"existing exact bytes"
    assert destination.stat().st_mode == original_mode
    assert sorted(path.name for path in tmp_path.iterdir()) == ["result.nt"]
    write_export(exported, destination, overwrite=True)
    assert destination.read_bytes() == exported.content
    assert destination.stat().st_mode == original_mode
    assert sorted(path.name for path in tmp_path.iterdir()) == ["result.nt"]
