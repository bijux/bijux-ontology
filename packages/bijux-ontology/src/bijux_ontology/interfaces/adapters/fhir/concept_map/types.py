"""Original R5 mapping occurrences and explicit local translation results."""

from dataclasses import dataclass
from typing import Literal

from bijux_ontology.errors import InputError
from bijux_ontology.reports import Report

from ..identity import FhirCodeIdentity
from ..value_set.types import FhirValueSet


@dataclass(frozen=True)
class FhirMapValue:
    """A declared native FHIR choice kind and its exact original JSON value."""

    kind: str
    value_json: str


@dataclass(frozen=True)
class FhirMapContext:
    """One explicitly supplied additional attribute, without string coercion."""

    attribute: str
    value: FhirMapValue


@dataclass(frozen=True)
class FhirMapCondition:
    """One ordered dependsOn/product occurrence and its original interpretation."""

    attribute: str
    value: FhirMapValue | None
    value_set: str | None
    metadata_json: str


@dataclass(frozen=True)
class FhirMapTarget:
    """One directed target; negative relationships remain explicit occurrences."""

    code: str | None
    value_set: str | None
    relationship: str
    conditions: tuple[FhirMapCondition, ...]
    products: tuple[FhirMapCondition, ...]
    metadata_json: str
    path: tuple[str | int, ...]


@dataclass(frozen=True)
class FhirMapElement:
    """An original source code or ValueSet selector, never inferred coverage."""

    code: str | None
    value_set: str | None
    no_map: bool
    targets: tuple[FhirMapTarget, ...]
    path: tuple[str | int, ...]


@dataclass(frozen=True)
class FhirMapGroup:
    """Exact R5 canonical CodeSystem references, including optional |version."""

    source: str | None
    target: str | None
    elements: tuple[FhirMapElement, ...]
    unmapped_json: str | None
    path: tuple[str | int, ...]


@dataclass(frozen=True, init=False)
class FhirConceptMap:
    """Factory-owned native-qualified source; interchange is not translation."""

    payload: bytes
    document_json: str
    attributes: tuple[tuple[str, str], ...]
    source_scope: str | None
    target_scope: str | None
    groups: tuple[FhirMapGroup, ...]
    report: Report

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Require actual source capture and maintained R5 model qualification."""
        raise InputError("Use read_concept_map with an actual R5 resource")

    @classmethod
    def _owned(cls, **values: object) -> "FhirConceptMap":
        """Adopt only immutable fields already paid by the owned reader."""
        result = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(result, name, value)
        return result


@dataclass(frozen=True)
class FhirMapValueSet:
    """An original supplied ValueSet and its explicitly chosen finite mode."""

    value_set: FhirValueSet
    mode: Literal["compose", "expansion"]


@dataclass(frozen=True)
class FhirMapMatch:
    """One applicable original mapping; selection/migration remains the caller's."""

    source: FhirCodeIdentity
    target: FhirCodeIdentity | None
    relationship: str
    conditions: tuple[FhirMapCondition, ...]
    products: tuple[FhirMapCondition, ...]
    path: tuple[str | int, ...]
    fallback: bool = False


@dataclass(frozen=True)
class FhirTranslation:
    """All established targets beside negative/noMap and unresolved coverage."""

    requested: FhirCodeIdentity
    matches: tuple[FhirMapMatch, ...]
    negative_matches: tuple[FhirMapMatch, ...]
    no_map: tuple[tuple[str | int, ...], ...]
    status: Literal["translated", "negative", "no_map", "unresolved", "not_applicable"]
    report: Report

    @property
    def successful(self) -> bool:
        """Only a complete answer with a positive target is successful translation."""
        return (
            self.status == "translated" and bool(self.matches) and self.report.verified
        )


@dataclass(frozen=True)
class FhirConceptMapExport:
    """Native JSON or explicitly partial SSSOM, with original loss witnesses."""

    content: bytes | None
    omitted: tuple[tuple[str | int, ...], ...]
    report: Report
