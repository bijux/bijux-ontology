"""Actual serialisation is checked separately from hypothetical field types."""

from typing import Annotated

import pytest
from pydantic import BaseModel, field_serializer
from rdflib.namespace import XSD

from bijux_ontology.declarations import FieldDeclaration as Field
from bijux_ontology.declarations import ModelDeclaration as Model
from bijux_ontology.declarations import ValueDeclaration as Value
from bijux_ontology.interfaces.adapters.contracts import RdfField
from bijux_ontology.interfaces.adapters.pydantic import model_declarations
from bijux_ontology.limits import Limits
from bijux_ontology.models import ModelUsage, check_emission

EX = "https://example.org/"


def test_real_serializer_output_is_compared_with_its_explicit_contract() -> None:
    class Study(BaseModel):
        title: Annotated[
            str,
            RdfField(
                EX + "title",
                (Value("literal", str(XSD.string)),),
                emission="guaranteed",
                output_contract=True,
            ),
        ]

        @field_serializer("title")
        def title_length(self, value: str) -> int:
            return len(value)

    extracted = model_declarations({Study: (EX + "Study",)})
    document = {
        "@context": {"@vocab": EX},
        "@type": "Study",
        **Study(title="Example").model_dump(),
    }
    before = document.copy()
    report = check_emission(
        document,
        extracted.declarations,
        [ModelUsage(extracted.declarations[0].identifier)],
    )
    assert [issue.code for issue in report.issues] == ["emission.value_mismatch"]
    assert document == before
    document["title"] = "Example"
    assert check_emission(
        document,
        extracted.declarations,
        [ModelUsage(extracted.declarations[0].identifier)],
    ).verified


def test_each_mixed_alternative_value_and_list_item_is_checked() -> None:
    branch = (
        Value("literal", str(XSD.string)),
        Value("iri"),
    )
    model = Model(
        "study",
        (EX + "Study",),
        (
            Field("related", EX + "related", branch),
            Field(
                "ordered",
                EX + "ordered",
                (Value("literal", str(XSD.string), container="@list"),),
            ),
        ),
    )
    document: dict[str, object] = {
        "@context": {"@vocab": EX},
        "@type": "Study",
        "related": ["text", {"@id": EX + "other"}],
        "ordered": {"@list": ["a", "b"]},
    }
    assert check_emission(document, [model], [ModelUsage("study")]).verified
    document["related"] = ["text", {"@id": EX + "other"}, 5]
    report = check_emission(document, [model], [ModelUsage("study")])
    assert [(issue.code, issue.location) for issue in report.issues] == [
        ("emission.value_mismatch", "/related[2]")
    ]
    document["ordered"] = {"@list": []}
    report = check_emission(document, [model], [ModelUsage("study")])
    assert len(report.issues) == 1


def test_nested_bindings_reversal_and_guaranteed_presence_remain_explicit() -> None:
    parent = Model(
        "parent",
        (EX + "Parent",),
        (
            Field(
                "child", EX + "parent", (Value("object", model="child"),), reverse=True
            ),
            Field("label", EX + "label", (Value("literal"),), emission="guaranteed"),
        ),
    )
    child = Model(
        "child", (EX + "Child",), (Field("name", EX + "name", (Value("literal"),)),)
    )
    document = {
        "@context": {
            "@vocab": EX,
            "kind": "@type",
            "child": {"@reverse": EX + "parent"},
        },
        "kind": "Parent",
        "label": "Parent",
        "child": {"kind": "Child", "name": "Child"},
    }
    report = check_emission(document, [parent, child], [ModelUsage("parent")])
    assert [issue.code for issue in report.issues] == [
        "emission.nested_scope_unverified"
    ]
    bindings = [ModelUsage("parent"), ModelUsage("child", ("child",))]
    report = check_emission(document, [parent, child], bindings)
    assert report.verified, report.to_dict()
    document.pop("label")
    report = check_emission(document, [parent, child], bindings)
    assert [issue.code for issue in report.issues] == [
        "emission.required_occurrence_missing"
    ]


def test_invalid_references_and_exhaustion_cannot_become_success() -> None:
    model = Model(
        "root", (), (Field("value", EX + "value", (Value(model="missing"),)),)
    )
    document = {"@context": {"@vocab": EX}, "value": "a"}
    report = check_emission(document, [model], [ModelUsage("root")])
    assert report.exit_code() == 2
    assert any(issue.code == "input.model_reference_missing" for issue in report.issues)
    limited = check_emission(
        document, [model], [ModelUsage("root")], limits=Limits(max_work=1)
    )
    assert not limited.complete
    assert limited.exit_code() == 2
    assert limited.issues[-1].kind == "exhausted"


def test_mapping_differences_and_ambiguous_root_scope_remain_visible() -> None:
    model = Model(
        "root",
        (EX + "Root",),
        (Field("name", EX + "name", (Value("literal"),)),),
    )
    document = {
        "@context": {"@vocab": EX, "name": EX + "wrong"},
        "@type": "Root",
        "name": "Text",
    }
    report = check_emission(document, [model], [ModelUsage("root")])
    assert "context.mapping_mismatch" in {issue.code for issue in report.issues}
    multiple = {
        "@context": {"@vocab": EX},
        "@graph": [{"@type": "Root", "name": "A"}, {"@type": "Root", "name": "B"}],
    }
    report = check_emission(
        multiple, [Model("root", (EX + "Root",))], [ModelUsage("root")]
    )
    assert [issue.code for issue in report.issues] == ["emission.node_unverified"]
    literal = {"@context": {"@vocab": EX}, "value": {"@value": "text"}}
    report = check_emission(
        literal, [Model("root", ())], [ModelUsage("root", ("value",))]
    )
    assert [issue.code for issue in report.issues] == ["emission.node_kind_mismatch"]


@pytest.mark.parametrize(
    ("datatype", "value", "outcome"),
    [
        (str(XSD.decimal), "1.25", "agreement"),
        (str(XSD.decimal), "1e3", "disagreement"),
        (str(XSD.integer), 1.0, "agreement"),
        (str(XSD.double), 10**21, "agreement"),
        (str(XSD.boolean), True, "agreement"),
        (EX + "CustomDatatype", "text", "unverified"),
        (str(XSD.token), " a\t b ", "unverified"),
        ("@json", {"name": "opaque"}, "unverified"),
    ],
)
def test_actual_literal_values_use_preserved_or_processor_native_lexical_forms(
    datatype: str,
    value: object,
    outcome: str,
) -> None:
    from rdflib.namespace import RDF

    model_datatype = str(RDF.JSON) if datatype == "@json" else datatype
    model = Model(
        "record",
        (EX + "Record",),
        (Field("value", EX + "value", (Value("literal", model_datatype),)),),
    )
    document = {
        "@context": {"@vocab": EX, "value": {"@id": EX + "value", "@type": datatype}},
        "@type": "Record",
        "value": value,
    }
    report = check_emission(document, [model], [ModelUsage("record")])
    assert report.outcome == outcome, report.to_dict()
    if outcome == "disagreement":
        assert [(issue.code, issue.location) for issue in report.issues] == [
            ("literal.lexical_invalid", "/value[0]")
        ]
    elif outcome == "unverified":
        assert not report.verified and report.exit_code(strict=True) == 1


def test_list_literal_validation_is_per_observation_across_alternative_branches() -> (
    None
):
    model = Model(
        "record",
        (EX + "Record",),
        (
            Field(
                "values",
                EX + "values",
                (
                    Value("literal", str(XSD.decimal), container="@list"),
                    Value("literal", str(XSD.string), container="@list"),
                ),
            ),
        ),
    )
    document = {
        "@context": {
            "@vocab": EX,
            "values": {
                "@id": EX + "values",
                "@type": str(XSD.decimal),
                "@container": "@list",
            },
        },
        "@type": "Record",
        "values": ["1.25", "1e3"],
    }
    report = check_emission(document, [model], [ModelUsage("record")])
    assert [(issue.code, issue.location) for issue in report.issues] == [
        ("literal.lexical_invalid", "/values[1]")
    ]
    assert report.checked == 5
