"""Signed GO assertion records retain exact source fields and grouped evidence."""

from dataclasses import dataclass
from typing import Literal

from bijux_ontology.data.foreign_references.types import ForeignReferenceResult
from bijux_ontology.declarations.types import TermDeclaration
from bijux_ontology.errors import InputError
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.obo.identifiers import AlternativeIdResolution
from bijux_ontology.vocabulary.results import Deprecation

GoFormat = Literal["gaf", "gpad"]
GoRole = Literal[
    "term", "relation", "evidence", "taxon", "extension_term", "extension_relation"
]


@dataclass(frozen=True)
class GoAnnotationDocument:
    """Explicit text and source identity, never a location to retrieve."""

    source: str
    content: str
    format: GoFormat


@dataclass(frozen=True)
class GoGeneProductDocument:
    """The caller's explicitly paired GPI 2.0 resource."""

    source: str
    content: str


@dataclass(frozen=True)
class GoExtension:
    """One original relation and target within an ordered conjunction."""

    relation: str
    target: str


@dataclass(frozen=True)
class GoEvidence:
    """A per-assertion evidence identifier, citations and grouped with/from."""

    code: str
    kind: Literal["gaf_code", "eco"]
    references: tuple[str, ...]
    with_from: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class GoGeneProduct:
    """One GPI source record, without collapsing distinct duplicate records."""

    source: str
    line: int
    identifier: str
    taxon: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class GoAnnotation:
    """One signed assertion; classes are never asserted as relation individuals."""

    source: str
    line: int
    format: GoFormat
    subject: str
    subject_form: str | None
    relation: str
    term: str
    negated: bool
    evidence: GoEvidence
    entity_taxon: str | None
    interacting_taxa: tuple[str, ...]
    extensions: tuple[tuple[GoExtension, ...], ...]
    date: str
    assigned_by: str
    fields: tuple[str, ...]
    entity: GoGeneProduct | None = None


@dataclass(frozen=True, init=False)
class GoAnnotations:
    """Factory-owned parsed documents and their required entity-join evidence."""

    source: GoAnnotationDocument
    gpi: GoGeneProductDocument | None
    headers: tuple[str, ...]
    annotations: tuple[GoAnnotation, ...]
    gene_products: tuple[GoGeneProduct, ...]
    join: ForeignReferenceResult | None
    report: Report

    def __init__(self) -> None:
        """Require bounded parsing and actual companion-resource qualification."""
        raise InputError("Use read_go_annotations")

    @classmethod
    def _owned(cls, **values: object) -> "GoAnnotations":
        """Publish only records captured by the finite resource reader."""
        result = object.__new__(cls)
        for key, value in values.items():
            object.__setattr__(result, key, value)
        return result


@dataclass(frozen=True)
class GoAnnotationPolicy:
    """Consumer-selected vocabulary roles and lifecycle/alternative-ID choices."""

    roles: tuple[GoRole, ...] = ("term",)
    allow_deprecated: bool = False
    alternative_ids: bool = False


@dataclass(frozen=True)
class GoReference:
    """One record-qualified vocabulary use, preserving original spelling."""

    annotation: GoAnnotation
    role: GoRole
    original: str
    iri: str | None
    alternative: AlternativeIdResolution | None = None
    deprecation: Deprecation | None = None


@dataclass(frozen=True)
class GoAnnotationInventory:
    """Ordinary declarations and per-record uses with required native reports."""

    references: tuple[GoReference, ...]
    declarations: tuple[TermDeclaration, ...]
    report: Report
    policy: GoAnnotationPolicy


@dataclass(frozen=True)
class GoAnnotationExport:
    """Complete text or RDF record output, withheld on incomplete conversion."""

    content: str | None
    rdf: PreparedSource | None
    report: Report
