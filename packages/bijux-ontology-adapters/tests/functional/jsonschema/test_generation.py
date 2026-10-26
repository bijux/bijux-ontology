"""Generated schemas qualify explicit output intent without claiming RDF equivalence."""

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from typing import cast, overload

from rdflib import Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    OutputRequirement,
    ValueDeclaration,
)
from bijux_ontology.declarations.value_constraints import ValueConstraints
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.models.emission.serialization import check_instance
from bijux_ontology.models.emission.types import ModelUsage
from bijux_ontology_adapters.jsonschema import (
    check_instance as check_schema_instance,
)
from bijux_ontology_adapters.jsonschema import (
    model_declarations,
)
from bijux_ontology_adapters.jsonschema.generation import (
    JsonRepresentation,
    compile_output_schema,
)

EX = Namespace("https://example.org/output#")
URI = "https://example.org/contracts/output"
TEXT = ValueDeclaration("literal", datatype=str(XSD.string))
REQUIRED = OutputRequirement(require_property=True, allow_empty=True)


def _field(name: str, *values: ValueDeclaration, **options: object) -> FieldDeclaration:
    """Keep independently authored RDF meaning explicit in each small fixture."""
    return FieldDeclaration(
        name,
        str(EX[name]),
        values or (TEXT,),
        output=REQUIRED,
        **options,  # type: ignore[arg-type]
    )


def _vocabulary() -> Graph:
    """Declare actual class, property and range semantics for the serializer checks."""
    graph = Graph()
    graph.add((EX.Record, RDF.type, OWL.Class))
    for name, datatype in (("title", XSD.string), ("count", XSD.integer)):
        graph.add((EX[name], RDF.type, OWL.DatatypeProperty))
        graph.add((EX[name], RDFS.range, datatype))
    return graph


def test_real_output_options_once_missing_numeric_and_union_checks() -> None:
    """A real serializer's owned output is checked by both semantic and JSON APIs."""
    from pydantic import BaseModel, Field

    class Record(BaseModel):
        kind: str = Field(default=str(EX.Record), serialization_alias="@type")
        title: str = Field(serialization_alias="title")
        count: int
        absent: str | None = None

    integer = ValueDeclaration(
        "literal",
        datatype=str(XSD.integer),
        constraints=ValueConstraints(minimum=Decimal(2), maximum=Decimal(9)),
    )
    model = ModelDeclaration(
        "record",
        (str(EX.Record),),
        (
            _field("title", emission="guaranteed"),
            _field("count", integer),
        ),
    )
    generated = compile_output_schema((model,), ("record",), schema_iri=URI)
    assert generated.schema is not None and not generated.report.verified
    calls: list[dict[str, object]] = []

    def serialize(value: Record, **options: object) -> object:
        calls.append(dict(options))
        return value.model_dump(**options)  # type: ignore[arg-type]

    options = {"mode": "json", "by_alias": True, "exclude_none": True}
    checked = check_instance(
        Record(title="Observed", count=3),
        serialize,
        (model,),
        (ModelUsage("record"),),
        vocabulary=_vocabulary(),
        options=options,
        context={"title": str(EX.title), "count": str(EX["count"])},
    )
    assert calls == [options]
    assert checked.report.verified, checked.report.to_dict()
    assert checked.output == {"@type": str(EX.Record), "title": "Observed", "count": 3}
    assert generated.validate(checked.output).verified
    for output in ({"count": 3}, {"title": "Observed", "count": 1}):
        assert generated.validate(output).outcome == "disagreement"
    assert "subject identity/types" in {
        item.obligation
        for item in generated.translations
        if item.status == "unsupported"
    }
    assert "RDF numeric identity" in {
        item.obligation for item in generated.translations if item.status == "weakened"
    }
    assert generated.validate({"title": "Observed", "count": 3.0}).verified
    union = ModelDeclaration(
        "union",
        (),
        (
            _field(
                "value", TEXT, ValueDeclaration("literal", datatype=str(XSD.boolean))
            ),
        ),
    )
    selected = compile_output_schema((union,), ("union",), schema_iri=URI)
    assert selected.validate({"value": "yes"}).verified
    assert selected.validate({"value": False}).verified
    assert selected.validate({"value": 2}).outcome == "disagreement"


def test_linked_annotations_and_existing_importer_roundtrip() -> None:
    """Owned local definitions and sidecars round-trip without changing the importer."""
    child = ModelDeclaration("label", (str(EX.Label),), (_field("text"),))
    parent = ModelDeclaration(
        "record",
        (str(EX.Record),),
        (
            _field("title", emission="guaranteed"),
            _field("label", ValueDeclaration("object", model="label")),
        ),
    )
    result = compile_output_schema(
        (parent, child), ("record",), schema_iri=URI, labels=((str(EX.title), "Title"),)
    )
    assert result.validate({"title": "one", "label": {"text": "nested"}}).verified
    assert not result.validate({"title": "one", "label": {}}).verified
    extracted = model_declarations(result.schema, result.mappings)
    assert extracted.report.verified, extracted.report.to_dict()
    by_name = {model.identifier: model for model in extracted.declarations}
    for original in (parent, child):
        assert by_name[original.identifier].fields == tuple(
            replace(field, key=field.key or field.name) for field in original.fields
        )
    annotation = cast(dict[str, object], result.annotations)
    assert annotation["schema"] == URI
    assert cast(dict[str, str], annotation["labels"])[str(EX.title)] == "Title"
    exported = cast(dict[str, object], result.schema)
    exported.clear()
    assert result.validate({"title": "one", "label": {"text": "nested"}}).verified
    assert "x-rdf" not in cast(dict[str, object], result.schema)

    @dataclass
    class Label:
        text: str
        kind: str = str(EX.Label)

    @dataclass
    class Record:
        title: str
        label: Label
        kind: str = str(EX.Record)

    calls: list[dict[str, object]] = []

    def serialize(value: Record, **options: object) -> object:
        calls.append(dict(options))
        return asdict(
            value,
            dict_factory=lambda pairs: {
                "@type" if key == "kind" else key: item for key, item in pairs
            },
        )

    graph = _vocabulary()
    graph.add((EX.Label, RDF.type, OWL.Class))
    graph.add((EX.label, RDF.type, OWL.ObjectProperty))
    graph.add((EX.text, RDF.type, OWL.DatatypeProperty))
    graph.add((EX.text, RDFS.range, XSD.string))
    checked = check_schema_instance(
        Record("one", Label("nested")),
        serialize,
        result.schema,
        result.mappings,
        vocabulary=graph,
        options={},
        output_schema=True,
        context={
            "title": str(EX.title),
            "label": str(EX.label),
            "text": str(EX.text),
        },
    )
    assert calls == [{}]
    assert checked.report.verified, checked.report.to_dict()
    assert result.validate(checked.output).verified


def test_required_const_discriminators_remain_branch_specific() -> None:
    """Only disjoint actual required constants qualify oneOf instead of anyOf."""
    models = tuple(
        ModelDeclaration(
            name,
            (),
            (
                _field(
                    "kind",
                    replace(
                        TEXT, constraints=ValueConstraints(enumeration=(Literal(name),))
                    ),
                ),
                _field(field),
            ),
        )
        for name, field in (("article", "title"), ("note", "summary"))
    )
    result = compile_output_schema(models, ("article", "note"), schema_iri=URI)
    schema = cast(dict[str, object], result.schema)
    assert "oneOf" in schema and "anyOf" not in schema
    for output in (
        {"kind": "article", "title": "one"},
        {"kind": "note", "summary": "two"},
    ):
        assert result.validate(output).verified
    for output in (
        {"kind": "other", "title": "one"},
        {"kind": "article", "summary": "wrong branch"},
    ):
        assert not result.validate(output).verified
    extracted = model_declarations(schema, result.mappings)
    assert extracted.report.verified, extracted.report.to_dict()
    assert {model.identifier for model in extracted.declarations} == {"article", "note"}
    overlapping = tuple(replace(model, fields=(_field("value"),)) for model in models)
    ambiguous = compile_output_schema(overlapping, ("article", "note"), schema_iri=URI)
    assert "anyOf" in cast(dict[str, object], ambiguous.schema)


def test_rdf_distinct_counts_and_list_positions_have_separate_contracts() -> None:
    """Repeated JSON values do not certify multiple distinct RDF terms."""
    field = replace(
        _field("title"),
        output=OutputRequirement(
            require_property=True, allow_empty=True, minimum_values=2
        ),
    )
    model = ModelDeclaration("record", (), (field,))
    selected = compile_output_schema(
        (model,),
        ("record",),
        schema_iri=URI,
        representations=(JsonRepresentation("record", "title", "array"),),
    )
    duplicate = {"title": ["same", "same"]}
    assert selected.validate(duplicate).verified
    assert not selected.report.verified
    assert any(item.status == "strengthened" for item in selected.translations)
    assert any(
        item.obligation == "distinct RDF values" and item.status == "unsupported"
        for item in selected.translations
    )
    checked = check_instance(
        duplicate,
        lambda value: value,
        (model,),
        (ModelUsage("record"),),
        vocabulary=_vocabulary(),
        options={},
        context={"title": str(EX.title)},
    )
    assert "emission.value_count_mismatch" in {
        issue.code for issue in checked.report.issues
    }
    listed_field = replace(
        field,
        values=(replace(TEXT, container="@list"),),
        output=OutputRequirement(allow_empty=False, minimum_list_members=2),
    )
    listed = compile_output_schema(
        (replace(model, fields=(listed_field,)),),
        ("record",),
        schema_iri=URI,
        representations=(JsonRepresentation("record", "title", "list"),),
    )
    assert any(
        item.obligation == "list member positions" and item.status == "preserved"
        for item in listed.translations
    )
    assert not listed.report.verified
    assert listed.validate({"title": {"@list": ["same", "same"]}}).verified
    assert not listed.validate({"title": {"@list": ["same"]}}).verified
    assert not listed.validate({}).verified
    empty_list = replace(listed_field, output=OutputRequirement(allow_empty=False))
    nil = compile_output_schema(
        (replace(model, fields=(empty_list,)),),
        ("record",),
        schema_iri=URI,
        representations=(JsonRepresentation("record", "title", "list"),),
    )
    assert nil.validate({"title": {"@list": []}}).verified


def test_unknown_conversions_custom_profiles_and_empty_enumeration() -> None:
    """Unsupported obligations remain visible without erasing usable JSON assertions."""
    decimal = replace(
        TEXT,
        datatype=str(XSD.decimal),
        constraints=ValueConstraints(
            minimum=Decimal("0.5"), pattern="^a", iri_namespaces=(str(EX),)
        ),
    )
    model = ModelDeclaration("record", (), (_field("value", decimal),))
    result = compile_output_schema((model,), ("record",), schema_iri=URI)
    assert result.schema is not None and not result.report.verified
    custom = compile_output_schema(
        (model,), ("record",), schema_iri=URI, profile="caller-custom"
    )
    assert not custom.report.verified
    assert any(
        item.obligation == "serializer representation" and item.status == "unsupported"
        for item in custom.translations
    )
    closed_empty = replace(TEXT, constraints=ValueConstraints(enumeration=()))
    empty = compile_output_schema(
        (replace(model, fields=(_field("value", closed_empty),)),),
        ("record",),
        schema_iri=URI,
    )
    assert not empty.validate({"value": "anything"}).verified
    noncanonical = replace(
        TEXT,
        datatype=str(XSD.integer),
        constraints=ValueConstraints(
            enumeration=(Literal("01", datatype=XSD.integer, normalize=False),)
        ),
    )
    lexical = compile_output_schema(
        (replace(model, fields=(_field("value", noncanonical),)),),
        ("record",),
        schema_iri=URI,
    )
    assert not lexical.report.verified
    assert any(
        item.obligation == "enumeration" and item.status == "unsupported"
        for item in lexical.translations
    )


def test_owned_artifacts_offline_resolution_and_finite_metadata() -> None:
    """Tiny limits fail closed, and bounded indexed capture never invokes iterators."""
    model = ModelDeclaration("record", (), (_field("title"),))

    class Indexed(Sequence[ModelDeclaration]):
        def __len__(self) -> int:
            return 1

        @overload
        def __getitem__(self, index: int) -> ModelDeclaration: ...

        @overload
        def __getitem__(self, index: slice) -> Sequence[ModelDeclaration]: ...

        def __getitem__(
            self, index: int | slice
        ) -> ModelDeclaration | Sequence[ModelDeclaration]:
            assert type(index) is int and index == 0
            return model

        def __iter__(self):  # type: ignore[no-untyped-def]
            raise AssertionError("Unbounded iterator must not be consumed")

    result = compile_output_schema(Indexed(), ("record",), schema_iri=URI)
    assert result.schema is not None
    assert result.validate({"title": "value"}).verified
    for limits in (Limits(max_work=1), Limits(max_bytes=100), Limits(max_states=1)):
        exhausted = compile_output_schema(
            (model,), ("record",), schema_iri=URI, limits=limits
        )
        assert exhausted.schema is None and exhausted.report.outcome == "exhausted"
    giant = compile_output_schema(
        (model,),
        ("record",),
        schema_iri="urn:" + "a" * 10000,
        limits=Limits(max_bytes=1000),
    )
    assert giant.schema is None and giant.report.outcome == "exhausted"
    foreign = deepcopy(cast(dict[str, object], result.schema))
    foreign["$ref"] = "https://unavailable.example/schema"
    assert (
        not replace(result, schema_json=__import__("json").dumps(foreign))
        .validate({"title": "value"})
        .verified
    )
    term = Literal("original")
    enum_model = replace(
        model,
        fields=(
            _field(
                "title",
                replace(TEXT, constraints=ValueConstraints(enumeration=(term,))),
            ),
        ),
    )
    owned = compile_output_schema((enum_model,), ("record",), schema_iri=URI)
    term._datatype = XSD.integer
    value = owned.mappings[0].fields[0][1].values[0]
    assert value.constraints is not None
    assert value.constraints.enumeration == (Literal("original"),)
    assert owned.validate({"title": "original"}).verified
    empty = compile_output_schema(
        (ModelDeclaration("empty", ()),), ("empty",), schema_iri=URI
    )
    assert empty.validate({}).verified and not empty.report.verified
    choices = tuple(Literal(chr(65 + index) * 80) for index in range(16))
    bounded_model = replace(
        model,
        fields=(
            _field(
                "title",
                replace(TEXT, constraints=ValueConstraints(enumeration=choices)),
            ),
        ),
    )
    enum_array = compile_output_schema(
        (bounded_model,),
        ("record",),
        schema_iri=URI,
        representations=(JsonRepresentation("record", "title", "array"),),
    )
    assert enum_array.schema is not None
    assert (
        enum_array.validate(
            {"title": ["unmatched" * 10] * 100}, limits=Limits(max_work=20000)
        ).outcome
        == "exhausted"
    )
    operation = Budget(Limits(max_work=1))
    assert result.validate({"title": "value"}, budget=operation).outcome == "exhausted"
