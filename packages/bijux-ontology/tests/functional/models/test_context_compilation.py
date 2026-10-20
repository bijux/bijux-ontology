"""Compiled mappings retain finite probes and actual ordinary output evidence."""

from collections.abc import Iterator
from copy import deepcopy
from dataclasses import replace
from typing import Any, ClassVar, cast

import pytest
from pydantic import BaseModel, Field
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, XSD

from bijux_ontology.absence import ABSENT
from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    OutputRequirement,
    ValueDeclaration,
)
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.models.context_compilation import CompiledContext, compile_context
from bijux_ontology.models.context_probes import ContextProbe, ProbeSample
from bijux_ontology.models.emission.serialization import check_instance
from bijux_ontology.models.emission.types import ModelUsage

EX = Namespace("https://example.org/compiler/")
OPTIONS: dict[str, object] = {"mode": "json", "by_alias": True, "exclude_none": True}


class Record(BaseModel):
    calls: ClassVar[list[dict[str, object]]] = []
    kind: str = Field(serialization_alias="@type")
    title: str = Field(serialization_alias="heading")
    link: str
    parent: str
    items: list[str]

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        type(self).calls.append(kwargs)
        return super().model_dump(**kwargs)


def _probe() -> ContextProbe:
    """Supply RDF meanings independently of Python model types and options."""
    model = ModelDeclaration(
        "record",
        (str(EX.Record),),
        (
            FieldDeclaration(
                "title",
                str(EX.title),
                (ValueDeclaration("literal", str(XSD.string)),),
                key="heading",
                output=OutputRequirement(require_property=True),
            ),
            FieldDeclaration("link", str(EX.link), (ValueDeclaration("iri"),)),
            FieldDeclaration(
                "parent", str(EX.child), (ValueDeclaration("iri"),), reverse=True
            ),
            FieldDeclaration(
                "items",
                str(EX.items),
                (ValueDeclaration("literal", str(XSD.string), container="@list"),),
            ),
        ),
    )
    return ContextProbe(model, type_values=("Record",))


def _vocabulary(model: ModelDeclaration) -> Graph:
    """List heads are physical resources even when their members are literals."""
    graph = Graph()
    for iri in model.types:
        graph.add((URIRef(iri), RDF.type, OWL.Class))
    for field in model.fields:
        graph.add(
            (
                URIRef(field.predicate),
                RDF.type,
                OWL.ObjectProperty
                if field.values[0].kind == "iri" or field.values[0].container == "@list"
                else OWL.DatatypeProperty,
            )
        )
    return graph


def _record() -> Record:
    """Construct ordinary actual values without generated probe sentinels."""
    return Record(
        kind="Record",
        title="An actual title",
        link=str(EX.linked),
        parent=str(EX.owner),
        items=["first", "second"],
    )


@pytest.mark.parametrize("type_scoped", [False, True])
def test_generated_and_actual_serialization_share_explicit_mapping_truth(
    type_scoped: bool,
) -> None:
    probe = _probe()
    compiled = compile_context([probe], type_scoped=type_scoped)
    assert compiled.report.verified, compiled.report.to_dict()
    assert len(compiled.report.children) == 1
    assert compiled.report.children[0].requested == 4
    context = cast(dict[str, Any], compiled.context)
    before = deepcopy(context)
    Record.calls.clear()
    checked = check_instance(
        _record(),
        Record.model_dump,
        (probe.model,),
        (ModelUsage("record"),),
        vocabulary=_vocabulary(probe.model),
        options=OPTIONS,
        context=context,
        declaration_report=compiled.report,
    )
    assert checked.report.verified, checked.report.to_dict()
    assert compiled.report in checked.report.children
    assert Record.calls == [OPTIONS]
    assert checked.output == {
        "@type": "Record",
        "heading": "An actual title",
        "link": str(EX.linked),
        "parent": str(EX.owner),
        "items": ["first", "second"],
    }
    assert cast(dict[str, Any], checked.document)["@context"] == context
    target = context["Record"]["@context"] if type_scoped else context
    for field in ("heading", "link", "parent", "items"):
        changed = deepcopy(before)
        definition = changed["Record"]["@context"] if type_scoped else changed
        if field == "link":
            definition[field].pop("@type")
        elif field == "parent":
            definition[field] = {"@id": str(EX.child), "@type": "@id"}
        elif field == "items":
            definition[field].pop("@container")
        else:
            definition[field]["@id"] = str(EX.wrong)
        wrong = check_instance(
            _record(),
            Record.model_dump,
            (probe.model,),
            (ModelUsage("record"),),
            vocabulary=_vocabulary(probe.model),
            options=OPTIONS,
            context=changed,
            declaration_report=compiled.report,
        )
        assert not wrong.report.verified
    assert target["link"]["@type"] == "@id"
    context.clear()
    assert compiled.context == before and compiled.report.verified


def test_same_scope_conflicts_never_export_a_partially_accepted_context() -> None:
    probe = _probe()
    other_field = replace(probe.model.fields[0], name="other", predicate=str(EX.other))
    conflicting = ContextProbe(
        replace(probe.model, identifier="other", fields=(other_field,)),
        type_values=("Other",),
    )
    branches = ContextProbe(
        ModelDeclaration(
            "alternatives",
            (),
            (
                FieldDeclaration(
                    "value",
                    str(EX.value),
                    (ValueDeclaration("iri"), ValueDeclaration("literal")),
                ),
            ),
        )
    )
    alias_field = replace(probe.model.fields[0], key="Record")
    collision = ContextProbe(
        replace(probe.model, fields=(alias_field,)), type_values=("Record",)
    )
    for selection in ((probe, conflicting), (branches,), (collision,)):
        compiled = compile_context(selection)
        assert compiled.context is ABSENT and not compiled.report.verified
        assert any(
            issue.code == "context.compilation_conflict"
            for issue in compiled.report.issues
        )
        assert compiled.report.outcome == "disagreement"
    # Matching definitions can coalesce, but every selected scope remains required.
    same = compile_context((probe, probe))
    assert same.report.verified and same.report.checked == same.report.requested == 2
    assert same.report.children[0].requested == 8


def test_explicit_type_scopes_and_absolute_types_do_not_guess_alias_pairings() -> None:
    first = _probe()
    field = replace(first.model.fields[0], predicate=str(EX.other))
    second = ContextProbe(
        ModelDeclaration("other", (str(EX.Other),), (field,)), type_values=("Other",)
    )
    scoped = compile_context((first, second), type_scoped=True)
    assert scoped.report.verified
    context = cast(dict[str, Any], scoped.context)
    assert context["Record"]["@context"]["heading"]["@id"] == str(EX.title)
    assert context["Other"]["@context"]["heading"]["@id"] == str(EX.other)
    vocabulary = _vocabulary(first.model) + _vocabulary(second.model)
    wrong_instance = _record()
    wrong_instance.kind = "Other"
    wrong = check_instance(
        wrong_instance,
        Record.model_dump,
        (first.model,),
        (ModelUsage("record"),),
        vocabulary=vocabulary,
        options=OPTIONS,
        context=context,
        declaration_report=scoped.report,
    )
    assert not wrong.report.verified

    class MultipleTypes(BaseModel):
        kind: str | list[str] = Field(serialization_alias="@type")
        title: str = Field(serialization_alias="heading")
        link: str
        parent: str
        items: list[str]

    both = MultipleTypes(
        kind=["Record", "Other"],
        title="A shared source term",
        link=str(EX.linked),
        parent=str(EX.owner),
        items=["one"],
    )
    combined = check_instance(
        both,
        MultipleTypes.model_dump,
        (first.model, second.model),
        (ModelUsage("record"), ModelUsage("other")),
        vocabulary=vocabulary,
        options=OPTIONS,
        context=context,
        declaration_report=scoped.report,
    )
    assert not combined.report.verified
    same_term = replace(second, type_values=("Record",))
    assert compile_context((first, same_term), type_scoped=True).context is ABSENT
    unaliased = compile_context((replace(first, type_values=()),))
    assert unaliased.report.verified and "Record" not in cast(
        dict[str, Any], unaliased.context
    )
    assert (
        compile_context((replace(first, type_values=()),), type_scoped=True).context
        is ABSENT
    )
    multiple = replace(
        first,
        model=replace(first.model, types=(str(EX.Record), str(EX.Other))),
        type_values=("Record", "Other"),
    )
    assert compile_context((multiple,)).context is ABSENT
    empty = ContextProbe(
        ModelDeclaration("empty", (str(EX.Empty),)), type_values=("Empty",)
    )
    assert compile_context((empty,)).report.verified
    typeless = ContextProbe(ModelDeclaration("unscoped", ()))
    assert compile_context((first, typeless)).context is ABSENT


def test_language_direction_containers_and_authored_object_samples() -> None:
    values = (
        ValueDeclaration("literal", str(RDF.langString), language="en"),
        ValueDeclaration("literal", language=ABSENT, direction=ABSENT),
        ValueDeclaration("literal", language="ar", direction="rtl"),
        ValueDeclaration("literal", str(XSD.integer), container="@set"),
        ValueDeclaration("object"),
    )
    fields = tuple(
        FieldDeclaration(f"field{index}", str(EX[f"field{index}"]), (value,))
        for index, value in enumerate(values)
    )
    raw = {"@id": str(EX.named)}
    sample = ProbeSample("field4", 0, raw)
    probe = ContextProbe(ModelDeclaration("values", (), fields), samples=(sample,))
    compiled = compile_context((probe,))
    assert compiled.report.verified, compiled.report.to_dict()
    context = cast(dict[str, Any], compiled.context)
    assert context["field0"] == {"@id": str(EX.field0), "@language": "en"}
    assert context["field1"]["@direction"] is None
    assert context["field2"]["@direction"] == "rtl"
    assert context["field3"]["@container"] == "@set"
    raw.clear()
    assert sample.value == {"@id": str(EX.named)}


@pytest.mark.parametrize(
    "value,predicate",
    [
        (ValueDeclaration("unknown"), str(EX.value)),
        (ValueDeclaration("object"), str(EX.value)),
        (ValueDeclaration("literal", str(EX.custom)), str(EX.value)),
        (ValueDeclaration("literal", container="@language"), str(EX.value)),
        (ValueDeclaration("iri"), "@id"),
    ],
)
def test_unsupported_branch_among_valid_siblings_remains_required(
    value: ValueDeclaration, predicate: str
) -> None:
    model = ModelDeclaration(
        "unsupported", (), (FieldDeclaration("value", predicate, (value,)),)
    )
    compiled = compile_context((_probe(), ContextProbe(model)))
    assert compiled.context is ABSENT and not compiled.report.verified
    assert compiled.report.outcome == "unverified"
    assert compiled.report.requested == 2


def test_bounded_capture_partial_findings_and_public_result_ownership() -> None:
    class Indexed(list[ContextProbe]):
        def __iter__(self) -> Iterator[ContextProbe]:
            raise AssertionError("Compilation must capture a bounded index range")

    probe = _probe()
    selected = Indexed([probe])
    compiled = compile_context(selected)
    selected.clear()
    assert compiled.report.verified and compiled.context is not ABSENT
    for limits in (Limits(max_work=1), Limits(max_states=1), Limits(max_bytes=10)):
        limited = compile_context((probe,), limits=limits)
        assert limited.context is ABSENT and limited.report.outcome == "exhausted"
        assert not limited.report.complete
    bad = replace(probe.model.fields[0], name="other", predicate=str(EX.wrong))
    conflict = ContextProbe(
        replace(probe.model, identifier="other", fields=(bad,)),
        type_values=("Other",),
    )
    partial = compile_context(
        (probe, conflict) + (probe,) * 50, limits=Limits(max_work=250)
    )
    assert partial.context is ABSENT and partial.report.outcome == "exhausted"
    assert any(
        issue.code == "context.compilation_conflict" for issue in partial.report.issues
    )
    assert not partial.report.complete and partial.report.requested == 52
    assert compile_context(()).context is ABSENT
    operation = Budget(Limits(max_work=1))
    assert compile_context((probe,), budget=operation).report.outcome == "exhausted"
    with pytest.raises(InputError):
        compile_context((probe,), budget=operation, limits=Limits())
    incomplete = Report("incomplete", 1, requested=2, complete=False)
    assert CompiledContext({"partial": str(EX.partial)}, incomplete).context is ABSENT


def test_repeated_generated_scope_metadata_shares_the_operation_byte_limit() -> None:
    model = ModelDeclaration(
        "long-model-" * 1000,
        (),
        (
            FieldDeclaration(
                "value",
                str(EX.value),
                (ValueDeclaration("literal", samples=(Literal("value"),) * 100),),
            ),
        ),
    )
    compiled = compile_context(
        (ContextProbe(model),),
        limits=Limits(max_bytes=20_000, max_total_bytes=200_000, max_states=5000),
    )
    assert compiled.context is ABSENT and compiled.report.outcome == "exhausted"
    assert not compiled.report.complete
