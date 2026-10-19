"""Owned original native documents and loss-qualified maintained writer results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report

from ..backend.types import BackendCapability, OwlProfile

OwlSyntax = Literal["owlxml", "functional", "manchester", "obo"]


@dataclass(frozen=True, init=False)
class OwlDocument:
    """Captured original bytes with explicit syntax and effective resolution base."""

    content: bytes
    format: OwlSyntax
    document_iri: str
    effective_url: str
    sha256: str

    def __init__(self) -> None:
        raise InputError("Use prepare_owl_document to capture original native bytes")

    @classmethod
    def _owned(cls, **values: object) -> OwlDocument:
        """Adopt fields qualified by the bounded native document factory."""
        result = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(result, name, value)
        return result


@dataclass(frozen=True)
class NativeOntology:
    """One actually loaded document, with original syntax bytes still inspectable."""

    source: OwlDocument
    ontology_iri: str | None
    version_iri: str | None
    imports: tuple[str, ...]
    axiom_count: int


@dataclass(frozen=True)
class NativeProfile:
    """A maintained profile observation on the selected parsed native closure."""

    profile: OwlProfile
    in_profile: bool
    violations: tuple[str, ...]


@dataclass(frozen=True)
class ConversionChange:
    """An actual maintained structural difference or unsupported OBO axiom."""

    kind: str
    value: str


@dataclass(frozen=True)
class NativeExport:
    """Bounded output and preservation truth, distinct from exact source/RDF identity.

    Only a complete lossless conversion returns content under loss='reject'.
    Explicit loss='report' permits a qualified lossy projection while its report
    retains findings. Lossless means equality of maintained parsed annotated
    axioms and ontology headers. Neither policy proves exact original/native
    serialization or RDF lexical preservation.
    """

    format: OwlSyntax
    content: bytes | None
    lossless: bool | None
    documents: tuple[NativeOntology, ...]
    changes: tuple[ConversionChange, ...]
    before_profiles: tuple[NativeProfile, ...]
    after_profiles: tuple[NativeProfile, ...]
    loader_profile: str | None
    capability: BackendCapability | None
    report: Report
