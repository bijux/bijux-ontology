"""Real catalogue output and explicit record metadata share declaration checks."""

import json
from dataclasses import dataclass, field
from typing import Annotated, NotRequired, Required, TypedDict

import pytest
from rdflib import Graph, Namespace
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.declarations.types import ValueDeclaration as Value
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.limits import Limits
from bijux_ontology.models.contracts.checking import check_models
from bijux_ontology.models.emission.checking import check_emission
from bijux_ontology.models.emission.types import ModelUsage
from bijux_ontology.rdf.sources.types import RdfSource
from bijux_ontology_adapters.contracts import RdfField
from bijux_ontology_adapters.dataclasses import (
    model_declarations as dataclass_declarations,
)
from bijux_ontology_adapters.typed_dict import (
    model_declarations as typed_dict_declarations,
)

EX = Namespace("https://example.org/catalogue#")
TITLE = RdfField(
    str(EX.title),
    (Value("literal", datatype=str(XSD.string)),),
    output_contract=True,
    emission="guaranteed",
)


class NamedDictionary(TypedDict):
    title: Required[Annotated[str, TITLE]]


class CatalogueNode(NamedDictionary):
    child: NotRequired[
        Annotated[
            "CatalogueNode",
            RdfField(
                str(EX.child),
                (Value("object", model="node"),),
                output_contract=True,
            ),
        ]
    ]


def test_inherited_dataclass_catalogue_matches_actual_jsonld_and_rdf_output() -> None:
    @dataclass
    class Named:
        title: str = field(metadata={"rdf": TITLE, "alias": "label"})

    @dataclass
    class Item(Named):
        pass

    @dataclass
    class Catalogue(Named):
        items: list["Item"] = field(
            default_factory=list,
            metadata={
                "rdf": RdfField(
                    str(EX.items),
                    (Value("object", model="item"),),
                    output_contract=True,
                    emission="guaranteed",
                ),
                "alias": "entries",
            },
        )

    extracted = dataclass_declarations(
        {Catalogue: (str(EX.Catalogue),), Item: (str(EX.Item),)},
        identifiers={Catalogue: "catalogue", Item: "item"},
    )
    assert extracted.report.verified
    catalogue, item = extracted.declarations
    assert [value.key for value in catalogue.fields] == ["label", "entries"]
    assert item.fields[0].key == "label"
    record = Catalogue("Collection", [Item("One item")])
    output: dict[str, object] = {
        "@context": {"label": str(EX.title), "entries": str(EX.items)},
        "@type": str(EX.Catalogue),
        "label": record.title,
        "entries": [
            {"@type": str(EX.Item), "label": entry.title} for entry in record.items
        ],
    }
    emitted = prepare_source(RdfSource(json.dumps(output), format="json-ld")).graph()
    assert len(tuple(emitted.triples((None, EX.title, None)))) == 2
    ontology = Graph()
    for kind in (EX.Catalogue, EX.Item):
        ontology.add((kind, RDF.type, OWL.Class))
    ontology.add((EX.title, RDF.type, OWL.DatatypeProperty))
    ontology.add((EX.title, RDFS.range, XSD.string))
    ontology.add((EX.items, RDF.type, OWL.ObjectProperty))
    ontology.add((EX.items, RDFS.domain, EX.Catalogue))
    ontology.add((EX.items, RDFS.range, EX.Item))
    assert check_models(extracted.declarations, ontology).verified
    report = check_emission(
        output,
        extracted.declarations,
        (ModelUsage("catalogue"), ModelUsage("item", ("entries", 0))),
    )
    assert report.verified, report.to_dict()
    output["label"] = 7
    assert not check_emission(
        output, extracted.declarations, (ModelUsage("catalogue"),)
    ).verified


def test_typed_dict_inheritance_nested_forward_reference_and_aliases_are_finite() -> (
    None
):
    result = typed_dict_declarations(
        {CatalogueNode: (str(EX.Node),)},
        identifiers={CatalogueNode: "node"},
        aliases={CatalogueNode: {"title": "label"}},
    )
    assert result.report.verified and len(result.declarations) == 1
    (declaration,) = result.declarations
    assert [value.key for value in declaration.fields] == ["label", "child"]
    assert declaration.fields[1].values[0].model == "node"
    assert declaration.fields[1].emission == "unknown"


def test_future_annotations_resolve_only_registered_models_without_type_inference() -> (
    None
):
    namespace: dict[str, object] = {"__name__": __name__}
    exec(
        "from __future__ import annotations\n"
        "from typing import TypedDict, Annotated\n"
        "class Node(TypedDict):\n"
        "    title: Annotated[str, TITLE]\n"
        "    child: Annotated[Node, RdfField(str(EX.child),\n"
        "        (Value('object', model='node'),), output_contract=True)]\n",
        globals(),
        namespace,
    )
    node = namespace["Node"]
    assert isinstance(node, type)
    result = typed_dict_declarations(
        {node: (str(EX.Node),)}, identifiers={node: "node"}
    )
    assert result.report.verified
    assert result.declarations[0].fields[1].values[0].model == "node"


def test_missing_metadata_and_undescribed_custom_serialization_remain_unknown() -> None:
    @dataclass
    class Price:
        amount: float = field(
            metadata={
                "rdf": RdfField(
                    str(EX.amount),
                    (Value("literal", datatype=str(XSD.decimal)),),
                    emission="guaranteed",
                )
            }
        )
        label: str = "default"

    result = dataclass_declarations({Price: (str(EX.Price),)})
    (declaration,) = result.declarations
    assert not result.report.verified
    assert {issue.code for issue in result.report.issues} == {
        "adapter.mapping_unverified",
        "adapter.output_unverified",
    }
    assert declaration.fields[0].values == (Value(),)
    assert declaration.fields[0].emission == "unknown"

    class Opaque(TypedDict):
        amount: "MissingType"  # type: ignore[name-defined]  # noqa: F821

    unresolved = typed_dict_declarations({Opaque: (str(EX.Price),)})
    assert not unresolved.report.verified and not unresolved.declarations[0].fields
    sidecar = RdfField(
        str(EX.amount),
        (Value("literal", datatype=str(XSD.string)),),
        output_contract=True,
    )
    known = typed_dict_declarations(
        {Opaque: (str(EX.Price),)}, field_contracts={Opaque: {"amount": sidecar}}
    )
    assert known.report.verified
    assert known.declarations[0].fields[0].values == sidecar.values


def test_serializer_sidecars_win_while_conflicting_metadata_remains_visible() -> None:
    @dataclass
    class Record:
        title: str = field(metadata={"rdf": TITLE, "alias": "label"})

    changed = RdfField(
        str(EX.description),
        (Value("literal", datatype=str(XSD.integer)),),
        output_contract=True,
    )
    result = dataclass_declarations(
        {Record: (str(EX.Record),)},
        aliases={Record: {"title": "heading"}},
        field_contracts={Record: {"title": changed}},
    )
    assert not result.report.verified
    assert {issue.code for issue in result.report.issues} == {
        "adapter.alias_contract_conflict",
        "adapter.predicate_contract_conflict",
        "adapter.field_contract_conflict",
    }
    declared = result.declarations[0].fields[0]
    assert declared.key == "heading" and declared.predicate == str(EX.description)
    assert declared.values == changed.values


def test_unregistered_nested_models_do_not_trigger_implicit_model_discovery() -> None:
    class Child(TypedDict):
        title: Annotated[str, TITLE]

    class Root(TypedDict):
        child: Annotated[
            Child,
            RdfField(
                str(EX.child), (Value("object", model="child"),), output_contract=True
            ),
        ]

    result = typed_dict_declarations(
        {Root: (str(EX.Root),)}, identifiers={Root: "root"}
    )
    assert len(result.declarations) == 1 and not result.report.verified
    assert any(issue.actual == "child" for issue in result.report.issues)


def test_alias_collisions_and_extraction_exhaustion_preserve_existing_contracts() -> (
    None
):
    @dataclass
    class Record:
        title: str = field(metadata={"rdf": TITLE})
        other: str = field(metadata={"rdf": TITLE})

    with pytest.raises(InputError, match="Duplicate serialization alias"):
        dataclass_declarations(
            {Record: (str(EX.Record),)},
            aliases={Record: {"title": "same", "other": "same"}},
        )
    exhausted = dataclass_declarations(
        {Record: (str(EX.Record),)}, limits=Limits(max_work=1)
    )
    assert not exhausted.declarations and exhausted.report.outcome == "exhausted"
