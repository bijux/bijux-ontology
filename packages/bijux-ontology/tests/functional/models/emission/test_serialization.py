"""Actual callable serializer samples retain explicit options and failed contracts."""

import json
from collections import OrderedDict
from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, Field
from rdflib import Graph, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.interfaces.adapters.contracts import RdfField
from bijux_ontology.interfaces.adapters.pydantic import model_declarations
from bijux_ontology.jsonld.checking import prepare_usages
from bijux_ontology.jsonld.expectations import ContextExpectation
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.models.contracts.checking import check_models
from bijux_ontology.models.contracts.preparation import PreparedModels
from bijux_ontology.models.emission.checking import check_emission
from bijux_ontology.models.emission.serialization import (
    check_instance,
    check_serialized,
)
from bijux_ontology.models.emission.types import ModelUsage
from bijux_ontology.vocabulary.sources.loading import load_vocabulary

EX = Namespace("https://example.org/")


def vocabulary() -> Graph:
    """Supply only the declared class and literal field contracts."""
    graph = Graph().add((EX.Record, RDF.type, OWL.Class))
    for predicate in (EX.name, EX.note):
        graph.add((predicate, RDF.type, OWL.DatatypeProperty))
        graph.add((predicate, RDFS.domain, EX.Record))
        graph.add((predicate, RDFS.range, XSD.string))
    return graph


def test_actual_pydantic_alias_and_omission_options_are_passed_once() -> None:
    class Record(BaseModel):
        name: Annotated[
            str,
            RdfField(
                str(EX.name),
                (ValueDeclaration("literal", str(XSD.string)),),
                output_contract=True,
                emission="optional",
            ),
        ] = Field(serialization_alias="title")
        note: Annotated[
            str | None,
            RdfField(
                str(EX.note),
                (ValueDeclaration("literal", str(XSD.string)),),
                output_contract=True,
                emission="optional",
            ),
        ] = None

    options = {"mode": "json", "by_alias": True, "exclude_none": True}
    extraction = model_declarations({Record: (str(EX.Record),)}, exclude_none=True)
    calls = []

    def serialize(instance: Record, **kwargs: object) -> object:
        """Use the real framework serializer with exactly the chosen public options."""
        calls.append(kwargs)
        return {"@type": str(EX.Record), **instance.model_dump(**kwargs)}  # type: ignore[arg-type]

    result = check_instance(
        Record(name="Name"),
        serialize,
        extraction.declarations,
        [ModelUsage(extraction.declarations[0].identifier)],
        vocabulary=vocabulary(),
        options=options,
        context={"title": str(EX.name), "note": str(EX.note)},
        declaration_report=extraction.report,
    )
    assert result.report.verified, result.report.to_dict()
    assert calls == [options]
    assert result.output == {"@type": str(EX.Record), "title": "Name"}
    copy = result.document
    assert isinstance(copy, dict)
    copy.clear()
    assert result.document and result.output


def test_independent_dataclass_serializer_cannot_change_kind_or_drop_guarantees() -> (
    None
):
    @dataclass
    class CatalogueRecord:
        """An application owns its ordinary non-biological serialization policy."""

        label: str

    model = ModelDeclaration(
        "record",
        (str(EX.Record),),
        (
            FieldDeclaration(
                "label",
                str(EX.name),
                (ValueDeclaration("literal", str(XSD.string)),),
                emission="guaranteed",
            ),
        ),
    )

    def serialize(instance: CatalogueRecord, *, output_kind: str) -> object:
        """Return literal output or a concrete incompatible serializer change."""
        output: dict[str, object] = {"@type": str(EX.Record)}
        if output_kind != "omitted":
            output["label"] = (
                {"@id": "urn:changed"} if output_kind == "iri" else instance.label
            )
        return output

    for kind in ("literal", "iri", "omitted"):
        result = check_instance(
            CatalogueRecord("Name"),
            serialize,
            [model],
            [ModelUsage("record")],
            vocabulary=vocabulary(),
            options={"output_kind": kind},
            context={"label": str(EX.name)},
        )
        assert result.report.verified is (kind == "literal")
        if kind != "literal":
            assert any(
                issue.code
                == (
                    "emission.value_mismatch"
                    if kind == "iri"
                    else "emission.required_occurrence_missing"
                )
                for issue in result.report.issues
            )
        assert result.output is not None


def test_serializer_failure_and_conflicting_context_preserve_incomplete_samples() -> (
    None
):
    model = ModelDeclaration("record", (str(EX.Record),))

    def failure(instance: object, **options: object) -> object:
        """A framework's own rejection is a failed actual serialization attempt."""
        raise ValueError("consumer rejected its input")

    failed = check_instance(
        object(),
        failure,
        [model],
        [ModelUsage("record")],
        vocabulary=vocabulary(),
        options={},
    )
    assert failed.report.outcome == "input_error" and not failed.report.complete
    conflict = check_instance(
        object(),
        lambda value: {"@context": {"name": str(EX.name)}},
        [model],
        [ModelUsage("record")],
        vocabulary=vocabulary(),
        options={},
        context={"name": "urn:other"},
    )
    assert conflict.report.outcome == "input_error"
    assert conflict.output == {"@context": {"name": str(EX.name)}}


def test_external_context_preserves_actual_json_types_and_order() -> None:
    """An external context cannot repair a serializer's different invalid context."""
    model = ModelDeclaration("record", (str(EX.Record),))
    models, usages = (model,), (ModelUsage("record"),)
    expected = {"@propagate": True, "name": str(EX.name)}
    prepared = PreparedModels(models, vocabulary(), context=expected)
    for propagate in (1, 1.0):
        actual = {
            "@context": {"name": str(EX.name), "@propagate": propagate},
            "@type": str(EX.Record),
            "name": "Name",
        }
        original = check_serialized(actual, models, usages, vocabulary=vocabulary())
        assert original.report.outcome == "input_error"
        checked = check_serialized(
            actual, models, usages, vocabulary=vocabulary(), context=expected
        )
        assert checked.report.outcome == "input_error"
        assert checked.output == actual and checked.document_json is None
        assert prepared.check(actual, usages).outcome == "input_error"

    reordered = {
        "@context": {"name": str(EX.name), "@propagate": True},
        "@type": str(EX.Record),
        "name": "Name",
    }
    assert check_serialized(
        reordered, models, usages, vocabulary=vocabulary(), context=expected
    ).report.verified
    assert prepared.check(reordered, usages).verified

    subclass_context = OrderedDict((("name", URIRef(EX.name)), ("@propagate", True)))
    compatible = {**reordered, "@context": subclass_context}
    assert check_serialized(
        compatible, models, usages, vocabulary=vocabulary(), context=expected
    ).report.verified
    assert prepared.check(
        {**reordered, "@context": dict(subclass_context)}, usages
    ).verified
    assert (
        PreparedModels(models, vocabulary(), context=dict(subclass_context))
        .check(reordered, usages)
        .verified
    )

    class ContextArray(list[object]):
        """An ordinary application list subclass retains JSON array meaning."""

    contexts = [{"name": str(EX.name)}, {"@propagate": True}]
    ordered = {"@context": contexts, "@type": str(EX.Record), "name": "Name"}
    assert check_serialized(
        ordered, models, usages, vocabulary=vocabulary(), context=contexts
    ).report.verified
    assert check_serialized(
        {**ordered, "@context": ContextArray(contexts)},
        models,
        usages,
        vocabulary=vocabulary(),
        context=contexts,
    ).report.verified
    assert (
        PreparedModels(models, vocabulary(), context=contexts)
        .check(ordered, usages)
        .verified
    )
    assert (
        check_serialized(
            ordered,
            models,
            usages,
            vocabulary=vocabulary(),
            context=list(reversed(contexts)),
        ).report.outcome
        == "input_error"
    )
    assert (
        PreparedModels(models, vocabulary(), context=list(reversed(contexts)))
        .check(ordered, usages)
        .outcome
        == "input_error"
    )


def test_serializer_checks_share_a_cumulative_operation_budget() -> None:
    model = ModelDeclaration(
        "record",
        (str(EX.Record),),
        (
            FieldDeclaration(
                "name",
                str(EX.name),
                (ValueDeclaration("literal", str(XSD.string)),),
                emission="guaranteed",
            ),
        ),
    )
    document = {"@context": {"@vocab": str(EX)}, "@type": "Record", "name": "Name"}
    owned = load_vocabulary(vocabulary())
    usages = [ModelUsage("record")]
    limits = Limits(max_work=80)
    assert check_models([model], owned, limits=limits).verified
    assert (
        check_emission(document, [model], usages, limits=limits).outcome == "exhausted"
    )
    exhausted = check_instance(
        None,
        lambda value: document,
        [model],
        usages,
        vocabulary=owned,
        options={},
        limits=limits,
    )
    assert exhausted.report.outcome == "exhausted"
    assert not exhausted.report.complete
    assert exhausted.output_json is None
    fresh = check_instance(
        None,
        lambda value: document,
        [model],
        usages,
        vocabulary=owned,
        options={},
        limits=Limits(max_work=120),
    )
    assert fresh.report.outcome == "exhausted" and fresh.output_json is None
    model_budget, emission_budget = Budget(), Budget()
    assert check_models([model], owned, budget=model_budget).verified
    assert check_emission(document, [model], usages, budget=emission_budget).verified
    grant = Limits(
        max_work=max(
            current.limits.max_work - current.remaining("work")
            for current in (model_budget, emission_budget)
        )
    )
    assert check_models([model], owned, limits=grant).verified
    assert check_emission(document, [model], usages, limits=grant).verified
    combined = check_instance(
        None,
        lambda value: document,
        [model],
        usages,
        vocabulary=owned,
        options={},
        limits=grant,
    )
    assert combined.report.outcome == "exhausted"
    assert combined.output == document
    recovered = check_instance(
        None,
        lambda value: document,
        [model],
        usages,
        vocabulary=owned,
        options={},
    )
    assert recovered.report.verified, recovered.report.to_dict()
    assert recovered.output == document


def test_serializer_snapshots_require_storage_before_retaining_output() -> None:
    """Large ordinary JSON inputs cannot be copied through a tiny shared grant."""
    models = (ModelDeclaration("record", (str(EX.Record),)),)
    usages = (ModelUsage("record"),)
    owned = load_vocabulary(vocabulary())
    limits = Limits(max_bytes=2_000_000, max_total_bytes=2048, max_states=200_000)
    large = {"values": ["x"] * 150_000}
    stopped = check_serialized(
        large, models, usages, vocabulary=owned, budget=Budget(limits)
    )
    assert stopped.report.outcome == "exhausted"
    assert stopped.output_json is None and stopped.document_json is None

    calls: list[dict[str, object]] = []

    def serialize(instance: object, **options: object) -> object:
        """An actual caller callback runs only after options are owned."""
        calls.append(options)
        return {"@type": str(EX.Record)}

    options = check_instance(
        None,
        serialize,
        models,
        usages,
        vocabulary=owned,
        options=large,
        budget=Budget(limits),
    )
    assert options.report.outcome == "exhausted" and calls == []
    assert options.output_json is None and options.document_json is None

    whitespace = check_serialized(
        {"values": ["µ"] * 40},
        models,
        usages,
        vocabulary=owned,
        limits=Limits(max_bytes=240),
    )
    assert whitespace.report.outcome == "exhausted"
    assert whitespace.output_json is None and whitespace.document_json is None

    output = {"@type": str(EX.Record)}
    context = {f"field{index}": str(EX.name) for index in range(30_000)}
    partial = check_serialized(
        output,
        models,
        usages,
        vocabulary=owned,
        context=context,
        budget=Budget(limits),
    )
    assert partial.report.outcome == "exhausted"
    assert partial.output == output and partial.document_json is None
    unicode_output = {
        "@context": {"name": str(EX.name)},
        "@type": str(EX.Record),
        "name": "Name µ\n😀",
    }
    recovered = check_serialized(unicode_output, models, usages, vocabulary=owned)
    assert (
        recovered.report.verified
        and recovered.output == recovered.document == unicode_output
    )
    assert recovered.output_json == json.dumps(
        unicode_output, ensure_ascii=False, allow_nan=False
    )


def test_serializer_diagnostics_bound_owned_text_without_caller_formatting() -> None:
    """Failure reporting cannot retain oversized text or dispatch user hooks."""

    class UnsafeError(ValueError):
        def __str__(self) -> str:
            raise AssertionError("Caller formatting must not run")

    model = ModelDeclaration("record", (str(EX.Record),))
    for error, expected in (
        (ValueError("x" * 100_000), "exhausted"),
        (UnsafeError("ordinary failure"), "input_error"),
        (ValueError(UnsafeError()), "input_error"),
        (ValueError("\ud800"), "input_error"),
        (MemoryError("resource exhausted"), "exhausted"),
    ):

        def failure(
            instance: object, *, error: Exception = error, **options: object
        ) -> object:
            raise error

        checked = check_instance(
            object(),
            failure,
            [model],
            [ModelUsage("record")],
            vocabulary=vocabulary(),
            options={},
            limits=Limits(max_bytes=1000, max_total_bytes=10000),
        )
        assert checked.report.outcome == expected
        assert not checked.report.complete
        assert max(len(issue.message) for issue in checked.report.issues) <= 1000
        if isinstance(error, UnsafeError):
            assert checked.report.issues[0].message == "ordinary failure"


def test_explicit_context_inventory_checks_the_same_joined_output() -> None:
    """Entity contracts and selected source meanings share one usage preparation."""
    model = ModelDeclaration(
        "record",
        (str(EX.Record),),
        (
            FieldDeclaration(
                "name", str(EX.name), (ValueDeclaration("literal", str(XSD.string)),)
            ),
        ),
    )
    for predicate, expected in ((EX.name, True), (EX.note, False)):
        output = {"@id": str(EX.record), "@type": str(EX.Record), "n": "name"}
        result = check_serialized(
            output,
            [model],
            [ModelUsage("record", entity=True)],
            vocabulary=vocabulary(),
            context={"n": str(predicate)},
            context_expectations=(ContextExpectation(("n",), str(EX.name)),),
        )
        assert result.output == output
        assert result.report.verified is expected, result.report.to_dict()


def test_private_prepared_output_preserves_exact_document_and_resource_owner() -> None:
    """A same-owner handoff cannot substitute JSON, base, resources or exclusions."""
    model = ModelDeclaration(
        "record",
        (str(EX.Record),),
        (
            FieldDeclaration(
                "name", str(EX.name), (ValueDeclaration("literal", str(XSD.string)),)
            ),
        ),
    )
    context = {"n": str(EX.name), "extra": str(EX.note)}
    original = {
        "@id": str(EX.record),
        "@type": str(EX.Record),
        "n": "name",
        "extra": True,
    }
    for changed in ("none", "number", "budget", "base", "loader", "drops"):
        operation = Budget()
        prepared = prepare_usages(
            {**original, "@context": context},
            budget=operation,
            allowed_drops=frozenset({"ignored"}) if changed == "drops" else frozenset(),
        )
        output = {**original, "extra": 1} if changed == "number" else original
        result = check_serialized(
            output,
            [model],
            [ModelUsage("record", entity=True)],
            vocabulary=vocabulary(),
            context=context,
            base="urn:different" if changed == "base" else None,
            document_loader=None if changed == "loader" else prepared._loader,
            budget=Budget() if changed == "budget" else operation,
            _prepared=prepared,
        )
        assert result.report.verified is (changed == "none"), result.report.to_dict()
        if changed != "none":
            assert result.report.outcome == "input_error"
