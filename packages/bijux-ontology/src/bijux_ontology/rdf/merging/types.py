"""Exact branch edits, explicit conflict decisions and owned three-way plans."""

from dataclasses import dataclass
from typing import Literal

from rdflib import BNode

from bijux_ontology.errors import InputError
from bijux_ontology.rdf.migrations.candidates import MigrationCandidate
from bijux_ontology.rdf.patches.types import (
    BlankNodePair,
    PatchBlankBinding,
    PatchQuad,
    PatchResource,
    SnapshotWitness,
)
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.reports import Report


@dataclass(frozen=True)
class MergeCorrespondence:
    """Explicit retained/new/removed nodes from the actual branch snapshots."""

    pairs: tuple[BlankNodePair, ...] = ()
    created: tuple[BNode, ...] = ()
    removed: tuple[BNode, ...] = ()


@dataclass(frozen=True)
class MergeChanges:
    """Original exact portable quad and physical graph inventory changes."""

    removals: tuple[PatchQuad, ...] = ()
    additions: tuple[PatchQuad, ...] = ()
    removed_graphs: tuple[PatchResource, ...] = ()
    added_graphs: tuple[PatchResource, ...] = ()


@dataclass(frozen=True)
class MergeConflict:
    """One connected overlap; its identifier belongs only to this exact plan."""

    identifier: int
    kind: Literal["property", "list", "graph"]
    left: MergeChanges
    right: MergeChanges


@dataclass(frozen=True)
class MergeResolution:
    """Choose one entire conflicting component, preserving unrelated changes."""

    conflict: int
    choice: Literal["base", "left", "right"]

    def __post_init__(self) -> None:
        """Reject ambiguous identifiers and implicit or unrecognized decisions."""
        if type(self.conflict) is not int or self.conflict < 0:
            raise InputError("Merge conflict identifiers require nonnegative integers")
        if self.choice not in ("base", "left", "right"):
            raise InputError("Merge resolutions choose base, left or right")


@dataclass(frozen=True)
class _Component:
    """A captured independent edit domain, with an optional explicit conflict."""

    left: MergeChanges
    right: MergeChanges
    conflict: int | None


@dataclass(frozen=True, init=False)
class RdfMergePlan:
    """Factory-owned finite plan retaining raw conflicts and exact base evidence."""

    base: SnapshotWitness | None
    conflicts: tuple[MergeConflict, ...]
    report: Report
    _components: tuple[_Component, ...]
    _owner: PreparedSource
    _bindings: tuple[PatchBlankBinding, ...]
    _ready: bool

    def __init__(self) -> None:
        """Only actual three-way planning can establish common-base ownership."""
        raise InputError("RDF merge plans must come from plan_rdf_merge")

    @classmethod
    def _from_planning(
        cls,
        base: SnapshotWitness | None,
        conflicts: tuple[MergeConflict, ...],
        report: Report,
        components: tuple[_Component, ...],
        owner: PreparedSource,
        bindings: tuple[PatchBlankBinding, ...],
        ready: bool,
    ) -> "RdfMergePlan":
        """Freeze already bounded evidence, including a completed conflict prefix."""
        result = object.__new__(cls)
        for name, value in (
            ("base", base),
            ("conflicts", conflicts),
            ("report", report),
            ("_components", components),
            ("_owner", owner),
            ("_bindings", bindings),
            ("_ready", ready),
        ):
            object.__setattr__(result, name, value)
        return result


@dataclass(frozen=True)
class RdfMergeResult:
    """Raw plan history stays separate from resolved structural/application truth."""

    plan: RdfMergePlan
    resolutions: tuple[MergeResolution, ...]
    candidate: MigrationCandidate | None
    structural_report: Report
    report: Report
