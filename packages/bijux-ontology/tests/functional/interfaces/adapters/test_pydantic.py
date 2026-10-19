"""Real framework aliases and explicit serializer knowledge."""

from dataclasses import dataclass
from typing import Annotated, Any, cast

import pytest
from pydantic import BaseModel, Field, computed_field, field_serializer
from rdflib import Graph, Namespace
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.declarations import ValueDeclaration as Value
from bijux_ontology.errors import InputError
from bijux_ontology.interfaces.adapters.contracts import RdfField
from bijux_ontology.interfaces.adapters.pydantic import model_declarations
from bijux_ontology.models.contracts.checking import check_models

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


@dataclass(frozen=True)
class MappingHint:
    """Application-owned public metadata interpreted only by its resolver."""

    predicate: str
    reverse: bool = False
    container: str | None = None


def test_foreign_metadata_and_sidecars_preserve_explicit_aliases_and_rdf_traits() -> (
    None
):
    hint = MappingHint(str(EX.related), reverse=True, container="@set")

    class Record(BaseModel):
        related: Annotated[list[str], hint] = Field(serialization_alias="parents")
        title: str

    def resolve(value: object) -> RdfField | None:
        """Declare the chosen output contract without reading Python types."""
        assert isinstance(value, MappingHint)
        return RdfField(
            value.predicate,
            (Value("iri", container=value.container),),
            reverse=value.reverse,
            output_contract=True,
        )

    result = model_declarations(
        {Record: (str(EX.Record),)},
        metadata_resolvers={MappingHint: resolve},
        field_contracts={
            (Record, "title"): RdfField(
                str(EX.title), (Value("literal"),), output_contract=True
            )
        },
    )
    assert result.report.verified
    related, title = result.declarations[0].fields
    assert related.key == "parents" and related.reverse
    assert related.values == (Value("iri", container="@set"),)
    assert title.predicate == str(EX.title)
    assert related.emission == title.emission == "unknown"
    assert Record.model_fields["related"].metadata == [hint]


def test_sidecars_retain_mapping_conflicts_and_unknown_serializer_output() -> None:
    intended = RdfField(str(EX.title), (Value("literal"),))

    class Record(BaseModel):
        title: Annotated[str, intended, MappingHint(str(EX.other))]

        @field_serializer("title")
        def title_as_number(self, value: str) -> int:
            return len(value)

    def resolve(value: object) -> RdfField | None:
        """Leave serializer knowledge unknown unless supplied explicitly."""
        assert isinstance(value, MappingHint)
        return RdfField(value.predicate, (Value("literal"),))

    unknown = model_declarations(
        {Record: ()}, metadata_resolvers={MappingHint: resolve}
    )
    field = unknown.declarations[0].fields[0]
    assert field.predicate == str(EX.title)
    assert field.values == (Value(),) and field.emission == "unknown"
    assert {issue.code for issue in unknown.report.issues} == {
        "adapter.mapping_conflict",
        "adapter.output_unverified",
    }
    described = RdfField(
        str(EX.title),
        (Value("literal"),),
        output_contract=True,
        emission="optional",
    )
    accepted = model_declarations(
        {Record: ()}, field_contracts={(Record, "title"): described}
    )
    assert accepted.report.verified
    assert accepted.declarations[0].fields[0].emission == "optional"
    conflicting = model_declarations(
        {Record: ()},
        field_contracts={
            (Record, "title"): RdfField(
                str(EX.sidecar), (Value("iri"),), output_contract=True
            )
        },
    )
    assert conflicting.report.outcome == "disagreement"
    assert conflicting.declarations[0].fields[0].predicate == str(EX.sidecar)
    with pytest.raises(InputError, match="declared fields"):
        model_declarations(
            {Record: ()}, field_contracts={(Record, "missing"): described}
        )


def test_output_registries_preserve_conflicts_and_bounded_prefixes() -> None:
    from dataclasses import replace

    from bijux_ontology.declarations.types import ModelDeclaration
    from bijux_ontology.limits import Budget, Limits

    small = RdfField(
        str(EX.title),
        (Value("literal", datatype=str(XSD.string)),),
        output_contract=True,
    )

    class Prior(BaseModel):
        title: Annotated[str, small]

    class Oversized(BaseModel):
        title: Annotated[
            str,
            RdfField("urn:" + "x" * 2000, (Value("literal"),), output_contract=True),
        ]

    operation = Budget(Limits(max_bytes=1000, max_issues=1))
    result = model_declarations(
        {Prior: (), Oversized: ()},
        identifiers={Prior: "prior", Oversized: "large"},
        field_contracts={(Prior, "title"): replace(small, predicate=str(EX.other))},
        budget=operation,
    )
    assert result.report.outcome == "exhausted"
    assert any(i.code == "adapter.mapping_conflict" for i in result.report.issues)
    assert [d.identifier for d in result.declarations] == ["prior"]
    with pytest.raises(InputError, match="cannot compete"):
        model_declarations(
            {Prior: ()},
            field_contracts={(Prior, "title"): small},
            serializer_contracts={Prior: ModelDeclaration("prior", ())},
        )
    with pytest.raises(InputError, match="identity or RDF types"):
        model_declarations(
            {Prior: ()}, serializer_contracts={Prior: ModelDeclaration("wrong", ())}
        )

    from rdflib import BNode, Literal, URIRef

    changed = replace(small, predicate=str(EX.other))
    for member in (Literal("title"), BNode("title"), URIRef("title")):
        for reverse in (False, True):
            entries = [((Prior, member), changed), ((Prior, "title"), small)]
            if reverse:
                entries.reverse()
            assert len(dict(entries)) == 2
            with pytest.raises(InputError, match="model/member pairs"):
                model_declarations({Prior: ()}, field_contracts=dict(entries))

    class DistinctHash(str):
        def __hash__(self) -> int:
            return hash(str(self)) + 1

    duplicate: dict[tuple[type[BaseModel], str], RdfField] = {
        (Prior, DistinctHash("title")): changed,
        (Prior, "title"): small,
    }
    assert len(duplicate) == 2
    with pytest.raises(InputError, match="after text normalization"):
        model_declarations({Prior: ()}, field_contracts=duplicate)

    class MemberName(str):
        pass

    assert model_declarations(
        {Prior: ()}, field_contracts={(Prior, MemberName("title")): small}
    ).report.verified

    from pydantic import create_model

    long = create_model("Long", **cast(dict[str, Any], {"f" * 700: (str, ...)}))
    bounded = model_declarations(
        {long: ()}, identifiers={long: "i" * 700}, limits=Limits(max_bytes=1000)
    )
    assert bounded.report.outcome == "exhausted"
    assert all(len(i.location.encode()) <= 1000 for i in bounded.report.issues)
