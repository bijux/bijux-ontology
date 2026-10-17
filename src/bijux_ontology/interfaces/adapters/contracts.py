"""Plain RDF metadata shared by optional application-model adapters."""

from dataclasses import dataclass

from bijux_ontology.declarations.types import (
    Emission,
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.errors import InputError
from bijux_ontology.reports import Report


@dataclass(frozen=True)
class RdfField:
    """An intended RDF field and explicit knowledge of its actual serializer.

    ``output_contract=True`` confirms that values describe the real output,
    including any custom field or model serializer. Required Python fields and
    defaults do not establish emitted RDF occurrences.
    """

    predicate: str
    values: tuple[ValueDeclaration, ...]
    reverse: bool = False
    emission: Emission = "unknown"
    output_contract: bool = False

    def __post_init__(self) -> None:
        field = FieldDeclaration(
            "field",
            self.predicate,
            self.values,
            reverse=self.reverse,
            emission=self.emission,
        )
        if type(self.output_contract) is not bool:
            raise InputError("output_contract must be a boolean")
        object.__setattr__(self, "values", field.values)


@dataclass(frozen=True)
class ModelExtraction:
    """Explicit declarations together with retained incomplete extraction results."""

    declarations: tuple[ModelDeclaration, ...]
    report: Report

    def __post_init__(self) -> None:
        declarations = tuple(self.declarations)
        if any(not isinstance(model, ModelDeclaration) for model in declarations):
            raise InputError("Expected model declarations")
        if not isinstance(self.report, Report):
            raise InputError("Expected an extraction report")
        identities: dict[str, ModelDeclaration] = {}
        for model in declarations:
            if model.identifier in identities and identities[model.identifier] != model:
                raise InputError(
                    "Conflicting extracted model identity", location=model.identifier
                )
            identities[model.identifier] = model
        object.__setattr__(self, "declarations", tuple(identities.values()))
