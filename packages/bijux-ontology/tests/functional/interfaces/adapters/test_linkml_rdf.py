"""Installed LinkML RDF dumping retains schema, identifier and enum semantics."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest
from linkml_runtime.dumpers import rdflib_dumper  # type: ignore[import-untyped]
from linkml_runtime.utils.schemaview import SchemaView  # type: ignore[import-untyped]
from pydantic import BaseModel
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.declarations.types import ValueDeclaration
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.errors import ResourceError
from bijux_ontology.interfaces.adapters.contracts import RdfField
from bijux_ontology.interfaces.adapters.linkml import model_declarations
from bijux_ontology.interfaces.adapters.linkml_rdf import (
    RdfInstanceCheck,
    RdfSerializer,
    check_instance,
)
from bijux_ontology.limits import Limits
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import RdfModelUsage

EX = Namespace("https://linkml.example/")
SCHEMA = """
id: https://linkml.example/schema
name: catalogue
prefixes:
  ex: https://linkml.example/
  xsd: http://www.w3.org/2001/XMLSchema#
  rdfs: http://www.w3.org/2000/01/rdf-schema#
classes:
  Named:
    class_uri: ex:Named
    slots: [id, label]
  Item:
    is_a: Named
    class_uri: ex:Item
    slots: [age, status, links]
  Catalogue:
    is_a: Named
    class_uri: ex:Catalogue
    slots: [items]
slots:
  id:
    identifier: true
    range: uri
  label:
    slot_uri: ex:label
    range: string
    required: true
  age:
    slot_uri: ex:age
    range: integer
  status:
    slot_uri: ex:status
    range: Status
  links:
    slot_uri: ex:links
    range: uri
    multivalued: true
  items:
    slot_uri: ex:items
    range: Item
    multivalued: true
    inlined: true
    inlined_as_list: true
types:
  string:
    uri: xsd:string
  integer:
    uri: xsd:integer
  uri:
    uri: xsd:anyURI
enums:
  Status:
    permissible_values:
      ready:
        meaning: ex:Ready
      waiting:
"""


class Item(BaseModel):
    id: str
    label: str
    age: int
    status: str
    links: list[str]


class Catalogue(BaseModel):
    id: str
    label: str
    items: list[Item]


def _instance() -> Catalogue:
    """Use real supported Pydantic instances accepted by the maintained dumper."""
    return Catalogue(
        id="ex:collection",
        label="Collection",
        items=[
            Item(
                id="ex:first",
                label="First",
                age=7,
                status="ready",
                links=["ex:one", "ex:two"],
            ),
            Item(id="ex:second", label="Second", age=8, status="waiting", links=[]),
        ],
    )


def _vocabulary() -> Graph:
    """Supply an independent ontology contract, never a replacement RDF serializer."""
    graph = Graph()
    for kind in (EX.Named, EX.Item, EX.Catalogue):
        graph.add((kind, RDF.type, OWL.Class))
    for name in (EX.label, EX.age, EX.status, EX.links, EX.items):
        graph.add((name, RDF.type, RDF.Property))
    graph.add((EX.label, RDFS.range, XSD.string))
    graph.add((EX.age, RDFS.range, XSD.integer))
    graph.add((EX.items, RDFS.range, EX.Item))
    return graph


def test_actual_linkml_output_inheritance_identifiers_enum_and_nested_multivalues() -> (
    None
):
    view = SchemaView(SCHEMA)
    original = deepcopy(view.schema)
    extraction = model_declarations(view, serializer="rdflib")
    assert extraction.report.verified, extraction.report.to_dict()
    item = next(
        model for model in extraction.declarations if model.identifier == "Item"
    )
    assert not item.subject.allow_blank
    assert "id" not in {field.name for field in item.fields}
    assert {field.name for field in item.fields} == {"label", "age", "status", "links"}
    assert next(field for field in item.fields if field.name == "age").values == (
        ValueDeclaration("literal", datatype=str(XSD.integer)),
    )
    instance = _instance()
    original_instance = instance.model_dump()
    result = check_instance(
        instance,
        view=view,
        vocabulary=_vocabulary(),
        options={},
        class_name="Catalogue",
    )
    assert result.report.verified, result.report.to_dict()
    assert result.source is not None
    graph = result.source.graph()
    assert (EX.collection, RDF.type, EX.Catalogue) in graph
    assert (EX.first, EX.status, EX.Ready) in graph
    assert (EX.second, EX.status, Literal("waiting")) in graph
    assert (EX.first, EX.age, Literal(7)) in graph
    assert set(graph.objects(EX.first, EX.links)) == {EX.one, EX.two}
    assert not tuple(graph.triples((None, EX.id, None)))
    assert view.schema == original
    assert instance.model_dump() == original_instance


def test_real_pre_resolved_local_imports_stay_offline_owned_and_fresh(
    tmp_path: Path,
) -> None:
    imported = tmp_path / "types.yaml"
    imported.write_text(
        "id: https://linkml.example/types\nname: types\ntypes:\n"
        "  string:\n    uri: xsd:string\n  integer:\n    uri: xsd:integer\n"
        "  uri:\n    uri: xsd:anyURI\n"
    )
    root = tmp_path / "schema.yaml"
    root.write_text(
        SCHEMA[: SCHEMA.index("types:")]
        + SCHEMA[SCHEMA.index("enums:") :]
        + "\nimports: [types]\n"
    )
    view = SchemaView(root)
    view.imports_closure()
    before = deepcopy(view.schema_map)
    serializer = RdfSerializer(view, options={})
    imported.unlink()
    root.unlink()
    result = serializer.check(
        _instance(), class_name="Catalogue", vocabulary=_vocabulary()
    )
    assert result.report.verified, result.report.to_dict()
    assert view.schema_map == before
    view.schema.imports.append("https://offline.invalid/changed")
    with pytest.raises(ResourceError, match="does not retrieve imports"):
        model_declarations(view, serializer="rdflib")
    assert serializer.check(
        _instance(), class_name="Catalogue", vocabulary=_vocabulary()
    ).report.verified


def test_changed_mapping_and_actual_dumper_range_have_useful_findings() -> None:
    view = SchemaView(SCHEMA)
    baseline = model_declarations(view, serializer="rdflib")
    changed = SchemaView(SCHEMA)
    changed.schema.slots["age"].range = "string"
    actual = rdflib_dumper.as_rdf_graph(_instance(), changed)
    # The selected maintained dumper's xsd:string branch preserves native int terms.
    assert (EX.first, EX.age, Literal(7)) in actual
    changed.schema.slots["age"].range = "uri"
    record = _instance()
    record.items[0].age = "ex:unexpected"  # type: ignore[assignment]
    record.items[1].age = "ex:other"  # type: ignore[assignment]
    actual = rdflib_dumper.as_rdf_graph(record, changed)
    report = check_rdf_emission(
        prepare_source(actual),
        baseline.declarations,
        [RdfModelUsage("Catalogue", EX.collection)],
    )
    assert "emission.value_mismatch" in {issue.code for issue in report.issues}
    mapping = SchemaView(SCHEMA)
    mapping.class_induced_slots("Item")
    mapping.schema.slots["age"].slot_uri = "ex:changedAge"
    contract = RdfField(
        str(EX.age),
        (ValueDeclaration("literal", datatype=str(XSD.integer)),),
        output_contract=True,
    )
    result = check_instance(
        _instance(),
        view=mapping,
        vocabulary=_vocabulary(),
        options={},
        class_name="Catalogue",
        field_contracts={"Item": {"age": contract}},
    )
    assert "adapter.predicate_contract_conflict" in {
        issue.code for issue in result.report.issues
    }
    assert "vocabulary.term_missing" in {
        issue.code
        for issue in check_instance(
            _instance(),
            view=mapping,
            vocabulary=_vocabulary(),
            options={},
            class_name="Catalogue",
        ).report.issues
    }


def test_unsupported_output_forms_and_budget_cannot_claim_qualification() -> None:
    unsupported = SchemaView(SCHEMA)
    unsupported.schema.slots["items"].list_elements_ordered = True
    extraction = model_declarations(unsupported, serializer="rdflib")
    assert not extraction.report.verified and not extraction.report.complete
    assert "adapter.output_unverified" in {
        issue.code for issue in extraction.report.issues
    }
    bounded = check_instance(
        _instance(),
        view=SchemaView(SCHEMA),
        vocabulary=_vocabulary(),
        options={},
        class_name="Catalogue",
        limits=Limits(max_work=1),
    )
    assert not bounded.report.complete and bounded.report.outcome == "exhausted"
    prefixes = SchemaView(SCHEMA)
    prefixes.schema.default_curi_maps = ["https://offline.invalid/prefix-map"]
    with pytest.raises(ResourceError, match="explicit namespace prefixes"):
        model_declarations(prefixes, serializer="rdflib")
    unresolved = SchemaView(SCHEMA + "\nimports: [https://offline.invalid/missing]\n")
    failed = check_instance(
        _instance(),
        view=unresolved,
        vocabulary=_vocabulary(),
        options={},
        class_name="Catalogue",
    )
    assert failed.source is None and not failed.report.complete
    assert "does not retrieve imports" in failed.report.issues[0].message


def test_reused_profile_captures_schema_options_and_separates_concurrent_outputs() -> (
    None
):
    view = SchemaView(SCHEMA)
    serializer = RdfSerializer(view, options={"prefix_map": {"record": str(EX)}})
    view.schema.slots["age"].slot_uri = "ex:callerChanged"

    def check(index: int) -> RdfInstanceCheck:
        """All output graphs are fresh even though schema/extraction are reused."""
        instance = _instance()
        instance.id = f"record:collection{index}"
        return serializer.check(
            instance, class_name="Catalogue", vocabulary=_vocabulary()
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = tuple(executor.map(check, range(8)))
    assert all(result.report.verified for result in results)
    for index, result in enumerate(results):
        assert result.source is not None
        graph = result.source.graph()
        assert (EX[f"collection{index}"], RDF.type, EX.Catalogue) in graph
        graph.remove((None, None, None))
        assert len(result.source.graph()) > 0
