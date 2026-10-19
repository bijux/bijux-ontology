"""Exact signature selection, original annotated axiom owners and consumer proofs."""

from dataclasses import dataclass
from typing import Literal

from bijux_ontology.reports import Report

from ..axioms.types import NamedEntity, ProjectedAxiom
from ..backend.types import BackendCapability
from ..documents.types import OwlDocument
from ..entailment.types import EntailmentCheck

LocalityMethod = Literal["BOT", "TOP", "STAR"]


@dataclass(frozen=True)
class LocalityAxiom:
    """One retained native structural axiom and its original document indexes.

    Identical annotated axioms in distinct loaded ontologies retain both owners;
    OWLAPI axiom-set identity does not retain duplicate physical text occurrences.
    """

    axiom: ProjectedAxiom
    documents: tuple[int, ...]


@dataclass(frozen=True)
class LocalityModule:
    """Checked standalone module with the exact declared preservation boundary.

    Imports are explicitly flattened from the owned selected closure. Native
    module enrichment retains relevant entity/axiom annotations; root ontology
    annotations are copied. Imported ontology headers remain in originals rather
    than becoming annotations of the derived ontology. Only the selected actual
    consumer proofs are checked; this is no universal conservativity certificate.
    """

    source: OwlDocument | None
    originals: tuple[OwlDocument, ...]
    signature: tuple[NamedEntity, ...]
    method: LocalityMethod | None
    axioms: tuple[LocalityAxiom, ...]
    before: EntailmentCheck | None
    after: EntailmentCheck | None
    capability: BackendCapability | None
    report: Report
