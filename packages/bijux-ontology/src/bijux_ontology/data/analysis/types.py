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
