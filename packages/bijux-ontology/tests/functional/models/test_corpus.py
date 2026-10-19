"""Actual serializer cases retain correctness and required alternative coverage."""

from dataclasses import replace
from typing import Any

import pytest
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.declarations.types import (
    EmissionCondition,
    EmissionGroup,
    EmissionScenario,
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Limits
from bijux_ontology.models.corpus import (
    BranchCoverage,
    ScenarioCoverage,
    SerializerCase,
    check_serializer_corpus,
)
from bijux_ontology.models.emission.types import ModelUsage

EX = Namespace("https://example.org/corpus/")


def vocabulary() -> Graph:
    graph = Graph()
    for kind in (EX.Record, EX.Child):
        graph.add((kind, RDF.type, OWL.Class))
    graph.add((EX.value, RDF.type, RDF.Property))
    for predicate in (EX.mode, EX.name):
        graph.add((predicate, RDF.type, OWL.DatatypeProperty))
        graph.add((predicate, RDFS.range, XSD.string))
    return graph


def declarations() -> tuple[ModelDeclaration, ...]:
    return (
        ModelDeclaration(
            "record",
            (str(EX.Record),),
            (
                FieldDeclaration(
                    "value",
                    str(EX.value),
                    (
                        ValueDeclaration("literal", str(XSD.string)),
                        ValueDeclaration("object", model="child"),
                    ),
                    emission="guaranteed",
                ),
                FieldDeclaration(
                    "mode",
                    str(EX.mode),
                    (ValueDeclaration("literal", str(XSD.string)),),
                    emission="optional",
                ),
            ),
        ),
        ModelDeclaration(
            "child",
            (str(EX.Child),),
            (
                FieldDeclaration(
                    "name",
                    str(EX.name),
                    (ValueDeclaration("literal", str(XSD.string)),),
                    emission="guaranteed",
                ),
            ),
        ),
    )


def document(value: object, *, mode: object = None) -> dict[str, object]:
    result: dict[str, object] = {
        "@context": {"@vocab": str(EX)},
        "@id": str(EX.record),
        "@type": "Record",
        "value": value,
    }
    if mode is not None:
        result["mode"] = mode
    return result


def identity(instance: object, **options: object) -> object:
    return instance


def case(identifier: str, output: object) -> SerializerCase:
    return SerializerCase(identifier, output, identity, (ModelUsage("record"),))


def test_literal_and_automatic_nested_resource_witnesses_retain_failing_branch() -> (
    None
):
    required = (
        BranchCoverage("record", "value", 0),
        BranchCoverage("record", "value", 1),
    )
    literal = case("literal", document("text"))
    nested = case(
        "resource", document({"@id": str(EX.child), "@type": "Child", "name": "name"})
    )
    result = check_serializer_corpus(
        (literal, nested),
        declarations(),
        vocabulary=vocabulary(),
        required_branches=required,
    )
    assert result.report.verified, result.report.to_dict()
    assert result.branches == (
        (required[0], ("literal",)),
        (required[1], ("resource",)),
    )
    missing = check_serializer_corpus(
        (literal,), declarations(), vocabulary=vocabulary(), required_branches=required
    )
    assert missing.coverage.outcome == "unverified" and not missing.report.verified
    invalid = case("bad_child", document({"@id": str(EX.child), "@type": "Child"}))
    failed = check_serializer_corpus(
        (literal, invalid),
        declarations(),
        vocabulary=vocabulary(),
        required_branches=required,
    )
    assert failed.coverage.verified
    assert not failed.report.verified
    assert any(
        issue.code == "emission.required_occurrence_missing"
        for issue in failed.report.issues
    )


def test_overlapping_branches_require_one_actual_exact_discriminator() -> None:
    model, child = declarations()
    model = replace(
        model,
        fields=(
            replace(
                model.fields[0],
                values=(
                    ValueDeclaration("literal"),
                    ValueDeclaration("literal", str(XSD.string)),
                ),
            ),
            model.fields[1],
        ),
    )
    requirements = tuple(
        BranchCoverage(
            "record", "value", index, EmissionCondition("mode", Literal(mode))
        )
        for index, mode in enumerate(("left", "right"))
    )
    cases = (
        case("left", document("text", mode="left")),
        case("right", document("text", mode="right")),
    )
    result = check_serializer_corpus(
        cases, (model, child), vocabulary=vocabulary(), required_branches=requirements
    )
    assert result.coverage.verified, result.report.to_dict()
    assert result.branches == (
        (requirements[0], ("left",)),
        (requirements[1], ("right",)),
    )
    ambiguous = check_serializer_corpus(
        cases,
        (model, child),
        vocabulary=vocabulary(),
        required_branches=tuple(
            replace(item, discriminator=None) for item in requirements
        ),
    )
    assert not ambiguous.coverage.verified and not any(
        witnesses for _, witnesses in ambiguous.branches
    )
    multiple = check_serializer_corpus(
        (case("both", document("text", mode=["left", "right"])),),
        (model, child),
        vocabulary=vocabulary(),
        required_branches=requirements,
    )
    assert not any(witnesses for _, witnesses in multiple.branches)


def test_scenario_coverage_uses_actual_presence_and_preserves_overlap_ambiguity() -> (
    None
):
    model, child = declarations()
    group = EmissionGroup(
        "selection",
        ("value",),
        scenarios=(
            EmissionScenario(
                "left", ("value",), EmissionCondition("mode", Literal("left"))
            ),
            EmissionScenario(
                "right", ("value",), EmissionCondition("mode", Literal("right"))
            ),
        ),
        closed=True,
    )
    model = replace(model, emission_groups=(group,))
    required = tuple(
        ScenarioCoverage("record", "selection", name) for name in ("left", "right")
    )
    result = check_serializer_corpus(
        (
            case("a", document("text", mode="left")),
            case("b", document("text", mode="right")),
        ),
        (model, child),
        vocabulary=vocabulary(),
        required_scenarios=required,
    )
    assert result.coverage.verified, result.report.to_dict()
    assert result.scenarios == ((required[0], ("a",)), (required[1], ("b",)))
    overlap = replace(
        model,
        emission_groups=(
            replace(
                group,
                scenarios=(
                    EmissionScenario("left", ("value",)),
                    EmissionScenario("right", ("value",)),
                ),
            ),
        ),
    )
    ambiguous = check_serializer_corpus(
        (case("a", document("text")),),
        (overlap, child),
        vocabulary=vocabulary(),
        required_scenarios=required,
    )
    assert not ambiguous.coverage.verified
    assert not any(witnesses for _, witnesses in ambiguous.scenarios)


def test_options_owned_exact_once_and_exhaustion_retains_prior_failure() -> None:
    options = {"mode": "actual"}
    calls = []

    def serialize(instance: object, **kwargs: object) -> object:
        calls.append(kwargs)
        return instance

    sample = SerializerCase(
        "first", document(42), serialize, (ModelUsage("record"),), options=options
    )
    options["mode"] = "mutated"
    sample.options.clear()
    result = check_serializer_corpus(
        (sample, case("second", document("okay"))),
        declarations(),
        vocabulary=vocabulary(),
        required_branches=(BranchCoverage("record", "value", 0),),
        limits=Limits(max_resources=2),
    )
    assert calls == [{"mode": "actual"}]
    assert not result.report.verified
    assert any(
        issue.code == "emission.value_mismatch" for issue in result.report.issues
    )
    assert result.report.outcome == "exhausted"
    assert any(
        issue.location == "serialization.corpus.remaining"
        for issue in result.report.issues
    )
    invalid: tuple[Any, ...] = (None, "bindings", b"bindings", 1, iter(()))
    for container in invalid:
        with pytest.raises(InputError):
            SerializerCase("invalid", {}, identity, container)
        for argument in ("cases", "models", "required_branches", "required_scenarios"):
            inputs: dict[str, Any] = {
                "cases": (sample,),
                "models": declarations(),
                "required_branches": (BranchCoverage("record", "value", 0),),
                "vocabulary": vocabulary(),
            }
            inputs[argument] = container
            with pytest.raises(InputError):
                check_serializer_corpus(**inputs)
    for argument in ("required_branches", "required_scenarios"):
        inputs = {
            "cases": (sample,),
            "models": declarations(),
            "required_branches": (BranchCoverage("record", "value", 0),),
            "vocabulary": vocabulary(),
        }
        inputs[argument] = [{}]
        with pytest.raises(InputError):
            check_serializer_corpus(**inputs)
    with pytest.raises(InputError):
        SerializerCase("invalid", {}, identity, (1,))  # type: ignore[arg-type]
    usages = [ModelUsage("record")]
    owned = SerializerCase("owned", {}, identity, usages)
    usages.clear()
    assert owned.usages == (ModelUsage("record"),)
    with pytest.raises(InputError):
        check_serializer_corpus((), declarations(), vocabulary=vocabulary())
    with pytest.raises(InputError):
        check_serializer_corpus(
            (sample, sample),
            declarations(),
            vocabulary=vocabulary(),
            required_branches=(BranchCoverage("record", "value", 0),),
        )


def test_discriminator_witnesses_do_not_join_same_iri_source_bindings() -> None:
    model, child = declarations()
    model = replace(
        model,
        fields=(
            replace(
                model.fields[0],
                values=(
                    ValueDeclaration("literal"),
                    ValueDeclaration("literal", str(XSD.string)),
                ),
            ),
            model.fields[1],
        ),
    )
    required = (
        BranchCoverage(
            "record", "value", 0, EmissionCondition("mode", Literal("left"))
        ),
    )
    output = {
        "@context": {"@vocab": str(EX)},
        "@graph": [document(42, mode="left"), document("text", mode="right")],
    }
    sample = SerializerCase(
        "split",
        output,
        identity,
        (ModelUsage("record", ("@graph", 0)), ModelUsage("record", ("@graph", 1))),
    )
    result = check_serializer_corpus(
        (sample,), (model, child), vocabulary=vocabulary(), required_branches=required
    )
    assert not result.coverage.verified
    assert result.branches == ((required[0], ()),)
    # Explicit entity scope intentionally sees the joined same-graph values. A
    # multivalued discriminator still cannot select one alternative.
    joined = SerializerCase(
        "joined",
        output,
        identity,
        (ModelUsage("record", reference=str(EX.record), graph=None, entity=True),),
    )
    result = check_serializer_corpus(
        (joined,), (model, child), vocabulary=vocabulary(), required_branches=required
    )
    assert not result.coverage.verified and not result.branches[0][1]
