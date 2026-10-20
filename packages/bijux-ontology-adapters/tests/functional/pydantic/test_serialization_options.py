"""Framework extraction retains the ordinary core semantic contracts."""

from typing import Annotated

from pydantic import BaseModel, Field
from rdflib import Graph, Namespace
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.declarations.types import (
    ValueDeclaration,
)
from bijux_ontology.models.emission.serialization import (
    check_instance,
)
from bijux_ontology.models.emission.types import ModelUsage
from bijux_ontology_adapters.contracts import RdfField
from bijux_ontology_adapters.pydantic import model_declarations

EX = Namespace("https://example.org/")


def vocabulary() -> Graph:
    """Supply only the declared class and literal field contracts."""
    graph = Graph().add((EX.Record, RDF.type, OWL.Class))
    for predicate in (EX.name, EX.note):
        graph.add((predicate, RDF.type, OWL.DatatypeProperty))
        graph.add((predicate, RDFS.domain, EX.Record))
        graph.add((predicate, RDFS.range, XSD.string))
    return graph


def test_actual_pydantic_alias_and_omission_options_are_passed_once() -> None:
    class Record(BaseModel):
        name: Annotated[
            str,
            RdfField(
                str(EX.name),
                (ValueDeclaration("literal", str(XSD.string)),),
                output_contract=True,
                emission="optional",
            ),
        ] = Field(serialization_alias="title")
        note: Annotated[
            str | None,
            RdfField(
                str(EX.note),
                (ValueDeclaration("literal", str(XSD.string)),),
                output_contract=True,
                emission="optional",
            ),
        ] = None

    options = {"mode": "json", "by_alias": True, "exclude_none": True}
    extraction = model_declarations({Record: (str(EX.Record),)}, exclude_none=True)
    calls = []

    def serialize(instance: Record, **kwargs: object) -> object:
        """Use the real framework serializer with exactly the chosen public options."""
        calls.append(kwargs)
        return {"@type": str(EX.Record), **instance.model_dump(**kwargs)}  # type: ignore[arg-type]

    result = check_instance(
        Record(name="Name"),
        serialize,
        extraction.declarations,
        [ModelUsage(extraction.declarations[0].identifier)],
        vocabulary=vocabulary(),
        options=options,
        context={"title": str(EX.name), "note": str(EX.note)},
        declaration_report=extraction.report,
    )
    assert result.report.verified, result.report.to_dict()
    assert calls == [options]
    assert result.output == {"@type": str(EX.Record), "title": "Name"}
    copy = result.document
    assert isinstance(copy, dict)
    copy.clear()
    assert result.document and result.output
