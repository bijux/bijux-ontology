"""Exact supported R5 source evidence and separately qualified RDF projections."""

from dataclasses import dataclass
from typing import Literal

from rdflib import URIRef

from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.errors import InputError
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.sources.view import Vocabulary

from ..identity import FhirCodeBinding, FhirCodeIdentity


@dataclass(frozen=True)
class FhirDesignation:
    """Original language and Coding qualifiers, beside explicit FHIR fallback."""

    value: str
    language: str | None
    effective_language: str | None
    use_json: str | None = None
    additional_uses_json: tuple[str, ...] = ()


@dataclass(frozen=True)
class FhirPropertyDefinition:
    """Declared property meaning; its URI is not guessed to be an RDF predicate."""

    code: str
    uri: str | None
    kind: str
    description: str | None


@dataclass(frozen=True)
class FhirProperty:
    """One supported original property occurrence, without scalar coercion."""

    code: str
    kind: str
    value: str | int | bool


@dataclass(frozen=True)
class FhirConcept:
    """One exact code, parent occurrence and ordered designation/property values."""

    identity: FhirCodeIdentity
    iri: URIRef
    parent: FhirCodeIdentity | None
    display: str | None
    definition: str | None
    designations: tuple[FhirDesignation, ...]
    properties: tuple[FhirProperty, ...]


@dataclass(frozen=True, init=False)
class FhirCodeSystem:
    """Factory-owned interchange evidence; RDF labels are a consumer projection.

    A supplement's concepts annotate its target rather than declare a new code
    inventory. Hierarchy meaning and qualified designations remain source facts;
    they do not silently become subclass edges or unqualified preferred labels.
    """

    payload: bytes
    document_json: str
    system: str
    version: str | None
    language: str | None
    case_sensitive: bool | None
    case_comparison: Literal["ascii"] | None
    content: str
    supplements: str | None
    hierarchy_meaning: str | None
    property_definitions: tuple[FhirPropertyDefinition, ...]
    concepts: tuple[FhirConcept, ...]
    bindings: tuple[FhirCodeBinding, ...]
    vocabulary: Vocabulary | None
    enumeration: ValueEnumeration
    report: Report

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Bind verified source facts only through the actual native reader."""
        raise InputError("Use read_code_system to acquire a CodeSystem")

    @classmethod
    def _from_owned(cls, **values: object) -> "FhirCodeSystem":
        """Adopt fields already qualified and charged by the reader."""
        result = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(result, name, value)
        return result


@dataclass(frozen=True)
class FhirCodeSystemExport:
    """Native-produced supported JSON, absent when production is unqualified."""

    content: bytes | None
    report: Report


@dataclass(frozen=True)
class FhirCodeMembership:
    """Qualified lexical comparison, preserving request and actual source identity."""

    requested: FhirCodeIdentity
    matched: FhirCodeIdentity | None
    matched_concept: FhirConcept | None
    status: Literal["found", "absent", "unknown"]
    report: Report
