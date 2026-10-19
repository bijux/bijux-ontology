"""Actual serialisation is checked separately from hypothetical field types."""

from copy import deepcopy
from typing import Annotated

import pytest
from pydantic import BaseModel, field_serializer
from rdflib.namespace import XSD

from bijux_ontology.declarations import FieldDeclaration as Field
from bijux_ontology.declarations import ModelDeclaration as Model
from bijux_ontology.declarations import ValueDeclaration as Value
from bijux_ontology.errors import InputError
from bijux_ontology.interfaces.adapters.contracts import RdfField
from bijux_ontology.interfaces.adapters.pydantic import model_declarations
from bijux_ontology.jsonld.observations import SourcePath, at_path
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


@pytest.mark.parametrize("container", ["@list", "@set"])
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("aliases", [False, True])
def test_actual_child_bindings_cross_containers_without_binding_the_wrapper(
    container: str, explicit: bool, aliases: bool
) -> None:
    parent = Model(
        "parent",
        (EX + "Parent",),
        (
            Field(
                "children",
                EX + "children",
                (Value("object", model="child", container=container),),
            ),
        ),
    )
    child = Model(
        "child",
        (EX + "Child",),
        (Field("label", EX + "label", (Value("literal"),), emission="guaranteed"),),
    )
    keyword = "entries" if aliases else container
    context: dict[str, object] = {
        "@vocab": EX,
        "kind": "@type",
        "identifier": "@id",
        "entries": container,
    }
    if not explicit:
        context["children"] = {"@id": EX + "children", "@container": container}
    child_node = {
        "identifier" if aliases else "@id": EX + "child",
        "kind" if aliases else "@type": "Child",
        "label": "Child",
    }
    document = {
        "@context": context,
        "@type": "Parent",
        "children": {keyword: [child_node]} if explicit else [child_node],
    }
    child_path = ("children", keyword, 0) if explicit else ("children", 0)
    bindings = [ModelUsage("parent"), ModelUsage("child", child_path)]
    original = deepcopy(document)
    assert check_emission(document, [parent, child], bindings).verified
    assert document == original
    omitted = check_emission(document, [parent, child], bindings[:1])
    assert "emission.nested_scope_unverified" in {
        issue.code for issue in omitted.issues
    }
    if explicit:
        wrapper = check_emission(
            document,
            [parent, child],
            [ModelUsage("parent"), ModelUsage("child", ("children",))],
        )
        assert not wrapper.verified
    del child_node["label"]
    missing = check_emission(document, [parent, child], bindings)
    assert any(
        issue.code == "emission.required_occurrence_missing"
        and issue.location == "/" + "/".join(map(str, (*child_path, "label")))
        for issue in missing.issues
    )


@pytest.mark.parametrize("placement", ["root", "graph", "included", "array"])
def test_selected_subject_uses_its_own_types_instead_of_child_value_types(
    placement: str,
) -> None:
    parent = {
        "@id": EX + "parent",
        "kind": "Parent",
        "child": {"@id": EX + "child", "kind": "Child"},
    }
    context = {"@vocab": EX, "kind": "@type"}
    document: object
    path: SourcePath
    if placement == "root":
        document, path = {"@context": context, **parent}, ()
    elif placement == "graph":
        document, path = {"@context": context, "@graph": [parent]}, ("@graph", 0)
    elif placement == "included":
        document, path = (
            {"@context": context, "@id": EX + "other", "@included": [parent]},
            ("@included", 0),
        )
    else:
        document, path = [{"@context": context, **parent}], (0,)
    model = Model(
        "parent",
        (EX + "Parent",),
        (Field("child", EX + "child", (Value("object", types=(EX + "Child",)),)),),
    )
    result = check_emission(document, [model], [ModelUsage("parent", path)])
    assert result.verified, result.to_dict()
    selected = at_path(document, path)
    assert isinstance(selected, dict)
    selected["kind"] = "Child"
    incorrect = check_emission(document, [model], [ModelUsage("parent", path)])
    assert any(
        issue.code == "emission.types_mismatch" and issue.actual == EX + "Child"
        for issue in incorrect.issues
    )


def test_nested_model_choices_require_actual_selected_bindings() -> None:
    document = {
        "@context": {"@vocab": EX},
        "@type": "Parent",
        "child": {"@type": "Child"},
    }
    models = [
        Model(
            "parent",
            (EX + "Parent",),
            (
                Field(
                    "child",
                    EX + "child",
                    (Value("object", model="first"), Value("object", model="second")),
                ),
            ),
        ),
        Model("first", (EX + "Child",)),
        Model("second", (EX + "Child",)),
    ]
    missing = check_emission(document, models, [ModelUsage("parent")])
    assert missing.outcome == "unverified"
    assert check_emission(
        document, models, [ModelUsage("parent"), ModelUsage("second", ("child",))]
    ).verified
    document["other"] = {"@type": "Child"}
    wrong_scope = check_emission(
        document, models, [ModelUsage("parent"), ModelUsage("second", ("other",))]
    )
    assert wrong_scope.outcome == "unverified"


def test_expected_subject_uses_effective_base_and_rejects_sibling_identity() -> None:
    from bijux_ontology.declarations.types import SubjectIdentity

    model = Model("record", (EX + "Record",), subject=SubjectIdentity(EX + "records/a"))
    document = {"@context": {"@vocab": EX}, "@id": "a", "@type": "Record"}
    assert check_emission(
        document, [model], [ModelUsage("record")], base=EX + "records/"
    ).verified
    for base in (EX + "other/", EX + "records/b/"):
        report = check_emission(document, [model], [ModelUsage("record")], base=base)
        assert "emission.subject_mismatch" in {issue.code for issue in report.issues}
    document["@id"] = "b"
    assert not check_emission(
        document, [model], [ModelUsage("record")], base=EX + "records/"
    ).verified
    with pytest.raises(InputError, match="conflict"):
        ModelUsage(
            "record", reference=EX + "a", subject=SubjectIdentity(EX + "b"), graph=None
        )
    with pytest.raises(InputError, match="explicit graph"):
        ModelUsage("record", reference=EX + "a")


@pytest.mark.parametrize("identifier", [None, "_:first", "_:renamed"])
def test_anonymous_identity_is_allowed_only_by_explicit_policy(
    identifier: str | None,
) -> None:
    from bijux_ontology.declarations.types import SubjectIdentity

    document = {"@context": {"@vocab": EX}, "@type": "Record"}
    if identifier is not None:
        document["@id"] = identifier
    model = Model("record", (EX + "Record",))
    assert check_emission(document, [model], [ModelUsage("record")]).verified
    report = check_emission(
        document,
        [model],
        [ModelUsage("record", subject=SubjectIdentity(allow_blank=False))],
    )
    assert [issue.code for issue in report.issues] == ["emission.subject_mismatch"]


@pytest.mark.parametrize("layout", ["inline", "included", "flat"])
def test_local_linked_descriptions_join_with_explicit_reference_binding(
    layout: str,
) -> None:
    child = {"@id": EX + "child", "@type": "Child", "label": "Name"}
    parent = {"@id": EX + "parent", "@type": "Parent", "child": {"@id": EX + "child"}}
    context = {"@vocab": EX}
    parent_path: tuple[str | int, ...] = ()
    document: object
    if layout == "inline":
        parent["child"] = child
        document = {"@context": context, **parent}
    elif layout == "included":
        document = {"@context": context, **parent, "@included": [child]}
    else:
        document = {"@context": context, "@graph": [parent, child]}
        parent_path = ("@graph", 0)
    models = [
        Model(
            "parent",
            (EX + "Parent",),
            (
                Field(
                    "child",
                    EX + "child",
                    (Value("object", model="child"),),
                    emission="guaranteed",
                ),
            ),
        ),
        Model(
            "child",
            (EX + "Child",),
            (Field("label", EX + "label", (Value("literal"),), emission="guaranteed"),),
        ),
    ]
    usages = [
        ModelUsage("parent", parent_path),
        ModelUsage("child", reference=EX + "child", graph=None),
    ]
    before = deepcopy(document)
    assert check_emission(document, models, usages).verified
    assert document == before
    child["@type"] = "Other"
    report = check_emission(document, models, usages)
    assert "emission.value_mismatch" in {issue.code for issue in report.issues}


def test_reference_types_never_leak_from_other_named_graphs() -> None:
    document = {
        "@context": {"@vocab": EX},
        "@id": EX + "parent",
        "@type": "Parent",
        "child": {"@id": EX + "child"},
        "@included": [
            {
                "@id": EX + "elsewhere",
                "@graph": [{"@id": EX + "child", "@type": "Child"}],
            }
        ],
    }
    models = [
        Model(
            "parent",
            (EX + "Parent",),
            (Field("child", EX + "child", (Value("object", types=(EX + "Child",)),)),),
        ),
        Model("child", (EX + "Child",)),
    ]
    report = check_emission(
        document,
        models,
        [ModelUsage("parent"), ModelUsage("child", reference=EX + "child", graph=None)],
    )
    assert {issue.code for issue in report.issues} == {
        "emission.value_unverified",
        "emission.reference_unverified",
    }
    assert check_emission(
        document,
        models,
        [ModelUsage("child", reference=EX + "child", graph=EX + "elsewhere")],
    ).verified
    closed = check_emission(
        document,
        models,
        [ModelUsage("child", reference=EX + "child", graph=None, closed=True)],
    )
    assert [issue.code for issue in closed.issues] == ["emission.reference_missing"]


def test_entity_join_does_not_discharge_source_object_field_obligations() -> None:
    model = Model(
        "record",
        (EX + "Record",),
        (Field("label", EX + "label", (Value("literal"),), emission="guaranteed"),),
    )
    document = {
        "@context": {"@vocab": EX},
        "@graph": [
            {"@id": EX + "record", "@type": "Record"},
            {"@id": EX + "record", "label": "Name"},
        ],
    }
    source = check_emission(document, [model], [ModelUsage("record", ("@graph", 0))])
    assert "emission.required_occurrence_missing" in {
        issue.code for issue in source.issues
    }
    assert check_emission(
        document, [model], [ModelUsage("record", ("@graph", 0), entity=True)]
    ).verified
    assert check_emission(
        document, [model], [ModelUsage("record", reference=EX + "record", graph=None)]
    ).verified
    second = at_path(document, ("@graph", 1))
    assert isinstance(second, dict)
    second["@id"] = EX + "sibling"
    report = check_emission(
        document, [model], [ModelUsage("record", ("@graph", 0), entity=True)]
    )
    assert "emission.required_entity_fact_missing" in {
        issue.code for issue in report.issues
    }


def test_graph_scoped_inventory_keeps_anonymous_subjects_and_graphs_distinct() -> None:
    from bijux_ontology.jsonld.checking import prepare_usages

    document = {
        "@context": {"@vocab": EX},
        "items": [
            {"@type": "Record", "label": "one"},
            {"@type": "Record", "label": "two"},
        ],
        "@included": [
            {"@graph": [{"@id": EX + "named", "@type": "Record"}]},
            {"@graph": [{"@id": EX + "named", "@type": "Other"}]},
        ],
    }
    prepared = prepare_usages(document)
    records = [
        entity for entity in prepared.entities.entities if EX + "Record" in entity.types
    ]
    assert len(records) == 3
    anonymous = [entity for entity in records if entity.key.identifier is None]
    assert len(anonymous) == 2 and anonymous[0].key != anonymous[1].key
    named = [
        entity
        for entity in prepared.entities.entities
        if entity.key.identifier == EX + "named"
    ]
    assert len(named) == 2 and named[0].key != named[1].key
    model = Model(
        "record",
        (EX + "Record",),
        (Field("label", EX + "label", (Value("literal"),), emission="guaranteed"),),
    )
    assert check_emission(
        document,
        [model],
        [
            ModelUsage("record", ("items", 0), entity=True),
            ModelUsage("record", ("items", 1), entity=True),
        ],
    ).verified


def test_repeated_references_resolve_one_entity_without_source_occurrence_fiction() -> (
    None
):
    models = [
        Model(
            "parent",
            (EX + "Parent",),
            (Field("child", EX + "child", (Value("object", model="child"),)),),
        ),
        Model("child", (EX + "Child",)),
    ]
    document = {
        "@context": {"@vocab": EX},
        "@type": "Parent",
        "child": [{"@id": EX + "child"}, {"@id": EX + "child"}],
        "@included": [{"@id": EX + "child", "@type": "Child"}],
    }
    assert check_emission(
        document,
        models,
        [ModelUsage("parent"), ModelUsage("child", reference=EX + "child", graph=None)],
    ).verified
    report = check_emission(
        document,
        models,
        [
            ModelUsage("parent"),
            ModelUsage("child", reference=EX + "child", graph=EX + "other"),
        ],
    )
    assert not report.verified
    assert "emission.nested_scope_unverified" in {issue.code for issue in report.issues}


def test_source_graph_selection_and_rdf_identity_do_not_infer_owl_equality() -> None:
    from bijux_ontology.declarations.types import SubjectIdentity

    document = {
        "@context": {"@vocab": EX, "id": "@id", "type": "@type"},
        "@id": EX + "graph",
        "@graph": [
            {
                "id": EX + "a",
                "type": "Record",
                "http://www.w3.org/2002/07/owl#sameAs": {"id": EX + "b"},
            }
        ],
    }
    model = Model("record", (EX + "Record",))
    usage = ModelUsage(
        "record", ("@graph", 0), subject=SubjectIdentity(EX + "a"), graph=EX + "graph"
    )
    assert check_emission(document, [model], [usage]).verified
    mismatch = ModelUsage("record", ("@graph", 0), subject=SubjectIdentity(EX + "b"))
    assert "emission.subject_mismatch" in {
        issue.code for issue in check_emission(document, [model], [mismatch]).issues
    }
    wrong_graph = ModelUsage("record", ("@graph", 0), graph=None)
    assert "emission.graph_mismatch" in {
        issue.code for issue in check_emission(document, [model], [wrong_graph]).issues
    }
    unresolved = check_emission(
        document,
        [model],
        [ModelUsage("record", reference=EX + "b", graph=EX + "graph")],
    )
    assert [issue.code for issue in unresolved.issues] == [
        "emission.reference_unverified"
    ]
    bounded = check_emission(document, [model], [usage], limits=Limits(max_work=1))
    assert not bounded.complete
    assert bounded.issues[-1].kind == "exhausted"


def test_subject_identity_plain_declaration_ingestion_is_explicit() -> None:
    from bijux_ontology.declarations.codecs import models_from_data
    from bijux_ontology.declarations.types import SubjectIdentity

    models = models_from_data(
        [
            {
                "identifier": "record",
                "types": [EX + "Record"],
                "subject": {"expected_iri": EX + "record", "allow_blank": False},
            }
        ]
    )
    assert models[0].subject == SubjectIdentity(EX + "record", allow_blank=False)
    for subject in (
        {"expected_iri": "relative"},
        {"allow_blank": "yes"},
        {"unexpected": True},
    ):
        with pytest.raises(InputError):
            models_from_data(
                [{"identifier": "record", "types": [], "subject": subject}]
            )
