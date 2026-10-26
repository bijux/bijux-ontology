"""Ordinary nested Pydantic samples use actual output and one semantic source."""

import sys
from types import FrameType
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
from bijux_ontology.diagnostics.errors import InputError
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
    originals: list[bool] = []
    receipts: list[bool] = []

    def observe(frame: FrameType, event: str, arg: object) -> None:
        """Count only original preparation; derivative documents remain independent."""
        if event != "call":
            return
        module = frame.f_globals.get("__name__")
        if module == "bijux_ontology.jsonld.processing.expansion" and (
            frame.f_code.co_name == "capture_original_expansion"
            and frame.f_back is not None
            and frame.f_back.f_globals.get("__name__")
            == "bijux_ontology.jsonld.usages.checking"
            and frame.f_back.f_code.co_name == "__init__"
        ):
            originals.append(True)
        if module == "bijux_ontology.models.emission.checking" and (
            frame.f_code.co_name == "check_emission"
        ):
            prepared = frame.f_locals.get("_prepared")
            receipts.append(
                prepared is not None
                and prepared._budget is frame.f_locals["budget"]
                and prepared._document is frame.f_locals["document"]
                and prepared._loader is frame.f_locals["document_loader"]
            )

    previous = sys.getprofile()
    try:
        sys.setprofile(observe)
        result = check_instance(
            catalogue(),
            vocabulary=vocabulary(),
            context=CONTEXT,
            options=OPTIONS,
            requirements={(Item, "label"): OutputRequirement(require_property=True)},
        )
    finally:
        sys.setprofile(previous)
    assert originals == [True] and receipts == [True]
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
    from bijux_ontology.execution.limits import Limits

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
    assert not bounded.report.complete and bounded.output_json is None
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


def test_computed_sidecars_keep_members_aliases_and_actual_output() -> None:
    from pydantic import computed_field

    from bijux_ontology.interfaces.adapters.contracts import RdfField

    calls: list[object] = []
    dumps: list[dict[str, Any]] = []

    class Computed(BaseModel):
        kind: str = Field(default="Item", exclude=True)

        def model_dump(self, **kwargs: Any) -> dict[str, Any]:
            dumps.append(kwargs)
            return {"@type": self.kind, **super().model_dump(**kwargs)}

        label: str

        @computed_field(alias="summaryName")
        def summary(self) -> str:
            calls.append("computed")
            return self.label.upper()

    sidecars: dict[tuple[type[BaseModel], str], RdfField] = {
        (Computed, "label"): RdfField(
            str(EX.label),
            (ValueDeclaration("literal", datatype=str(XSD.string)),),
            output_contract=True,
        ),
        (Computed, "summary"): RdfField(
            str(EX.note),
            (ValueDeclaration("literal", datatype=str(XSD.string)),),
            output_contract=True,
            emission="guaranteed",
        ),
    }
    extraction = model_declarations(
        {Computed: (str(EX.Item),)},
        identifiers={Computed: "computed"},
        field_contracts=sidecars,
    )
    assert extraction.report.verified, extraction.report.to_dict()
    field = extraction.declarations[0].fields[-1]
    assert field.name == "summary" and field.key == "summaryName"
    from bijux_ontology.execution.limits import Limits

    stopped = check_instance(
        Computed(label="Name"),
        inventory=extraction,
        model="computed",
        vocabulary=vocabulary(),
        context={**CONTEXT, "summaryName": str(EX.note)},
        options={"mode": "json", "by_alias": True},
        limits=Limits(max_work=1),
    )
    assert stopped.report.outcome == "exhausted"
    assert not calls and not dumps and stopped.output_json is None
    assert all(i.code != "input.empty_scope" for i in stopped.report.issues)
    context = {**CONTEXT, "summaryName": str(EX.note)}
    instance = Computed(label="Name")
    actual = check_instance(
        instance,
        inventory=extraction,
        model="computed",
        vocabulary=vocabulary(),
        context=context,
        options={"mode": "json", "by_alias": True},
    )
    assert actual.report.verified, actual.report.to_dict()
    assert actual.output == {"@type": "Item", "label": "Name", "summaryName": "NAME"}
    assert calls == ["computed"]
    assert dumps == [{"mode": "json", "by_alias": True}]
    changed = check_instance(
        instance,
        inventory=extraction,
        model="computed",
        vocabulary=vocabulary(),
        context={**context, "summaryName": str(EX.label)},
        options={"mode": "json", "by_alias": True},
    )
    assert any(i.code == "context.mapping_mismatch" for i in changed.report.issues)
    unowned = model_declarations(
        {Computed: (str(EX.Item),)},
        field_contracts={
            key: value for key, value in sidecars.items() if key[1] != "summary"
        },
    )
    assert any(
        i.code == "adapter.computed_output_unverified" for i in unowned.report.issues
    )

    partial = model_declarations(
        {Computed: (str(EX.Item),)},
        identifiers={Computed: "computed"},
        serializer_contracts={Computed: ModelDeclaration("computed", (str(EX.Item),))},
    )
    open_result = check_instance(
        instance,
        inventory=partial,
        model="computed",
        vocabulary=vocabulary(),
        context=context,
        options={"mode": "json", "by_alias": True},
    )
    assert open_result.report.outcome == "unverified"
    assert any(i.code == "adapter.output_unverified" for i in open_result.report.issues)

    class Collision(Computed):
        label: str = Field(serialization_alias="summaryName")

    with pytest.raises(InputError, match="computed serialization alias"):
        model_declarations({Collision: (str(EX.Item),)})


def test_whole_serializer_registry_checks_renamed_children_once() -> None:
    from bijux_ontology.declarations.types import EmissionCoverage

    calls: list[object] = []

    class ChildOutput(BaseModel):
        label: str

    class ParentOutput(BaseModel):
        child: ChildOutput
        wrong: bool = False
        missing: bool = False

        @model_serializer
        def serialized(self) -> dict[str, object]:
            calls.append("model")
            child = {"@type": "Item"}
            if not self.missing:
                child["label"] = self.child.label
            return {"@type": "Catalogue", "changed" if self.wrong else "renamed": child}

    child = ModelDeclaration(
        "child-output",
        (str(EX.Item),),
        (
            FieldDeclaration(
                "label",
                str(EX.label),
                (ValueDeclaration("literal", datatype=str(XSD.string)),),
                output=OutputRequirement(require_property=True),
            ),
        ),
        coverage=EmissionCoverage(metadata=("@context", "@type")),
    )
    parent = ModelDeclaration(
        "parent-output",
        (str(EX.Catalogue),),
        (
            FieldDeclaration(
                "child",
                str(EX.items),
                (ValueDeclaration("object", model="child-output"),),
                key="renamed",
                output=OutputRequirement(require_property=True),
            ),
        ),
        coverage=EmissionCoverage(metadata=("@context", "@type")),
    )
    extraction = model_declarations(
        {ParentOutput: parent.types, ChildOutput: child.types},
        identifiers={ParentOutput: parent.identifier, ChildOutput: child.identifier},
        serializer_contracts={ParentOutput: parent, ChildOutput: child},
    )
    assert extraction.report.verified
    assert extraction.declarations == (parent, child)
    context = {**CONTEXT, "renamed": str(EX.items), "changed": str(EX.changed)}
    for wrong, missing in ((False, False), (True, False), (False, True)):
        result = check_instance(
            ParentOutput(child=ChildOutput(label="Name"), wrong=wrong, missing=missing),
            inventory=extraction,
            model=parent.identifier,
            vocabulary=vocabulary(),
            context=context,
            options=OPTIONS,
        )
        assert result.report.verified is (not wrong and not missing), (
            result.report.to_dict()
        )
        if not wrong and not missing:
            assert result.output == {
                "@type": "Catalogue",
                "renamed": {"@type": "Item", "label": "Name"},
            }
        else:
            assert any(i.kind == "finding" for i in result.report.issues)
    assert calls == ["model"] * 3
    assert parent.fields[0].name == "child" and parent.fields[0].key == "renamed"


def test_whole_serializer_conditions_use_actual_emitted_discriminators() -> None:
    from rdflib import Literal

    from bijux_ontology.declarations.types import (
        EmissionCondition,
        EmissionCoverage,
        EmissionGroup,
        EmissionScenario,
    )

    class BranchOutput(BaseModel):
        mode: str
        wrong: bool = False

        @model_serializer
        def serialized(self) -> dict[str, object]:
            selected = "a" if (self.mode == "left") != self.wrong else "b"
            return {"@type": "Item", "mode": self.mode, selected: "Name"}

    declaration = ModelDeclaration(
        "branch-output",
        (str(EX.Item),),
        tuple(
            FieldDeclaration(
                name,
                str(EX[name] if name != "mode" else EX.note),
                (ValueDeclaration("literal", datatype=str(XSD.string)),),
                emission="optional",
            )
            for name in ("mode", "a", "b")
        ),
        emission_groups=(
            EmissionGroup(
                "choice",
                ("a", "b"),
                minimum=1,
                maximum=1,
                closed=True,
                scenarios=(
                    EmissionScenario(
                        "left", ("a",), EmissionCondition("mode", Literal("left"))
                    ),
                    EmissionScenario(
                        "right", ("b",), EmissionCondition("mode", Literal("right"))
                    ),
                ),
            ),
        ),
        coverage=EmissionCoverage(metadata=("@context", "@type")),
    )
    extraction = model_declarations(
        {BranchOutput: declaration.types},
        identifiers={BranchOutput: declaration.identifier},
        serializer_contracts={BranchOutput: declaration},
    )
    context = {**CONTEXT, "mode": str(EX.note), "a": str(EX.a), "b": str(EX.b)}
    graph = vocabulary()
    for predicate in (EX.a, EX.b):
        graph.add((predicate, RDF.type, OWL.DatatypeProperty))
        graph.add((predicate, RDFS.range, XSD.string))
    for mode, wrong in (("left", False), ("right", False), ("left", True)):
        result = check_instance(
            BranchOutput(mode=mode, wrong=wrong),
            inventory=extraction,
            model=declaration.identifier,
            vocabulary=graph,
            context=context,
            options=OPTIONS,
        )
        assert result.report.verified is not wrong, result.report.to_dict()
        if wrong:
            assert any(
                i.code == "emission.scenario_mismatch" for i in result.report.issues
            )


def test_serializer_failure_metadata_uses_the_bounded_native_boundary() -> None:
    from bijux_ontology.execution.limits import Limits
    from bijux_ontology.interfaces.adapters.contracts import RdfField

    formatted: list[bool] = []

    class UnsafeError(Exception):
        def __str__(self) -> str:
            formatted.append(True)
            raise AssertionError("Caller formatting must not execute")

    UnsafeError.__name__ = "x" * 100_000

    class FailedOutput(BaseModel):
        label: Annotated[
            str,
            RdfField(
                str(EX.label),
                (ValueDeclaration("literal", datatype=str(XSD.string)),),
                output_contract=True,
            ),
        ] = "declared"
        problem: ClassVar[Exception] = UnsafeError()

        def model_dump(self, **kwargs: Any) -> dict[str, Any]:
            raise self.problem

    for error, outcome in (
        (UnsafeError(), "input_error"),
        (ValueError("😀" * 1000), "exhausted"),
    ):
        FailedOutput.problem = error
        result = check_instance(
            FailedOutput(),
            vocabulary=vocabulary(),
            options={"mode": "json"},
            limits=Limits(max_bytes=1000),
        )
        assert result.output_json is None and not result.report.verified
        assert result.report.issues[-1].kind == outcome
        assert all(len(i.message.encode()) <= 1000 for i in result.report.issues)
    FailedOutput.problem = UnsafeError()
    prior = model_declarations(
        {FailedOutput: (str(EX.Item),)},
        identifiers={FailedOutput: "failed"},
        field_contracts={
            (FailedOutput, "label"): RdfField(
                str(EX.note),
                (ValueDeclaration("literal", datatype=str(XSD.string)),),
                output_contract=True,
            )
        },
    )
    assert any(i.code == "adapter.mapping_conflict" for i in prior.report.issues)
    limited = check_instance(
        FailedOutput(),
        inventory=prior,
        vocabulary=vocabulary(),
        options={"mode": "json"},
        limits=Limits(max_bytes=1000, max_issues=1),
    )
    assert limited.report.outcome == "exhausted"
    assert any(i.code == "adapter.mapping_conflict" for i in limited.report.issues)
    assert sum(i.kind != "exhausted" for i in limited.report.issues) == 1
    assert not formatted
