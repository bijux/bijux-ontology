"""Owned baseline evidence and actual incremental revalidation outcomes."""

from dataclasses import dataclass
from typing import Literal

from rdflib import URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Limits
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.transforms import Transformation
from bijux_ontology.shacl.coverage import ShaclTarget
from bijux_ontology.shacl.execution.results import ShaclResult


@dataclass(frozen=True, init=False)
class IncrementalState:
    """Bind actual fixed resources, current data and frozen completed evidence.

    Only preparation/revalidation creates states. Mutable raw ShaclResult views
    are never the source of a later reuse decision. A failed complete result may
    be retained, but always forces full revalidation on the next patch.
    """

    source: PreparedSource
    shapes: PreparedSource
    ontology: PreparedSource | None
    roots: tuple[str, ...]
    targets: tuple[ShaclTarget, ...]
    target_predicates: frozenset[URIRef]
    local: bool
    trusted_sparql: bool
    limits: Limits
    report: Report

    def __init__(self, **_fields: object) -> None:
        """Require an actual maintained baseline rather than supplied assertions."""
        raise InputError("Incremental states require prepare_incremental")

    @classmethod
    def _owned(
        cls,
        *,
        source: PreparedSource,
        shapes: PreparedSource,
        ontology: PreparedSource | None,
        roots: tuple[str, ...],
        targets: tuple[ShaclTarget, ...],
        target_predicates: frozenset[URIRef],
        local: bool,
        trusted_sparql: bool,
        limits: Limits,
        report: Report,
    ) -> "IncrementalState":
        """Capture only internally qualified resources and immutable metadata."""
        result = object.__new__(cls)
        for name, value in (
            ("source", source),
            ("shapes", shapes),
            ("ontology", ontology),
            ("roots", roots),
            ("targets", targets),
            ("target_predicates", target_predicates),
            ("local", local),
            ("trusted_sparql", trusted_sparql),
            ("limits", limits),
            ("report", report),
        ):
            object.__setattr__(result, name, value)
        return result


@dataclass(frozen=True)
class IncrementalPreparation:
    """Initial actual selected-target validation and any reusable owned state."""

    state: IncrementalState | None
    result: ShaclResult | None
    report: Report


@dataclass(frozen=True)
class IncrementalResult:
    """Checked patch, raw executions and explicit reuse/recheck pair accounting."""

    state: IncrementalState | None
    application: Transformation | None
    results: tuple[ShaclResult, ...]
    strategy: Literal["local", "full"]
    checked_pairs: int
    reused_pairs: int
    total_pairs: int
    report: Report
