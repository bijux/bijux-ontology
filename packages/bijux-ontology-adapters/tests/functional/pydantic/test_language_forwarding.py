"""Framework extraction retains the ordinary core semantic contracts."""

from typing import Any, ClassVar, cast

import pytest
from pydantic import BaseModel, Field, field_serializer
from rdflib import Graph, Namespace
from rdflib.namespace import OWL, RDF

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.jsonld.processing.conversion import DirectionPolicy
from bijux_ontology_adapters.pydantic import check_instance

EX = Namespace("https://language.example/")


def _model(language: str = "ar", *, key: str | None = None) -> ModelDeclaration:
    return ModelDeclaration(
        "record",
        (str(EX.Record),),
        (
            FieldDeclaration(
                "label",
                str(EX.label),
                (ValueDeclaration("literal", language=language, direction="rtl"),),
                key=key,
            ),
        ),
    )


def _vocabulary() -> Graph:
    return (
        Graph()
        .add((EX.Record, RDF.type, OWL.Class))
        .add((EX.label, RDF.type, RDF.Property))
    )


class ArabicRecord(BaseModel):
    calls: ClassVar[list[dict[str, object]]] = []
    identity: str = Field(serialization_alias="@id")
    kind: str = Field(serialization_alias="@type")
    label: str = Field(serialization_alias="title")
    note: str | None = None

    @field_serializer("label")
    def label_json(self, value: str) -> dict[str, str]:
        return {"@value": value, "@language": "ar", "@direction": "rtl"}

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        type(self).calls.append(kwargs)
        return super().model_dump(**kwargs)


@pytest.mark.parametrize("policy", ["i18n-datatype", "compound-literal"])
def test_pydantic_sample_infers_direction_without_a_physical_langstring_promise(
    policy: DirectionPolicy,
) -> None:
    record = ArabicRecord(identity=str(EX.root), kind=str(EX.Record), label="اسم")
    options = {
        "mode": "json",
        "by_alias": True,
        "exclude_none": True,
        "exclude_unset": False,
        "exclude_defaults": False,
    }
    context = {"title": str(EX.label), "note": str(EX.note)}
    ArabicRecord.calls.clear()
    result = check_instance(
        record,
        vocabulary=_vocabulary(),
        context=context,
        options=options,
        rdf_direction=policy,
    )
    assert result.report.verified, result.report.to_dict()
    assert ArabicRecord.calls == [options]
    assert cast(dict[str, object], result.output)["title"] == {
        "@value": "اسم",
        "@language": "ar",
        "@direction": "rtl",
    }
    assert "note" not in cast(dict[str, object], result.output)
    independent = _model("en", key="title")
    wrong = check_instance(
        record,
        vocabulary=_vocabulary(),
        context=context,
        options=options,
        inventory=[independent],
        rdf_direction=policy,
    )
    assert not wrong.report.verified and ArabicRecord.calls == [options, options]
    assert any(issue.code == "emission.value_mismatch" for issue in wrong.report.issues)
