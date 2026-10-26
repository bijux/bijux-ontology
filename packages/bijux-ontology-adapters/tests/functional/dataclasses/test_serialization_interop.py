"""Framework extraction retains the ordinary core semantic contracts."""

from dataclasses import dataclass, field

from pydantic import BaseModel, FieldSerializationInfo, field_serializer
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import RDF, XSD

from bijux_ontology.declarations.types import (
    ValueDeclaration as Value,
)
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.models.emission.checking import check_emission
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import ModelUsage, RdfModelUsage
from bijux_ontology_adapters.contracts import RdfField
from bijux_ontology_adapters.dataclasses import model_declarations

EX = Namespace("https://output.example/")


def test_dataclass_rdflib_and_actual_pydantic_jsonld_serializers_agree() -> None:
    title_contract = RdfField(
        str(EX.title),
        (Value("literal", datatype=str(XSD.string)),),
        output_contract=True,
        emission="guaranteed",
    )

    @dataclass
    class Item:
        title: str = field(metadata={"rdf": title_contract})

    @dataclass
    class Catalogue:
        items: list[Item] = field(
            metadata={
                "rdf": RdfField(
                    str(EX.items),
                    (Value("object", model="item"),),
                    output_contract=True,
                    emission="guaranteed",
                )
            }
        )

    class JsonItem(BaseModel):
        title: str

        @field_serializer("title")
        def serialize_title(
            self, value: str, info: FieldSerializationInfo
        ) -> str | int:
            return len(value) if info.context and info.context.get("numeric") else value

    class JsonCatalogue(BaseModel):
        items: list[JsonItem]

    extraction = model_declarations(
        {Catalogue: (str(EX.Catalogue),), Item: (str(EX.Item),)},
        identifiers={Catalogue: "catalogue", Item: "item"},
    )
    assert extraction.report.verified
    record = Catalogue([Item("First"), Item("Second")])
    json_record = JsonCatalogue(
        items=[JsonItem(title=item.title) for item in record.items]
    )
    for numeric in (False, True):
        graph = Graph().add((EX.root, RDF.type, EX.Catalogue))
        output = json_record.model_dump(mode="json", context={"numeric": numeric})
        output.update(
            {
                "@id": str(EX.root),
                "@type": str(EX.Catalogue),
                "@context": {"items": str(EX.items), "title": str(EX.title)},
            }
        )
        for index, item in enumerate(record.items):
            child = EX[f"item/{index}"]
            graph.add((EX.root, EX.items, child))
            graph.add((child, RDF.type, EX.Item))
            graph.add(
                (child, EX.title, Literal(len(item.title) if numeric else item.title))
            )
            output["items"][index].update({"@id": str(child), "@type": str(EX.Item)})
        native = check_rdf_emission(
            prepare_source(graph),
            extraction.declarations,
            [RdfModelUsage("catalogue", EX.root)],
        )
        expanded = check_emission(
            output, extraction.declarations, [ModelUsage("catalogue")]
        )
        assert native.verified is expanded.verified is (not numeric)
        assert {issue.code for issue in native.issues} == {
            issue.code for issue in expanded.issues
        }
        if numeric:
            assert {issue.code for issue in native.issues} == {
                "emission.value_mismatch"
            }
