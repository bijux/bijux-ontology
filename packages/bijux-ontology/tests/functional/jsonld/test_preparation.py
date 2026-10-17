"""Real controlled JSON-LD conversion preserves dataset identity without globals."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
import rdflib
from pyld import jsonld  # type: ignore[import-untyped]
from rdflib import XSD, BNode, Literal, URIRef

from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.errors import InputError, ResourceError
from bijux_ontology.jsonld.conversion import convert_to_rdf
from bijux_ontology.jsonld.preparation import prepare_jsonld
from bijux_ontology.jsonld.resources import context_document_loader
from bijux_ontology.limits import Limits
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.vocabulary import load_vocabulary

EX = "https://example.org/"


def test_logically_based_dataset_retains_empty_graphs_blank_links_and_literals() -> (
    None
):
    calls: list[str] = []
    document = {
        "@context": {"p": {"@id": EX + "p", "@type": "@id"}},
        "@id": EX + "root",
        "p": "_:empty",
        EX + "number": [
            {"@value": "01", "@type": "http://www.w3.org/2001/XMLSchema#integer"},
            {"@value": "1", "@type": "http://www.w3.org/2001/XMLSchema#integer"},
        ],
        "@included": [
            {"@id": "_:empty", "@graph": []},
            {"@id": EX + "empty", "@graph": []},
            {"@id": EX + "populated", "@graph": [{"p": "relative"}]},
        ],
    }

    def loader(url: str, options: dict[str, object]) -> dict[str, object]:
        """The retrieval URL supplies the default base, independent of its alias."""
        calls.append(url)
        return {
            "contextUrl": None,
            "documentUrl": EX + "effective/data.jsonld",
            "document": document,
        }

    prepared = prepare_jsonld(EX + "requested", document_loader=loader)
    assert prepared.report.verified, prepared.report.to_dict()
    assert prepared.source is not None
    source = prepared.source
    assert source.document_iri == EX + "requested"
    assert source.effective_url == EX + "effective/data.jsonld"
    assert source.graph_fidelity == "complete"
    assert source.literal_fidelity == "source_terms"
    dataset = source.dataset()
    target = next(dataset.default_graph.objects(URIRef(EX + "root"), URIRef(EX + "p")))
    assert isinstance(target, BNode)
    names = {graph.identifier for graph in dataset.graphs()}
    assert target in names
    assert URIRef(EX + "empty") in names
    assert len(dataset.graph(target)) == 0
    populated = dataset.graph(URIRef(EX + "populated"))
    assert (None, URIRef(EX + "p"), URIRef(EX + "effective/relative")) in populated
    assert {
        str(value)
        for value in dataset.default_graph.objects(
            URIRef(EX + "root"), URIRef(EX + "number")
        )
    } == {"01", "1"}
    with pytest.raises(InputError, match="graph name"):
        source.graph()
    dataset.graph(URIRef(EX + "populated")).remove((None, None, None))
    assert len(source.dataset()) > 0
    assert calls == [EX + "requested"]
    nquads = convert_to_rdf(document, base=EX + "effective/data.jsonld")
    assert "context.rdf_empty_graph_loss" in {
        issue.code for issue in nquads.report.issues
    }


def test_default_graph_exports_and_interoperability_failures_have_clear_scope() -> None:
    prepared = prepare_jsonld({EX + "p": "Text"})
    assert prepared.source is not None
    assert (
        None,
        URIRef(EX + "p"),
        Literal("Text", datatype=XSD.string),
    ) in prepared.source.graph()
    token = {
        EX + "p": {
            "@value": " a\n  b ",
            "@type": "http://www.w3.org/2001/XMLSchema#token",
        }
    }
    unsupported = prepare_jsonld(token)
    assert unsupported.source is None
    assert unsupported.report.outcome == "unverified"
    invalid = prepare_jsonld({EX + "p": object()})
    assert invalid.source is None
    assert invalid.report.outcome == "input_error"
    bounded = prepare_jsonld({EX + "p": "Text"}, limits=Limits(max_work=1))
    assert bounded.source is None
    assert bounded.report.outcome == "exhausted"


def test_concurrent_contexts_keep_owned_resources_and_global_policies() -> None:
    barrier = Barrier(2)
    default_loader = jsonld.get_document_loader()
    normalisation = rdflib.NORMALIZE_LITERALS

    def convert(index: int) -> set[str]:
        """Give each operation a different payload under the same logical URL."""

        def loader(url: str, options: dict[str, object]) -> dict[str, object]:
            """Force overlapping resolution so a global cache would be exposed."""
            barrier.wait(timeout=5)
            return {
                "contextUrl": None,
                "documentUrl": url,
                "document": {"@context": {"p": EX + f"p{index}"}},
            }

        result = prepare_jsonld(
            {"@context": EX + "context", "p": "Text"}, document_loader=loader
        )
        assert result.report.verified, result.report.to_dict()
        assert result.source is not None
        return {str(value) for value in result.source.graph().predicates()}

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(convert, (0, 1)))
    assert results == [{EX + "p0"}, {EX + "p1"}]
    assert jsonld.get_document_loader() is default_loader
    assert rdflib.NORMALIZE_LITERALS is normalisation


def test_jsonld_snapshot_loading_reuses_contexts_across_required_imports(
    tmp_path: Path,
) -> None:
    context_iri = EX + "context"
    imported_iri = EX + "imported"
    context = {
        "@context": {
            "owl": "http://www.w3.org/2002/07/owl#",
            "imports": {"@id": "owl:imports", "@type": "@id"},
        }
    }
    document = {
        "@context": context_iri,
        "@graph": [
            {"@id": EX + "ontology", "@type": "owl:Ontology", "imports": imported_iri},
            {"@id": "Local", "@type": "owl:Class"},
        ],
    }
    imported = {"@context": context_iri, "@id": EX + "Imported", "@type": "owl:Class"}
    snapshot = tmp_path / "ontology.snapshot"
    snapshot.write_text(json.dumps(document), encoding="utf-8")
    calls = []

    def loader(url: str, options: dict[str, object]) -> dict[str, object]:
        calls.append(url)
        return {"contextUrl": None, "documentUrl": url, "document": context}

    source = RdfSource(
        snapshot,
        format="json-ld",
        document_iri=EX + "original",
        effective_url=EX + "vocab/",
    )
    vocabulary = load_vocabulary(
        source,
        imports={imported_iri: RdfSource(json.dumps(imported), format="json-ld")},
        document_loader=context_document_loader(loader),
    )
    assert calls == [context_iri]
    assert (URIRef(EX + "vocab/Local"), None, None) in vocabulary.local
    assert (URIRef(EX + "Imported"), None, None) in vocabulary.imports[0]
    with pytest.raises(ResourceError, match="not verified"):
        prepare_source(source)
    inline = prepare_source(
        json.dumps({"@id": EX + "item", EX + "name": "Colour"}), format="json-ld"
    )
    assert (URIRef(EX + "item"), URIRef(EX + "name"), None) in inline.graph()
