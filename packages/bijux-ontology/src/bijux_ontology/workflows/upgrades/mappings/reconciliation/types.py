"""Reviewable exact duplicate groups and explicit mapping approval intent."""

from dataclasses import dataclass

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.mappings.composition.types import MappingOccurrence
from bijux_ontology.vocabulary.mappings.types import (
    MappingPolicy,
    MappingRead,
    MappingSet,
)


@dataclass(frozen=True)
class MappingGroup:
    """Identical row content and interpretation context, with every source witness."""

    occurrences: tuple[MappingOccurrence, ...]


@dataclass(frozen=True, init=False)
class MappingReconciliation:
    """Captured source inventory and a native candidate's actual policy findings.

    inventory_complete is distinct from policy agreement: the candidate may have
    real conflicts that require an explicit resolution. All source metadata,
    release identities and licenses remain on the original occurrence witnesses.
    """

    sources: tuple[MappingSet, ...]
    groups: tuple[MappingGroup, ...]
    policy: MappingPolicy
    subject_vocabulary: VocabularyView
    object_vocabulary: VocabularyView
    production: MappingRead | None
    inventory_complete: bool
    report: Report

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise InputError("Use reconcile_mappings to capture a reconciliation")

    @classmethod
    def _owned(cls, **values: object) -> "MappingReconciliation":
        result = object.__new__(cls)
        for key, value in values.items():
            object.__setattr__(result, key, value)
        return result


@dataclass(frozen=True)
class MappingResolution:
    """Approve exactly these group positions; every omitted group is rejected.

    Positions belong to the supplied reconciliation object, not another release
    or regenerated plan. No confidence threshold or implicit winner is applied.
    """

    reconciliation: MappingReconciliation
    retain: tuple[int, ...]
    approved_by: str
    reason: str


@dataclass(frozen=True)
class ReconciledMappings:
    """Original review evidence, explicit choice and independently checked output."""

    reconciliation: MappingReconciliation
    resolution: MappingResolution
    production: MappingRead | None
    report: Report

    @property
    def mapping_set(self) -> MappingSet | None:
        """Expose approved output only after native and application checks pass."""
        return (
            self.production.mapping_set
            if self.report.verified and self.production
            else None
        )
