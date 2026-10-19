"""Supplied class meaning through native schema and annotation engines."""

import json
from typing import cast

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.compare import isomorphic
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.errors import InputError
from bijux_ontology.interfaces.adapters.obo_graphs import (
    OboGraphsDocument,
    read_obo_graphs,
    write_obo_graphs,
)
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.vocabulary.annotations.graphs.reading import (
    inspect_annotation_graph,
)
from bijux_ontology.vocabulary.annotations.graphs.types import AnnotationStatement
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.obo.reading import read_obo_records
from bijux_ontology.vocabulary.restrictions.projection import project_restrictions
from bijux_ontology.vocabulary.sources.loading import load_vocabulary

pytest.importorskip("jsonschema")
EX = Namespace("https://example.org/obographs/")
DEFINITION = URIRef("http://purl.obolibrary.org/obo/IAO_0000115")
XREF = URIRef("http://www.geneontology.org/formats/oboInOwl#hasDbXref")


def _fixture() -> dict[str, object]:
    """An ontology with both edge meanings, a logical definition and qualifiers."""
    return {
        "graphs": [
            {
                "id": str(EX.ontology),
                "nodes": [
                    {
                        "id": str(EX.Child),
                        "type": "CLASS",
                        "lbl": "Child",
                        "meta": {
                            "definition": {
                                "val": "A curated definition",
                                "xrefs": ["PMID:123"],
                                "meta": {
                                    "basicPropertyValues": [
                                        {
                                            "pred": str(EX.curator),
                                            "val": "Curator",
                                            "meta": {"comments": ["Reviewed"]},
                                        }
                                    ]
                                },
                            },
                            "synonyms": [
                                {
                                    "pred": "hasExactSynonym",
                                    "val": "same",
                                    "xrefs": ["PMID:1"],
                                },
                                {
                                    "pred": "hasBroadSynonym",
                                    "val": "same",
                                    "xrefs": ["PMID:2"],
                                },
                            ],
                        },
                    },
                    {"id": str(EX.Genus), "type": "CLASS"},
                    {"id": str(EX.Filler), "type": "CLASS"},
                    {"id": str(EX.part), "type": "PROPERTY", "propertyType": "OBJECT"},
                ],
                "edges": [
                    {"sub": str(EX.Child), "pred": "is_a", "obj": str(EX.Genus)},
                    {"sub": str(EX.Child), "pred": str(EX.part), "obj": str(EX.Filler)},
                ],
                "logicalDefinitionAxioms": [
                    {
                        "definedClassId": str(EX.Child),
                        "genusIds": [str(EX.Genus)],
                        "restrictions": [
                            {"propertyId": str(EX.part), "fillerId": str(EX.Filler)}
                        ],
                    }
                ],
            }
        ]
    }


def _document() -> OboGraphsDocument:
    """Run the actual optional schema and retain the public checked source."""
    result = read_obo_graphs(
        json.dumps(_fixture()),
        graph_id=str(EX.ontology),
        literal_predicates=(EX.curator,),
    )
    assert result.report.verified and result.source is not None, result.report.to_dict()
    return result


def test_class_edges_qualified_metadata_and_logical_definitions_roundtrip_meaning() -> (
    None
):
    document = _document()
    assert document.source is not None
    graph = document.source.graph()
    assert (EX.Child, RDFS.subClassOf, EX.Genus) in graph
    assert (EX.Child, EX.part, EX.Filler) not in graph
    assert list(graph.objects(EX.Child, OWL.equivalentClass))
    view = inspect_vocabulary(load_vocabulary(document.source))
    projected = project_restrictions(view, str(EX.Child))
    assert projected.report.verified
    direct = [
        item for item in projected.items if item.axiom.predicate == RDFS.subClassOf
    ]
    assert len(direct) == 1 and direct[0].quantifier == OWL.someValuesFrom
    assert direct[0].property == EX.part and direct[0].filler == EX.Filler
    records = read_obo_records(view, str(EX.Child))
    assert records.report.verified and len(records.records) == 3
    definition = next(item for item in records.records if item.role == "definition")
    assert definition.attachments[0].cross_references[0].object == Literal("PMID:123")
    qualified = inspect_annotation_graph(
        document.source,
        (AnnotationStatement(EX.Child, DEFINITION, Literal("A curated definition")),),
        annotation_predicates=(XREF, EX.curator, RDFS.comment),
    )
    assert qualified.report.verified and any(
        item.kind == OWL.Annotation for item in qualified.attachments
    )
    assert any(
        path.statement.value == Literal("Reviewed") and len(path.attachments) == 2
        for path in qualified.paths
    )
    exported = write_obo_graphs(
        document.source, graph_id=str(EX.ontology), literal_predicates=(EX.curator,)
    )
    assert (
        exported.report.verified
        and exported.content is not None
        and not exported.omitted
    ), exported.report.to_dict()
    replay = read_obo_graphs(
        exported.content, graph_id=str(EX.ontology), literal_predicates=(EX.curator,)
    )
    assert replay.report.verified and replay.source is not None
    assert isomorphic(graph, replay.source.graph())


def test_unsupported_universal_and_real_individual_assertions_cannot_disappear() -> (
    None
):
    document = _document()
    assert document.source is not None
    graph = document.source.graph()
    universal = BNode()
    graph.add((EX.Child, RDFS.subClassOf, universal))
    graph.add((universal, RDF.type, OWL.Restriction))
    graph.add((universal, OWL.onProperty, EX.part))
    graph.add((universal, OWL.allValuesFrom, EX.Filler))
    graph.add((EX.Child, EX.part, EX.Filler))
    source = prepare_source(graph)
    strict = write_obo_graphs(
        source, graph_id=str(EX.ontology), literal_predicates=(EX.curator,)
    )
    assert strict.content is None and not strict.report.verified
    assert any(item.predicate == OWL.allValuesFrom for item in strict.omitted)
    assert any(
        item.subject == EX.Child
        and item.predicate == EX.part
        and item.value == EX.Filler
        for item in strict.omitted
    )
    lossy = write_obo_graphs(
        source,
        graph_id=str(EX.ontology),
        literal_predicates=(EX.curator,),
        allow_lossy=True,
    )
    assert lossy.content is not None and not lossy.report.verified
    assert any(
        issue.code == "adapter.obographs_projection_loss" for issue in lossy.report
    )
    replay = read_obo_graphs(
        lossy.content, graph_id=str(EX.ontology), literal_predicates=(EX.curator,)
    )
    assert replay.source is not None and not list(
        replay.source.graph().objects(None, OWL.allValuesFrom)
    )


def test_actual_roles_graph_selection_and_source_kind_failures_are_distinct() -> None:
    missing_policy = read_obo_graphs(json.dumps(_fixture()), graph_id=str(EX.ontology))
    assert (
        missing_policy.report.outcome == "unverified" and missing_policy.source is None
    )
    for key, value in (
        ("unknownAxiom", {"operator": "only"}),
        ("domainRangeAxioms", []),
    ):
        supplied = _fixture()
        selected = cast(list[dict[str, object]], supplied["graphs"])[0]
        selected[key] = value
        result = read_obo_graphs(
            json.dumps(supplied),
            graph_id=str(EX.ontology),
            literal_predicates=(EX.curator,),
        )
        assert result.report.outcome == "unverified" and result.source is None
    supplied = _fixture()
    selected = cast(list[dict[str, object]], supplied["graphs"])[0]
    cast(list[dict[str, object]], selected["edges"])[0]["sub"] = True
    invalid = read_obo_graphs(
        json.dumps(supplied),
        graph_id=str(EX.ontology),
        literal_predicates=(EX.curator,),
    )
    assert invalid.report.outcome == "input_error" and invalid.source is None
    absent = read_obo_graphs(
        json.dumps(_fixture()),
        graph_id=str(EX.missing),
        literal_predicates=(EX.curator,),
    )
    assert absent.report.outcome == "input_error"
    supplied = _fixture()
    selected = cast(list[dict[str, object]], supplied["graphs"])[0]
    cast(list[dict[str, object]], selected["nodes"])[0]["type"] = "INDIVIDUAL"
    role = read_obo_graphs(
        json.dumps(supplied),
        graph_id=str(EX.ontology),
        literal_predicates=(EX.curator,),
    )
    assert role.report.outcome == "unverified" and role.source is None


def test_resource_qualifiers_and_conflicting_deprecation_remain_unrepresented() -> None:
    document = _document()
    assert document.source is not None
    graph = document.source.graph()
    owner = next(graph.subjects(EX.curator, Literal("Curator")))
    graph.remove((owner, EX.curator, Literal("Curator")))
    graph.add((owner, EX.curator, EX.Curator))
    graph.add((EX.Child, OWL.deprecated, Literal(True)))
    graph.add((EX.Child, OWL.deprecated, Literal(False)))
    result = write_obo_graphs(
        prepare_source(graph),
        graph_id=str(EX.ontology),
        literal_predicates=(EX.curator,),
    )
    assert result.content is None and not result.report.verified
    assert any(
        item.predicate == EX.curator and item.value == EX.Curator
        for item in result.omitted
    )
    assert sum(item.predicate == OWL.deprecated for item in result.omitted) == 2
    for kind in (OWL.Class, OWL.ObjectProperty, OWL.AnnotationProperty):
        collision = Graph()
        collision.add((EX.ontology, RDF.type, OWL.Ontology))
        collision.add((EX.ontology, RDF.type, kind))
        for allow_lossy in (False, True):
            unsupported = write_obo_graphs(
                prepare_source(collision),
                graph_id=str(EX.ontology),
                allow_lossy=allow_lossy,
            )
            assert unsupported.content is None
            assert unsupported.report.outcome == "unverified"
            assert any(
                "distinct selected roles" in issue.message
                for issue in unsupported.report
            )


def test_physical_dataset_selection_and_owned_blank_witnesses_remain_exact() -> None:
    document = _document()
    assert document.source is not None
    dataset = Dataset()
    for triple in document.source.graph():
        dataset.graph(EX.physical).add(triple)
    dataset.graph(EX.unselected).add((EX.Other, OWL.disjointWith, EX.Child))
    source = prepare_source(dataset)
    ambiguous = write_obo_graphs(
        source, graph_id=str(EX.ontology), literal_predicates=(EX.curator,)
    )
    assert ambiguous.content is None and ambiguous.report.outcome == "input_error"
    selected = write_obo_graphs(
        source,
        graph=EX.physical,
        graph_id=str(EX.ontology),
        literal_predicates=(EX.curator,),
    )
    assert selected.report.verified and selected.content is not None
    assert not selected.omitted
    pinned = prepare_source(RdfSource(dataset, graph_name=str(EX.physical)))
    assert write_obo_graphs(
        pinned, graph_id=str(EX.ontology), literal_predicates=(EX.curator,)
    ).report.verified
    assert isomorphic(document.source.graph(), source.graph(EX.physical))


def test_budgets_and_metadata_guards_never_publish_partial_truth() -> None:
    budget = Budget()
    document = read_obo_graphs(
        json.dumps(_fixture()),
        graph_id=str(EX.ontology),
        literal_predicates=(EX.curator,),
        budget=budget,
    )
    assert document.source is not None
    exported = write_obo_graphs(
        document.source,
        graph_id=str(EX.ontology),
        literal_predicates=(EX.curator,),
        budget=budget,
    )
    assert exported.report.verified and budget.remaining("work") < Limits().max_work
    stopped = read_obo_graphs(
        json.dumps(_fixture()),
        graph_id=str(EX.ontology),
        literal_predicates=(EX.curator,),
        limits=Limits(max_work=1000),
    )
    assert stopped.source is None and stopped.report.outcome == "exhausted"
    document.source.document_iri = "x" * 5000
    guarded = write_obo_graphs(
        document.source,
        graph_id=str(EX.ontology),
        literal_predicates=(EX.curator,),
        limits=Limits(max_bytes=1000),
    )
    assert (
        guarded.report.outcome == "exhausted"
        and guarded.content is None
        and not guarded.report.children
    )
    with pytest.raises(InputError):
        read_obo_graphs(b"{}", graph_id=str(EX.ontology), budget=cast(Budget, object()))
    with pytest.raises(InputError):
        write_obo_graphs(
            document.source, graph_id=str(EX.ontology), limits=cast(Limits, object())
        )
