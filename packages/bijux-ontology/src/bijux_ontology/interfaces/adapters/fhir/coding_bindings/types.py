"""Explicit field bindings and separate FHIR conformance/recommendation evidence."""

from dataclasses import dataclass, field
from typing import Literal

from bijux_ontology._jsonpaths import SourcePath
from bijux_ontology.interfaces.adapters.fhir.code_system.types import (
    FhirCodeMembership,
    FhirCodeSystem,
)
from bijux_ontology.interfaces.adapters.fhir.identity import FhirCodeIdentity
from bijux_ontology.interfaces.adapters.fhir.value_set.types import FhirValueSet
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.results import Annotation, Selection


@dataclass(frozen=True)
class FhirDisplayPolicy:
    """Select all defined labels or RFC4647 language lookup with explicit fallback.

    Designation use qualifiers stay in the original matched CodeSystem concept.
    Neither a ValueSet display nor CodeableConcept.text replaces Coding.display.
    """

    mode: Literal["ignore", "defined", "language"] = "ignore"
    ranges: tuple[str, ...] = ()
    fallback: tuple[Literal["untagged", "other"], ...] = ()
    require_display: bool = False


@dataclass(frozen=True)
class FhirCodingBinding:
    """One declared field, datatype, binding and independent occurrence policy.

    Exact names traverse dictionaries, dataclasses or ordinary Pydantic models.
    Arrays at any selected step fan out, preserving every concrete index. Minimum
    occurrences apply per selected parent; strength alone never requires an
    otherwise absent optional field. The reference is an exact R5 canonical URL.
    """

    path: tuple[str, ...]
    datatype: Literal["code", "Coding", "CodeableConcept"]
    strength: Literal["required", "extensible", "preferred", "example"]
    value_set: FhirValueSet
    value_set_reference: str
    mode: Literal["compose", "expansion"] = "expansion"
    external_value_set: bool = True
    minimum_occurrences: int = 0
    maximum_occurrences: int | None = None
    display: FhirDisplayPolicy = field(default_factory=FhirDisplayPolicy)


@dataclass(frozen=True)
class FhirExtensibilityDecision:
    """Caller clinical judgment bound to the exact occurrence and original value.

    representable=False means that no bound concept applies; True means that an
    in-set concept is applicable and must be used. The checker never establishes
    the justification's clinical correctness or reuses it for changed input.
    """

    path: SourcePath
    value_set_reference: str
    value_json: str
    representable: bool
    justification: str


@dataclass(frozen=True)
class FhirCodingEvidence:
    """Original coding, exact identity and independent optional display selection."""

    path: SourcePath
    source_json: str
    identity: FhirCodeIdentity | None
    membership: FhirCodeMembership | None
    display: Selection[Annotation] | None
    identity_report: Report
    display_report: Report
    code_system: FhirCodeSystem | None


@dataclass(frozen=True)
class FhirBindingOccurrence:
    """One actual element repetition with datatype-specific binding truth."""

    path: SourcePath
    datatype: str
    source_json: str
    codings: tuple[FhirCodingEvidence, ...]
    conforms: bool | None
    binding_report: Report
    binding: FhirCodingBinding


@dataclass(frozen=True)
class FhirCodingBindingCheck:
    """Required checks are separate from non-required recommendation evidence."""

    occurrences: tuple[FhirBindingOccurrence, ...]
    report: Report
    recommendations: Report
    preparation: tuple[Report, ...]
