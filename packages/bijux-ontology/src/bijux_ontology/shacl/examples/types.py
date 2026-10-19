"""Concrete checked examples and explicitly named missing-value counterexamples."""

from dataclasses import dataclass
from typing import Literal

from rdflib import BNode, URIRef

from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.shacl.analysis.types import ShapeAnalysis
from bijux_ontology.shacl.execution.results import ShaclResult
from bijux_ontology.shacl.inspection.types import ShapeInspection


@dataclass(frozen=True)
class MissingValueExample:
    """Violate a named direct property MinCount while checking named siblings.

    Every name refers to the original root's direct property shapes. The full
    original root/focus is also checked and must actually fail this obligation.
    """

    shape: URIRef
    preserve: tuple[URIRef, ...] = ()


@dataclass(frozen=True)
class ShapeExample:
    """One concrete dataset and unchanged original binding's native raw outcome."""

    source: PreparedSource
    validation: ShaclResult
    preserved: tuple[ShaclResult, ...] = ()


@dataclass(frozen=True)
class ExampleGeneration:
    """Construction status never replaces any example's actual SHACL truth.

    Trials retain native rejected candidates' raw results without claiming those
    data conform. A found negative example remains negative in both its native
    result and the aggregate report. Finite pool/search failure is never a
    general SHACL unsatisfiability certificate.
    """

    status: Literal[
        "found", "search_exhausted", "unsupported", "input_error", "exhausted"
    ]
    shapes: PreparedSource
    root: URIRef | BNode
    focus: URIRef
    seed: PreparedSource
    positive: ShapeExample | None
    negative: ShapeExample | None
    trials: tuple[ShaclResult, ...]
    inspection: ShapeInspection | None
    analysis: ShapeAnalysis | None
    report: Report
    shapes_metadata: SnapshotMetadata | None
    seed_metadata: SnapshotMetadata | None
