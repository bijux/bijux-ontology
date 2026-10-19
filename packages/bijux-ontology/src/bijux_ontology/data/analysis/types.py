"""Exact supplied annotation evidence and owned finite analytical populations."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from rdflib import URIRef

from bijux_ontology.errors import InputError
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.relation_paths.types import RelationWitness


@dataclass(frozen=True)
class EntityAnnotation:
    """One supplied positive annotation; duplicates are evidence, not extra weight."""

    entity: str
    term: URIRef


@dataclass(frozen=True)
class PopulationIdentity:
    """Actual counted inputs accompany the caller's corpus and snapshot identity."""

    corpus_id: str
    annotation_snapshot_id: str
    background_fingerprint: str
    annotation_fingerprint: str
    hierarchy_fingerprint: str
    hierarchy_regime: Literal["asserted", "caller-inferred"]
    counting: str = "distinct-eligible-entity/reflexive-selected-ancestor"


@dataclass(frozen=True, init=False, eq=False)
class AnnotationPopulation:
    """Immutable qualified finite memberships, never an OWL closure claim."""

    identity: PopulationIdentity
    terms: tuple[URIRef, ...]
    background: tuple[str, ...]
    annotations: tuple[EntityAnnotation, ...]
    duplicate_annotations: int
    witnesses: tuple[RelationWitness, ...]
    ancestors: Mapping[URIRef, frozenset[URIRef]]
    members: Mapping[URIRef, frozenset[str]]
    unannotated: tuple[str, ...]
    report: Report

    def __init__(self) -> None:
        raise InputError("Use prepare_annotation_population to qualify annotations")

    @classmethod
    def _owned(cls, **values: object) -> "AnnotationPopulation":
        result = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(result, name, value)
        return result


@dataclass(frozen=True)
class PopulationRead:
    """A failed preparation retains summaries but cannot authorize analysis."""

    population: AnnotationPopulation | None
    report: Report
    unknown_annotations: tuple[EntityAnnotation, ...] = ()
    unannotated: tuple[str, ...] = ()


@dataclass(frozen=True)
class AncestorInformation:
    """A most-informative shared ancestor and its distinct entity frequency."""

    term: URIRef
    count: int
    background_count: int
    information: float


@dataclass(frozen=True)
class TermSimilarity:
    """All tied Resnik witnesses under one exact counted population."""

    left: URIRef | None
    right: URIRef | None
    score: float | None
    ancestors: tuple[AncestorInformation, ...]
    identity: PopulationIdentity | None
    report: Report
    method: str = "resnik/entity-frequency/base2"


@dataclass(frozen=True)
class TermSetSimilarity:
    """Complete pair evidence and an explicitly named symmetric aggregation."""

    left: tuple[URIRef, ...]
    right: tuple[URIRef, ...]
    score: float | None
    pairs: tuple[TermSimilarity, ...]
    left_best: tuple[tuple[URIRef, URIRef], ...]
    right_best: tuple[tuple[URIRef, URIRef], ...]
    identity: PopulationIdentity | None
    report: Report
    aggregation: str = "symmetric-mean-directional-best"


@dataclass(frozen=True)
class EnrichmentCounts:
    """Auditable fixed-universe contingency counts for a prespecified term."""

    term: URIRef
    study_and_term: int
    study_count: int
    term_count: int
    background_count: int
    table: tuple[tuple[int, int], tuple[int, int]]


@dataclass(frozen=True)
class EnrichmentTerm:
    """Actual maintained-library p and whole-family adjusted p, without rounding."""

    counts: EnrichmentCounts
    raw_p: float
    adjusted_p: float


@dataclass(frozen=True)
class EnrichmentIdentity:
    """Counted population, study, fixed family and actual numerical method."""

    population: PopulationIdentity
    study_fingerprint: str
    family_id: str
    family_fingerprint: str
    correction: str
    dependence: str
    outside_background: str
    scipy_version: str
    numpy_version: str
    test: str = "fisher-exact/greater/fixed-margins"
    sampling: str = "supplied-snapshot/eligible-fixed-universe/fixed-margins"


@dataclass(frozen=True)
class EnrichmentRead:
    """No adjusted values are published from a partial or failed family."""

    tests: tuple[EnrichmentTerm, ...]
    counts: tuple[EnrichmentCounts, ...]
    identity: EnrichmentIdentity | None
    excluded: tuple[str, ...]
    unannotated_study: tuple[str, ...]
    report: Report
