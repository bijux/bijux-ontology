"""Qualified validation-fragment translations with original resources retained."""

from dataclasses import dataclass
from typing import Literal

from rdflib import URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.sources.metadata import SnapshotMetadata
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.shacl.authoring.types import ShaclNodeShape, ShaclPropertyShape
from bijux_ontology.shacl.evidence.results import ShaclResult
from bijux_ontology.shacl.inspection.types import ShapeInspection
from bijux_ontology.shex.types import ShexBinding, ShexResult, ShexSchema


@dataclass(frozen=True)
class InterchangeRemainder:
    """A located semantic obligation outside the declared common fragment."""

    location: str
    reason: str


@dataclass(frozen=True)
class ShapePreservation:
    """A fixed one-root map and outgoing-predicate consumption contract.

    Preservation covers validation in the existing native scalar profile, not
    descriptive annotations, other targets, inferred class closure or arbitrary
    shapes. The complete original document remains available on the translation.
    """

    shape: URIRef
    closed: bool
    target_policy: Literal["exact", "pairs"]
    predicates: tuple[URIRef, ...]
    datatypes: tuple[str, ...]


@dataclass(frozen=True, init=False)
class ShapeTranslation:
    """One factory-owned source, compiled resources and exact unchanged shape map."""

    source: PreparedSource | ShexSchema
    direction: Literal["shacl_to_shex", "shex_to_shacl"]
    shapes: PreparedSource | None
    schema: ShexSchema | None
    schema_imports: tuple[ShexSchema, ...]
    bindings: tuple[ShexBinding, ...]
    definitions: tuple[ShaclNodeShape | ShaclPropertyShape, ...]
    inspection: ShapeInspection | None
    preservation: ShapePreservation | None
    shape_metadata: SnapshotMetadata | None
    remainder: tuple[InterchangeRemainder, ...]
    report: Report

    def __init__(self) -> None:
        """Require compilation rather than a caller-manufactured preservation claim."""
        raise InputError("Use shacl_to_shex or shex_to_shacl")

    @classmethod
    def _owned(cls, **values: object) -> "ShapeTranslation":
        """Publish only already qualified immutable operation records."""
        result = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(result, name, value)
        return result


@dataclass(frozen=True)
class InterchangeObligation:
    """Actual raw validations beside one exact pair's comparison outcome."""

    binding: ShexBinding
    shacl: ShaclResult | None
    shex_status: Literal["matched", "not_matched", "unknown"]
    equivalent: bool | None
    report: Report


@dataclass(frozen=True)
class InterchangeCheck:
    """Finite native comparison evidence, with negative data findings preserved."""

    translation: ShapeTranslation
    data_metadata: SnapshotMetadata | None
    shex: ShexResult | None
    obligations: tuple[InterchangeObligation, ...]
    report: Report
