"""Explicit MIREOT boundaries, retention and selected consumer evidence."""

from dataclasses import dataclass
from typing import Literal

from bijux_ontology.diagnostics.reports import Report

from ...axioms.types import AxiomInspection, NamedEntity, ProjectedAxiom
from ...documents.types import OwlDocument
from ...inference.entailment.types import EntailmentCheck
from ...inference.relationships.types import InferredContracts


@dataclass(frozen=True)
class MireotBoundary:
    """One required same-role lower-to-upper hierarchy boundary."""

    lower: NamedEntity
    upper: NamedEntity


@dataclass(frozen=True)
class MireotRetention:
    """Required properties/axioms and explicit entity/root annotation policies.

    Entity annotations on retained entities remain exact native annotated
    axioms. Other logical axioms are required only when explicitly selected;
    MIREOT does not promise locality or universal consequence preservation.
    """

    required_properties: tuple[NamedEntity, ...] = ()
    required_axioms: tuple[ProjectedAxiom, ...] = ()
    entity_annotations: Literal["retain-all"] = "retain-all"
    ontology_annotations: Literal["copy", "omit"] = "copy"


@dataclass(frozen=True)
class MireotImport:
    """Ordinary native/RDF outputs published only after all selected checks pass.

    Original and resulting typed inventories expose the actual syntactic scope.
    Before/after entailments qualify only the selected consumers. No locality,
    conservative-extension or ontology-wide equivalence claim is made.
    """

    source: OwlDocument | None
    rdf_content: bytes | None
    original: OwlDocument | None
    boundaries: tuple[MireotBoundary, ...]
    retention: MireotRetention | None
    original_inspection: AxiomInspection | None
    inspection: AxiomInspection | None
    rdf_inspection: AxiomInspection | None
    original_hierarchy: InferredContracts | None
    hierarchy: InferredContracts | None
    before: EntailmentCheck | None
    after: EntailmentCheck | None
    native_identity: str | None
    report: Report
