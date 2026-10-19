"""Chosen JSON-LD contexts preserve the selected RDF scope or yield no export."""

import json

from rdflib import BNode, Dataset, Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD

from bijux_ontology.documents.compaction import export_jsonld
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.jsonld.resources import OfflineDocumentLoader
from bijux_ontology.limits import Limits

EX = "https://example.org/"
S, P, G = (URIRef(EX + name) for name in ("subject", "predicate", "graph"))


def test_compaction_uses_actual_reverse_id_language_and_logical_base() -> None:
    graph = Graph()
    graph.add((S, URIRef(EX + "knows"), URIRef(EX + "friend")))
    graph.add((URIRef(EX + "friend"), URIRef(EX + "parent"), S))
    graph.add((S, URIRef(EX + "label"), Literal("Colour", lang="en-GB")))
    graph.add((S, P, Literal("01", datatype=XSD.integer, normalize=False)))
    context = {
        "id": "@id",
        "knows": {"@id": EX + "knows", "@type": "@id"},
        "children": {"@reverse": EX + "parent", "@type": "@id"},
        "labels": {"@id": EX + "label", "@container": "@language"},
        "number": {"@id": str(P), "@type": str(XSD.integer)},
    }
    exported = export_jsonld(prepare_source(graph), context=context, base=EX)
    assert exported.content is not None, exported.report.to_dict()
    assert exported.report.verified
    output = json.loads(exported.content)
    assert output["@context"] == context
    output = next(node for node in output["@graph"] if node["id"] == "subject")
    assert output["knows"] == "friend"
    assert output["labels"] == {"en-gb": "Colour"}
    assert output["number"] == "01"
    framed = export_jsonld(
        prepare_source(graph),
        context=context,
        base=EX,
        frame={"@id": str(S), "children": {}},
    )
    assert framed.content is not None, framed.report.to_dict()
    assert json.loads(framed.content)["children"]["id"] == "friend"
    assert str(next(graph.objects(S, P))) == "01"


def test_compaction_retains_shared_blank_nodes_and_exact_graph_ownership() -> None:
    dataset = Dataset()
    shared = BNode()
    dataset.default_graph.add((S, P, shared))
    dataset.graph(G).add((shared, P, Literal("Named")))
    dataset.graph(G).add((shared, RDF.type, URIRef(EX + "Thing")))
    source = prepare_source(dataset)
    exported = export_jsonld(source, context={"p": str(P), "type": "@type"})
    assert exported.content is not None, exported.report.to_dict()
    assert exported.report.verified
    selected = export_jsonld(source, context={}, graph_name=str(G))
    assert selected.content is not None, selected.report.to_dict()
    assert b'"@graph"' not in selected.content
    empty = URIRef(EX + "empty")
    dataset.graph(empty)
    complete = prepare_source(dataset)
    rejected = export_jsonld(complete, context={})
    assert rejected.content is None
    assert "rdf.export_empty_graph_loss" in {
        issue.code for issue in rejected.report.issues
    }
    excluded = export_jsonld(complete, context={}, preserve_graphs=False)
    assert excluded.content is not None, excluded.report.to_dict()
    assert "empty-named-graphs" in excluded.report.exclusions
    assert empty in {graph.identifier for graph in complete.dataset().graphs()}


def test_compaction_owns_offline_context_and_reports_invalid_or_bounded_work() -> None:
    source = prepare_source(Graph().add((S, P, Literal("Text"))))
    context_url = EX + "contexts/root"
    context = {"@context": {"@import": "child", "label": str(P)}}
    resources = OfflineDocumentLoader(
        {
            context_url: context,
            EX + "contexts/child": {"@context": {"id": "@id"}},
        }
    )
    exported = export_jsonld(source, context=context_url, document_loader=resources)
    assert exported.content is not None, exported.report.to_dict()
    assert json.loads(exported.content)["label"] == "Text"
    assert context["@context"]["@import"] == "child"
    unavailable = export_jsonld(source, context=context_url)
    assert unavailable.content is None and unavailable.report.outcome == "input_error"
    malformed = export_jsonld(
        source, context={"bad": {"@reverse": str(P), "@id": str(P)}}
    )
    assert malformed.content is None and malformed.report.outcome == "input_error"
    bounded = export_jsonld(source, context={}, limits=Limits(max_work=1))
    assert bounded.content is None and bounded.report.outcome == "exhausted"


def test_context_cannot_hide_required_rdf_meaning_with_null_or_aliases() -> None:
    source = prepare_source(Graph().add((S, P, Literal("Kept"))))
    for context in ({str(P): None}, {"predicate": None, "@vocab": EX + "wrong/"}):
        result = export_jsonld(source, context=context)
        if result.content is not None:
            assert result.report.verified
            assert str(P).encode() in result.content
        else:
            assert not result.report.verified
    dropped = export_jsonld(source, context={}, frame={"@id": EX + "absent"})
    assert dropped.content is None
    assert "rdf.structure_changed" in {issue.code for issue in dropped.report.issues}
