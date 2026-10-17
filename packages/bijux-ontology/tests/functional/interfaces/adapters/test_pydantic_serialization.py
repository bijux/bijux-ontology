"""Ordinary nested Pydantic samples use actual output and one semantic source."""

from typing import Annotated, Any, ClassVar, cast

import pytest
from pydantic import BaseModel, Field, PlainSerializer, model_serializer
from rdflib import Graph, Namespace
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    OutputRequirement,
    ValueDeclaration,
)
from bijux_ontology.errors import InputError
from bijux_ontology.interfaces.adapters.pydantic import (
    check_instance,
    model_declarations,
)

EX = Namespace("https://example.org/catalogue/")
CONTEXT: dict[str, object] = {
    "@vocab": str(EX),
    "label": str(EX.label),
    "items": str(EX.items),
    "quantity": str(EX.quantity),
    "note": str(EX.note),
}
OPTIONS: dict[str, object] = {"mode": "json", "by_alias": True, "exclude_none": True}


class Item(BaseModel):
    kind: str = Field(serialization_alias="@type")
    identity: str = Field(serialization_alias="@id")
    label: str = Field(exclude_if=lambda value: value == "omit")
    quantity: Annotated[
        int,
        PlainSerializer(
            lambda value: str(value) if value < 0 else value,
            return_type=Any,
            when_used="json",
        ),
    ] = 1
    note: str | None = None


class Catalogue(BaseModel):
    calls: ClassVar[list[dict[str, object]]] = []
    kind: str = Field(serialization_alias="@type")
    identity: str = Field(serialization_alias="@id")
    items: list[Item]

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        type(self).calls.append(kwargs)
        return super().model_dump(**kwargs)


def catalogue() -> Catalogue:
    return Catalogue(
        kind="Catalogue",
        identity=str(EX.catalogue),
        items=[
            Item(kind="Item", identity=str(EX.one), label="One"),
            Item(kind="Item", identity=str(EX.two), label="Two"),
        ],
    )


def vocabulary() -> Graph:
    graph = Graph()
    for kind in (EX.Catalogue, EX.Item):
        graph.add((kind, RDF.type, OWL.Class))
    graph.add((EX.items, RDF.type, OWL.ObjectProperty))
    graph.add((EX.items, RDFS.domain, EX.Catalogue))
    graph.add((EX.items, RDFS.range, EX.Item))
    for predicate, datatype in (
        (EX.label, XSD.string),
        (EX.quantity, XSD.integer),
        (EX.note, XSD.string),
    ):
        graph.add((predicate, RDF.type, OWL.DatatypeProperty))
        graph.add((predicate, RDFS.domain, EX.Item))
        graph.add((predicate, RDFS.range, datatype))
    return graph


def test_ordinary_nested_profile_dumps_once_and_returns_owned_checked_output() -> None:
    Catalogue.calls.clear()
    result = check_instance(
        catalogue(),
        vocabulary=vocabulary(),
        context=CONTEXT,
        options=OPTIONS,
        requirements={(Item, "label"): OutputRequirement(require_property=True)},
    )
    assert result.report.verified, result.report.to_dict()
    assert Catalogue.calls == [OPTIONS]
    assert isinstance(result.output, dict) and "@context" not in result.output
    assert cast(dict[str, Any], result.output)["items"][0]["quantity"] == 1
    assert "note" not in cast(dict[str, Any], result.output)["items"][0]
    assert cast(dict[str, Any], result.document)["@context"] == CONTEXT
    copy = result.output
    copy.clear()
    assert result.output
    assert any("not serialized in sample" in item for item in result.report.exclusions)


def test_explicit_output_requirements_detect_missing_child_without_child_bindings() -> (
    None
):
    value = catalogue()
    value.items[1].label = "omit"
    result = check_instance(
        value,
        vocabulary=vocabulary(),
        context=CONTEXT,
        options=OPTIONS,
        requirements={(Item, "label"): OutputRequirement(require_property=True)},
    )
    assert not result.report.verified
    assert any(
        issue.code == "emission.required_occurrence_missing"
        and issue.location == "/items/1/label"
        for issue in result.report.issues
    )
    assert cast(dict[str, Any], result.output)["items"][1].get("label") is None


def test_annotated_serializer_actual_datatype_is_checked_without_python_inference() -> (
    None
):
    value = catalogue()
    value.items[0].quantity = -1
    wrong = check_instance(
        value, vocabulary=vocabulary(), context=CONTEXT, options=OPTIONS
    )
    assert not wrong.report.verified
    assert cast(dict[str, Any], wrong.output)["items"][0]["quantity"] == "-1"
    assert any(
        issue.code == "models.datatype_conflict" for issue in wrong.report.issues
    )
    python = check_instance(
        value,
        vocabulary=vocabulary(),
        context=CONTEXT,
        options={**OPTIONS, "mode": "python"},
    )
    assert python.report.verified, python.report.to_dict()
    assert cast(dict[str, Any], python.output)["items"][0]["quantity"] == -1
    strict = model_declarations({Item: (str(EX.Item),)})
    assert not strict.report.verified
    assert any(
        issue.code == "adapter.mapping_unverified" for issue in strict.report.issues
    )


def test_independent_inventory_is_not_rewritten_when_context_mapping_changes() -> None:
    inventory = [
        ModelDeclaration(
            "catalogue",
            (str(EX.Catalogue),),
            (
                FieldDeclaration(
                    "items", str(EX.items), (ValueDeclaration("object", model="item"),)
                ),
            ),
        ),
        ModelDeclaration(
            "item",
            (str(EX.Item),),
            (
                FieldDeclaration(
                    "label",
                    str(EX.label),
                    (ValueDeclaration("literal", datatype=str(XSD.string)),),
                    output=OutputRequirement(require_property=True),
                ),
            ),
        ),
    ]
    good = check_instance(
        catalogue(),
        vocabulary=vocabulary(),
        context=CONTEXT,
        options=OPTIONS,
        inventory=inventory,
    )
    assert good.report.verified, good.report.to_dict()
    changed = check_instance(
        catalogue(),
        vocabulary=vocabulary(),
        context={**CONTEXT, "label": str(EX.changed)},
        options=OPTIONS,
        inventory=inventory,
    )
    assert not changed.report.verified
    assert any(
        issue.code == "context.mapping_mismatch"
        and issue.location.startswith("/items/")
        for issue in changed.report.issues
    )
    assert inventory[1].fields[0].predicate == str(EX.label)


def test_actual_options_and_custom_shapes_remain_explicit() -> None:
    value = catalogue()
    exact = {**OPTIONS, "exclude_unset": True, "exclude_defaults": True}
    omitted = check_instance(
        value, vocabulary=vocabulary(), context=CONTEXT, options=exact
    )
    assert omitted.report.verified, omitted.report.to_dict()
    assert "quantity" not in cast(dict[str, Any], omitted.output)["items"][0]
    unaliased = check_instance(
        value,
        vocabulary=vocabulary(),
        context={**CONTEXT, "kind": "@type", "identity": "@id"},
        options={"mode": "json", "by_alias": False, "exclude_none": False},
    )
    assert unaliased.report.verified, unaliased.report.to_dict()
    assert cast(dict[str, Any], unaliased.output)["items"][0]["note"] is None

    class Transformed(BaseModel):
        label: str

        @model_serializer
        def transformed(self) -> dict[str, object]:
            return {"@type": "Item", "replacement": self.label}

    custom = check_instance(
        Transformed(label="Name"),
        vocabulary=vocabulary(),
        context=CONTEXT,
        options=OPTIONS,
    )
    assert not custom.report.verified
    assert any(
        issue.code == "adapter.output_unverified" for issue in custom.report.issues
    )
    with pytest.raises(InputError, match="Unsupported"):
        check_instance(
            value,
            vocabulary=vocabulary(),
            context=CONTEXT,
            options={"mode": "json", "include": {"items"}},
        )


def test_anonymous_nested_nodes_need_no_manual_array_index_bindings() -> None:
    class AnonymousItem(BaseModel):
        kind: str = Field(serialization_alias="@type")
        label: str

    class AnonymousCatalogue(BaseModel):
        kind: str = Field(serialization_alias="@type")
        items: list[AnonymousItem]

    value = AnonymousCatalogue(
        kind="Catalogue",
        items=[
            AnonymousItem(kind="Item", label="One"),
            AnonymousItem(kind="Item", label="Two"),
        ],
    )
    result = check_instance(
        value,
        vocabulary=vocabulary(),
        context=CONTEXT,
        options=OPTIONS,
        requirements={
            (AnonymousItem, "label"): OutputRequirement(require_property=True)
        },
    )
    assert result.report.verified, result.report.to_dict()


def test_failed_resource_or_exhausted_preparation_keeps_actual_sample_output() -> None:
    from bijux_ontology.limits import Limits

    calls: list[str] = []

    def changing_loader(url: str, options: dict[str, object]) -> dict[str, object]:
        calls.append(url)
        return {
            "documentUrl": url,
            "contextUrl": None,
            "document": {
                "@context": CONTEXT
                if len(calls) == 1
                else {"@vocab": str(EX), "label": str(EX.changed)}
            },
        }

    retained = check_instance(
        catalogue(),
        vocabulary=vocabulary(),
        context="https://example.org/context",
        options=OPTIONS,
        document_loader=changing_loader,
    )
    assert retained.report.verified, retained.report.to_dict()
    assert calls == ["https://example.org/context"]
    unresolved = check_instance(
        catalogue(),
        vocabulary=vocabulary(),
        context="https://unavailable.example/context",
        options=OPTIONS,
    )
    assert not unresolved.report.verified and unresolved.output
    assert unresolved.report.outcome == "input_error"
    bounded = check_instance(
        catalogue(),
        vocabulary=vocabulary(),
        context=CONTEXT,
        options=OPTIONS,
        limits=Limits(max_work=1),
    )
    assert not bounded.report.complete and bounded.output
    assert any(issue.kind == "exhausted" for issue in bounded.report.issues)


def test_custom_reference_only_child_does_not_claim_nested_model_coverage() -> None:
    class ReferenceItem(BaseModel):
        label: str

        @model_serializer
        def reference(self) -> dict[str, object]:
            return {"@id": str(EX.reference)}

    class Parent(BaseModel):
        kind: str = Field(serialization_alias="@type")
        items: ReferenceItem

    graph = Graph().add((EX.Catalogue, RDF.type, OWL.Class))
    graph.add((EX.items, RDF.type, OWL.ObjectProperty))
    result = check_instance(
        Parent(kind="Catalogue", items=ReferenceItem(label="Hidden")),
        vocabulary=graph,
        context=CONTEXT,
        options=OPTIONS,
    )
    assert not result.report.verified
    assert any(
        issue.code == "adapter.output_unverified" and issue.location == "/items"
        for issue in result.report.issues
    )
