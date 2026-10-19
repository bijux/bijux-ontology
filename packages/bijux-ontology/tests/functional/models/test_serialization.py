"""Actual callable serializer samples retain explicit options and failed contracts."""

from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, Field
from rdflib import Graph, Namespace
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology import (
    FieldDeclaration,
    ModelDeclaration,
    ModelUsage,
    ValueDeclaration,
)
from bijux_ontology.interfaces.adapters.contracts import RdfField
from bijux_ontology.interfaces.adapters.pydantic import model_declarations
from bijux_ontology.models.serialization import check_instance

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
