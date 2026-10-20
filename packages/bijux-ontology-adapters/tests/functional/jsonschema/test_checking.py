"""Real JSON Schema output contracts preserve explicit RDF branch intent."""

from copy import deepcopy
from typing import Any, NotRequired, TypedDict, cast

import pytest
from pydantic import BaseModel, Field
from rdflib import Graph, Namespace
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.declarations.types import OutputRequirement, ValueDeclaration
from bijux_ontology.execution.limits import Limits
from bijux_ontology.models.emission.types import ModelUsage
from bijux_ontology_adapters.contracts import RdfField
from bijux_ontology_adapters.jsonschema import (
    SchemaModel,
    check_instance,
    model_declarations,
)


class _CheckOptions(TypedDict):
    vocabulary: Graph
    options: dict[str, object]
    context: object
    resources: NotRequired[dict[str, object]]


EX = Namespace("https://example.org/schema#")
URI = "https://example.org/contracts/catalogue"
DIALECT = "https://json-schema.org/draft/2020-12/schema"
CONTEXT = {"label": str(EX.label), "count": str(EX["count"]), "items": str(EX["items"])}
LABEL = RdfField(
    str(EX.label),
    (ValueDeclaration("literal", datatype=str(XSD.string)),),
    output_contract=True,
)


def _vocabulary() -> Graph:
    """Declare only the RDF semantics independently required by the sidecars."""
    graph = Graph()
    for cls in (EX.Article, EX.Measure, EX.Catalogue):
        graph.add((cls, RDF.type, OWL.Class))
    for predicate, datatype in ((EX.label, XSD.string), (EX["count"], XSD.integer)):
        graph.add((predicate, RDF.type, OWL.DatatypeProperty))
        graph.add((predicate, RDFS.range, datatype))
    graph.add((EX["items"], RDF.type, OWL.ObjectProperty))
    return graph


def _schema() -> dict[str, object]:
    """Select disjoint serializer variants through actual supplied references."""
    return {
        "$schema": DIALECT,
        "$id": URI,
        "oneOf": [{"$ref": "#/$defs/article"}, {"$ref": "#/$defs/measure"}],
        "$defs": {
            "article": {
                "type": "object",
                "properties": {
                    "@type": {"const": str(EX.Article)},
                    "label": {"type": "string"},
                },
                "required": ["@type", "label"],
                "additionalProperties": False,
            },
            "measure": {
                "type": "object",
                "properties": {
                    "@type": {"const": str(EX.Measure)},
                    "count": {"type": "integer"},
                },
                "required": ["@type", "count"],
                "additionalProperties": False,
            },
        },
    }


def _mappings() -> tuple[SchemaModel, ...]:
    """Keep alternative model types and fields independent of input annotations."""
    return (
        SchemaModel(
            URI + "#/$defs/article",
            "article",
            (str(EX.Article),),
            (("label", LABEL),),
            (("label", OutputRequirement(require_property=True)),),
        ),
        SchemaModel(
            URI + "#/$defs/measure",
            "measure",
            (str(EX.Measure),),
            (
                (
                    "count",
                    RdfField(
                        str(EX["count"]),
                        (ValueDeclaration("literal", datatype=str(XSD.integer)),),
                        output_contract=True,
                    ),
                ),
            ),
        ),
    )


def test_actual_serializer_references_and_discriminated_output_are_branch_scoped() -> (
    None
):
    """Actual ordinary JSON serialization runs once with exact caller options."""

    class Article(BaseModel):
        kind: str = Field(default=str(EX.Article), serialization_alias="@type")
        label: str
        absent: str | None = None

    class Measure(BaseModel):
        kind: str = Field(default=str(EX.Measure), serialization_alias="@type")
        count: int

    calls = []

    def serialize(value: BaseModel, **options: object) -> object:
        calls.append(dict(options))
        return value.model_dump(**options)  # type: ignore[arg-type]

    options = {"mode": "json", "by_alias": True, "exclude_none": True}
    for value, expected in (
        (Article(label="One"), "article"),
        (Measure(count=2), "measure"),
    ):
        result = check_instance(
            value,
            serialize,
            _schema(),
            _mappings(),
            vocabulary=_vocabulary(),
            options=options,
            output_schema=True,
            context=CONTEXT,
        )
        assert result.report.verified, result.report.to_dict()
        assert result.output == value.model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
        assert result.document != result.output
        assert "@context" not in cast(dict[str, object], result.output)
        assert cast(dict[str, object], result.document)["@context"] == CONTEXT
        assert calls[-1] == options
        wrong = "measure" if expected == "article" else "article"
        rejected = check_instance(
            value,
            serialize,
            _schema(),
            _mappings(),
            (ModelUsage(wrong),),
            vocabulary=_vocabulary(),
            options=options,
            output_schema=True,
            context=CONTEXT,
        )
        assert rejected.report.outcome == "input_error"
    assert len(calls) == 4
    altered = list(_mappings())
    altered[0] = SchemaModel(
        URI + "#/$defs/article", "article", (str(EX.Measure),), (("label", LABEL),)
    )
    assert not check_instance(
        Article(label="One"),
        serialize,
        _schema(),
        altered,
        vocabulary=_vocabulary(),
        options=options,
        output_schema=True,
        context=CONTEXT,
    ).report.verified


def test_input_required_and_explicit_output_obligations_are_separate() -> None:
    """JSON required/additionalProperties never invent RDF emission or closure."""
    schema = {
        "$schema": DIALECT,
        "$id": URI,
        "type": "object",
        "properties": {"@type": {"type": "string"}, "label": {"type": "string"}},
        "required": ["label"],
        "additionalProperties": False,
    }
    mapping = SchemaModel(URI, "article", (str(EX.Article),), (("label", LABEL),))
    extracted = model_declarations(schema, (mapping,))
    assert extracted.report.verified
    field = extracted.declarations[0].fields[0]
    assert field.emission == "unknown" and field.output is None
    output = {"@type": str(EX.Article)}
    kwargs: _CheckOptions = {
        "vocabulary": _vocabulary(),
        "options": {},
        "context": CONTEXT,
    }
    assert check_instance(
        output, lambda value: value, schema, (mapping,), **kwargs
    ).report.verified
    output_checked = check_instance(
        output, lambda value: value, schema, (mapping,), output_schema=True, **kwargs
    )
    assert "adapter.schema_mismatch" in {
        issue.code for issue in output_checked.report.issues
    }
    required = SchemaModel(
        URI,
        "article",
        (str(EX.Article),),
        (("label", LABEL),),
        (("label", OutputRequirement(require_property=True)),),
    )
    assert not check_instance(
        output, lambda value: value, schema, (required,), **kwargs
    ).report.verified
    extra = {"@type": str(EX.Article), "label": "One", "count": 2}
    extra_checked = check_instance(
        extra, lambda value: value, schema, (mapping,), **kwargs
    )
    assert extra_checked.report.outcome == "unverified"
    assert "emission.predicate_undeclared" not in {
        issue.code for issue in extra_checked.report.issues
    }
    assert not check_instance(
        extra, lambda value: value, schema, (mapping,), output_schema=True, **kwargs
    ).report.verified


def test_supplied_external_references_and_nested_variant_models_are_reusable() -> None:
    """Referenced alternatives retain their own child guarantees and source paths."""
    resource = _schema()
    root = {
        "$schema": DIALECT,
        "$id": URI + "/root",
        "type": "object",
        "properties": {
            "@type": {"const": str(EX.Catalogue)},
            "items": {"type": "array", "items": {"$ref": URI}},
        },
        "required": ["items"],
    }
    parent = SchemaModel(
        URI + "/root",
        "catalogue",
        (str(EX.Catalogue),),
        (
            (
                "items",
                RdfField(
                    str(EX["items"]),
                    (
                        ValueDeclaration("object", model="article"),
                        ValueDeclaration("object", model="measure"),
                    ),
                    output_contract=True,
                ),
            ),
        ),
    )
    output: dict[str, object] = {
        "@type": str(EX.Catalogue),
        "items": [
            {"@type": str(EX.Article), "label": "One"},
            {"@type": str(EX.Measure), "count": 2},
        ],
    }
    kwargs: _CheckOptions = {
        "vocabulary": _vocabulary(),
        "options": {},
        "context": CONTEXT,
        "resources": {URI: resource},
    }
    result = check_instance(
        output,
        lambda value: value,
        root,
        (parent, *_mappings()),
        output_schema=True,
        **kwargs,
    )
    assert result.report.verified, result.report.to_dict()
    childless = deepcopy(output)
    cast(list[dict[str, object]], childless["items"])[0].pop("label")
    assert not check_instance(
        childless,
        lambda value: value,
        root,
        (parent, *_mappings()),
        output_schema=True,
        **kwargs,
    ).report.verified
    mutated = result.output
    cast(dict[str, Any], mutated)["items"].clear()
    assert result.output == output


def test_unsupported_or_unmapped_scope_and_tiny_budgets_cannot_qualify_output() -> None:
    """Unknown schema semantics and reference retrieval never turn into acceptance."""
    base = {
        "$schema": DIALECT,
        "$id": URI,
        "type": "object",
        "properties": {"label": {"type": "string"}},
    }
    mapping = SchemaModel(URI, "article", (str(EX.Article),), (("label", LABEL),))
    extras: tuple[dict[str, object], ...] = (
        {"$dynamicRef": "#dynamic"},
        {"allOf": [{}]},
        {"patternProperties": {".*": {}}},
        {"$ref": "https://invalid.example/unavailable"},
    )
    for extra in extras:
        result = model_declarations({**base, **extra}, (mapping,))
        assert not result.report.verified
    assert not model_declarations(
        base, (SchemaModel(URI, "article", (str(EX.Article),)),)
    ).report.verified
    recursive = {**base, "properties": {"label": {"$ref": URI}}}
    assert not model_declarations(recursive, (mapping,)).report.verified
    for maximum in (1, 20, 200):
        result = model_declarations(base, (mapping,), limits=Limits(max_work=maximum))
        assert not result.report.verified and result.report.outcome == "exhausted"
    many = {
        **base,
        "properties": {
            "label": {"type": "string"},
            "missing": {"type": "string"},
            "other": {"type": "string"},
        },
    }
    result = model_declarations(many, (mapping,), limits=Limits(max_issues=1))
    assert not result.report.verified
    assert result.report.issues[0].code == "adapter.mapping_unverified"
    assert result.report.issues[-1].kind == "exhausted"


def test_mapping_conflicts_and_custom_serializer_changes_retain_raw_truth() -> None:
    """Neither malformed sidecars nor a serializer's actual changed kind is hidden."""
    schema = _schema()
    with pytest.raises(ValueError):
        SchemaModel(
            URI, "article", (str(EX.Article),), (("label", LABEL), ("label", LABEL))
        )
    extraction = model_declarations(schema, (*_mappings(), _mappings()[0]))
    assert extraction.report.outcome == "input_error"
    output = {"@type": str(EX.Article), "label": 7}
    checked = check_instance(
        output,
        lambda value: value,
        schema,
        _mappings(),
        vocabulary=_vocabulary(),
        options={},
        context=CONTEXT,
    )
    assert not checked.report.verified
    assert "emission.value_mismatch" in {issue.code for issue in checked.report.issues}


def test_callback_exception_metadata_is_not_retained_without_bounds() -> None:
    """Even a caller-defined exception class name cannot escape report quotas."""
    exception = type("x" * 100_000, (Exception,), {})

    def serialize(value: object) -> object:
        raise exception()

    result = check_instance(
        None,
        serialize,
        _schema(),
        _mappings(),
        vocabulary=_vocabulary(),
        options={},
        limits=Limits(max_bytes=1000),
    )
    assert result.report.outcome == "input_error"
    assert all(len(issue.message) < 100 for issue in result.report.issues)
