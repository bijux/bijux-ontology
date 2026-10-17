"""Real framework aliases and explicit serializer knowledge."""

from typing import Annotated

import pytest
from pydantic import BaseModel, Field, computed_field, field_serializer
from rdflib import Graph, Namespace
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.declarations import ValueDeclaration as Value
from bijux_ontology.errors import InputError
from bijux_ontology.interfaces.adapters.contracts import RdfField
from bijux_ontology.interfaces.adapters.pydantic import model_declarations
from bijux_ontology.models import check_models

EX = Namespace("https://example.org/")


def test_alias_and_explicit_output_contract_match_real_serialization() -> None:
    class Record(BaseModel):
        title: Annotated[
            str,
            RdfField(
                str(EX.title),
                (Value("literal", datatype=str(XSD.string)),),
                output_contract=True,
                emission="guaranteed",
            ),
        ] = Field(serialization_alias="label")

    result = model_declarations(
        {Record: (str(EX.Record),)}, identifiers={Record: "record"}
    )
    assert result.report.verified
    declaration = result.declarations[0]
    assert declaration.fields[0].key == "label"
    assert declaration.fields[0].emission == "guaranteed"
    assert Record(title="name").model_dump(by_alias=True) == {"label": "name"}
    graph = Graph().add((EX.Record, RDF.type, OWL.Class))
    graph.add((EX.title, RDF.type, OWL.DatatypeProperty))
    graph.add((EX.title, RDFS.range, XSD.string))
    assert check_models(result.declarations, graph).verified


def test_unknown_serializers_and_computed_output_never_gain_presence_guarantees() -> (
    None
):
    class Record(BaseModel):
        title: Annotated[
            str,
            RdfField(
                str(EX.title),
                (Value("literal", datatype=str(XSD.string)),),
                emission="guaranteed",
            ),
        ]

        @field_serializer("title")
        def title_as_number(self, value: str) -> int:
            return len(value)

        @computed_field
        def summary(self) -> str:
            return self.title

    result = model_declarations({Record: (str(EX.Record),)})
    field = result.declarations[0].fields[0]
    assert field.emission == "unknown" and field.values[0].kind == "unknown"
    assert {issue.code for issue in result.report.issues} == {
        "adapter.output_unverified",
        "adapter.computed_output_unverified",
    }


def test_exclusions_and_duplicate_aliases_do_not_silently_rewrite_contracts() -> None:
    contract = RdfField(
        str(EX.title),
        (Value("literal", datatype=str(XSD.string)),),
        output_contract=True,
        emission="guaranteed",
    )

    class Conditional(BaseModel):
        title: Annotated[str, contract] = Field(exclude_if=lambda value: value == "")

    result = model_declarations({Conditional: (str(EX.Record),)})
    assert result.declarations[0].fields[0].emission == "optional"
    assert any(
        issue.code == "adapter.emission_unverified" for issue in result.report.issues
    )

    class Excluded(BaseModel):
        hidden: str = Field(default="hidden", exclude=True, serialization_alias="same")
        title: Annotated[str, contract] = Field(serialization_alias="same")

    result = model_declarations({Excluded: (str(EX.Record),)})
    assert result.report.verified and len(result.declarations[0].fields) == 1

    class Collision(BaseModel):
        title: Annotated[str, contract] = Field(serialization_alias="same")
        other: Annotated[str, contract] = Field(serialization_alias="same")

    with pytest.raises(InputError, match="Duplicate serialization alias"):
        model_declarations({Collision: (str(EX.Record),)})


def test_extraction_limits_retain_prior_models_and_completed_findings() -> None:
    from bijux_ontology.limits import Limits

    class First(BaseModel):
        name: str

    class Second(BaseModel):
        other: str

    result = model_declarations(
        {First: (str(EX.Record),), Second: (str(EX.Record),)},
        limits=Limits(max_issues=1),
    )
    assert len(result.declarations) == 1
    assert result.report.exit_code() == 2 and not result.report.complete
    assert [issue.code for issue in result.report.issues] == [
        "adapter.mapping_unverified",
        "execution.limit_exceeded",
    ]
    bounded = model_declarations({First: ()}, limits=Limits(max_work=1))
    assert not bounded.declarations and bounded.report.exit_code() == 2
