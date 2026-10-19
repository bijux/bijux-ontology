"""Bounded support for one already qualified logical observation."""

from dataclasses import dataclass
from typing import Literal

from bijux_ontology.diagnostics.reports import Report

from ...axioms.types import ProjectedAxiom
from ...backend.types import BackendCapability
from ...documents.types import OwlDocument
from ..consistency.types import ConsistencyCheck
from ..entailment.types import EntailmentCheck


@dataclass(frozen=True)
class ExplanationQuestion:
    """Select one entailment, named-class unsatisfiability or inconsistency."""

    kind: Literal["entailment", "unsatisfiability", "inconsistency"]
    axiom: ProjectedAxiom | None = None
    class_iri: str | None = None
    location: str = ""


@dataclass(frozen=True)
class SourceAxiom:
    """An actual annotated structural axiom in an owned source module.

    Rendering hashes identify this pinned OWLAPI structural rendering, not a
    general logical or RDF canonicalization. Duplicate identical physical source
    occurrences are deduplicated by OWLAPI.
    Multiple annotated structures can support one logical axiom; no uniquely
    responsible occurrence or source coordinate is inferred.
    """

    document: OwlDocument
    rendering: str
    sha256: str


@dataclass(frozen=True)
class AxiomSupport:
    """One logical support axiom and established original source structures."""

    rendering: str
    sha256: str
    origins: tuple[SourceAxiom, ...]


@dataclass(frozen=True)
class Justification:
    """Standalone checked proof bytes and independently reproduced support.

    Signature declarations in the standalone document are replay scaffolding,
    separate from its logical support. Neither smallest size nor subset
    minimality is claimed; bounded selection never claims complete enumeration.
    """

    source: OwlDocument
    axioms: tuple[AxiomSupport, ...]
    declarations: tuple[str, ...]
    reproduced: bool
    minimality: Literal["neither"]
    enumeration_complete: bool
    report: Report


@dataclass(frozen=True)
class ExplanationCheck:
    """Keep original truth even if explanation generation cannot complete."""

    question: ExplanationQuestion | None
    underlying: ConsistencyCheck | EntailmentCheck | None
    applicable: bool | None
    justification: Justification | None
    generator_version: str | None
    capability: BackendCapability | None
    explanation_report: Report
    report: Report
