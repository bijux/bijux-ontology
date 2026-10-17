"""All SHACL resource roles retain explicit prepared dataset selections."""

from rdflib import Namespace
from rdflib.namespace import OWL, RDF, SH

from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.shacl import validate_shapes

EX = Namespace("https://example.org/")


def test_shacl_data_shapes_ontology_and_imports_share_resource_selection() -> None:
    data = prepare_source(
        RdfSource(
            "@prefix ex:<https://example.org/>. "
            "ex:data { ex:record a ex:Child. } "
            "ex:unselected { ex:other a ex:Child. }",
            format="trig",
            graph_name=str(EX.data),
        )
    )
    shapes = prepare_source(
        RdfSource(
            "@prefix ex:<https://example.org/>. "
            "@prefix sh:<http://www.w3.org/ns/shacl#>. "
            "@prefix owl:<http://www.w3.org/2002/07/owl#>. "
            "ex:shapes { ex:Shape a sh:NodeShape; "
            "sh:targetNode ex:record; sh:class ex:Parent. "
            "ex:Document owl:imports ex:ImportedShapes. } "
            "ex:unselected { ex:Shape sh:rule ex:Unsupported. }",
            format="trig",
            graph_name=str(EX.shapes),
        )
    )
    ontology = prepare_source(
        RdfSource(
            "@prefix ex:<https://example.org/>. "
            "@prefix rdfs:<http://www.w3.org/2000/01/rdf-schema#>. "
            "ex:Child rdfs:subClassOf ex:Parent. "
            "ex:unselected { ex:Child rdfs:subClassOf ex:Poison. }",
            format="trig",
            graph_name="@default",
        )
    )
    imported = RdfSource(
        "@prefix ex:<https://example.org/>. "
        "@prefix sh:<http://www.w3.org/ns/shacl#>. "
        "ex:selected { ex:Nested a sh:NodeShape. } "
        "ex:unselected { ex:Poison a sh:NodeShape; "
        "sh:targetNode ex:record; sh:class ex:Absent. }",
        format="trig",
        graph_name=str(EX.selected),
    )
    result = validate_shapes(
        data,
        shapes,
        ontology=ontology,
        imports={str(EX.ImportedShapes): imported},
    )
    assert result.conforms is True and result.report.verified, result.report.to_dict()
    assert result.shapes_count == 2
    assert (EX.Document, OWL.imports, EX.ImportedShapes) in shapes.graph()
    assert (EX.Shape, RDF.type, SH.NodeShape) in shapes.graph()
