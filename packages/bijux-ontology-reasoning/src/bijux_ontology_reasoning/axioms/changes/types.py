"""Explicit typed axiom transactions and sealed native candidates."""

from collections.abc import Callable
from dataclasses import dataclass, field

from bijux_ontology.limits import Budget
from bijux_ontology.reports import Report

from ...backend.types import BackendCapability
from ...documents.types import NativeProfile, OwlDocument, OwlSyntax
from ..types import ProjectedAxiom


@dataclass(frozen=True)
class AxiomCheck:
    """A trusted named application check sharing the operation allowance."""

    name: str
    check: Callable[[OwlDocument, Budget], Report]


@dataclass(frozen=True)
class AxiomBase:
    """Exact byte integrity and native interpretation context for a planned edit."""

    sha256: str
    format: OwlSyntax
    document_iri: str
    effective_url: str


@dataclass(frozen=True)
class AxiomPatch:
    """Native structural root-axiom differences, distinct from RDF differences."""

    base: AxiomBase | None
    additions: tuple[ProjectedAxiom, ...]
    removals: tuple[ProjectedAxiom, ...]
    capability: BackendCapability | None
    report: Report


@dataclass(frozen=True)
class AxiomCandidate:
    """Reviewable native bytes; only checked, unchanged bytes may be published."""

    source: OwlDocument | None
    original: OwlDocument
    additions: tuple[ProjectedAxiom, ...]
    removals: tuple[ProjectedAxiom, ...]
    profiles: tuple[NativeProfile, ...]
    before: tuple[Report, ...]
    after: tuple[Report, ...]
    capability: BackendCapability | None
    report: Report
    _receipt: tuple[object, ...] | None = field(default=None, repr=False, compare=False)
    _checked_source: OwlDocument | None = field(default=None, repr=False, compare=False)
