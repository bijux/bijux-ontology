"""Owned output encodings, explicit translation status and generated artifacts."""

import json
from dataclasses import dataclass
from typing import Literal

from bijux_ontology.errors import InputError
from bijux_ontology.interfaces.adapters.jsonschema.types import SchemaModel
from bijux_ontology.limits import Budget, Limits, bound_text
from bijux_ontology.reports import Report

from .validation import validate_output


@dataclass(frozen=True)
class JsonRepresentation:
    """Select a scalar, ordinary array, or canonical explicit @list wrapper."""

    model: str
    field: str
    shape: Literal["scalar", "array", "list"] = "scalar"

    def __post_init__(self) -> None:
        """Require named finite representation intent without inferring RDF meaning."""
        budget = Budget()
        for text in (self.model, self.field):
            if not isinstance(text, str) or not text:
                raise InputError("JSON representation requires model and field names")
            bound_text(text, budget)
        if self.shape not in ("scalar", "array", "list"):
            raise InputError("Select scalar, array or list JSON representation")


@dataclass(frozen=True)
class SchemaTranslation:
    """One authored obligation and its qualified JSON-only preservation status."""

    model: str
    field: str | None
    obligation: str
    status: Literal["preserved", "strengthened", "weakened", "unsupported"]
    detail: str


@dataclass(frozen=True)
class CompiledOutputSchema:
    """Usable JSON artifact and separate required RDF-equivalence qualification.

    JSON conformance proves only this selected native representation contract.
    It does not certify RDF typing, arbitrary contexts or every serializer call.
    Mappings/annotations are a linked sidecar; the unchanged import adapter can
    round-trip the documented single-model/local-definitions/native-scalar subset.
    """

    schema_json: str | None
    annotations_json: str
    mappings: tuple[SchemaModel, ...]
    translations: tuple[SchemaTranslation, ...]
    report: Report

    @property
    def schema(self) -> object:
        """Export an independent JSON validation artifact, when compilation finished."""
        return json.loads(self.schema_json) if self.schema_json is not None else None

    @property
    def annotations(self) -> object:
        """Export independent ontology annotations without inventing JSON assertions."""
        return json.loads(self.annotations_json)

    def validate(
        self,
        output: object,
        *,
        limits: Limits | None = None,
        budget: Budget | None = None,
    ) -> Report:
        """Validate actual JSON independently of untranslated RDF obligations."""
        return validate_output(self.schema_json, output, limits=limits, budget=budget)
