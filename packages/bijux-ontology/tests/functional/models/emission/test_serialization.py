"""Actual callable serializer samples retain explicit options and failed contracts."""

from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, Field
from rdflib import Graph, Namespace
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology import (
    FieldDeclaration,
    Limits,
    ModelDeclaration,
    ModelUsage,
    ValueDeclaration,
    check_emission,
    check_models,
    load_vocabulary,
)
from bijux_ontology.interfaces.adapters.contracts import RdfField
from bijux_ontology.interfaces.adapters.pydantic import model_declarations
from bijux_ontology.jsonld.checking import prepare_usages
from bijux_ontology.jsonld.expectations import ContextExpectation
from bijux_ontology.limits import Budget
from bijux_ontology.models.emission.serialization import (
    check_instance,
    check_serialized,
)

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
    assert check_emission(document, [model], usages, limits=limits).verified
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
    assert exhausted.output == document
    fresh = check_instance(
        None,
        lambda value: document,
        [model],
        usages,
        vocabulary=owned,
        options={},
        limits=Limits(max_work=120),
    )
    assert fresh.report.verified, fresh.report.to_dict()


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
