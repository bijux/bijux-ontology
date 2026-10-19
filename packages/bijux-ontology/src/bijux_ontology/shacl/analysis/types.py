"""Finite incompatibility proofs and exact signed shape dependencies."""

from dataclasses import dataclass
from typing import Literal

from rdflib.term import Node

from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.shacl.profiles.types import ShapeDependency, Triple


@dataclass(frozen=True)
class LocalConflict:
    """Constraints on one active shape cannot hold together for an invocation."""

    shape: Node
    reason: Literal["count_bounds", "fixed_count", "fixed_allowed", "allowed_count"]
    witnesses: tuple[Triple, ...]
    allowed_values: tuple[Node, ...] | None = None


@dataclass(frozen=True)
class RootConflict:
    """A selected root inherits established incompatible local requirements."""

    root: Node
    local_shapes: tuple[Node, ...]
    dependencies: tuple[ShapeDependency, ...]


@dataclass(frozen=True)
class SignedDependency:
    """One original reference and its finite supported polarity qualification."""

    dependency: ShapeDependency
    polarity: Literal["positive", "negative", "nonmonotone"]
    active: bool = True


@dataclass(frozen=True)
class ShapeComponent:
    """A dependency SCC with no invented recursive validation interpretation."""

    shapes: tuple[Node, ...]
    dependencies: tuple[SignedDependency, ...]
    recursive: bool
    boundary: Literal["acyclic", "processor_defined", "negative_recursion_unsupported"]


@dataclass(frozen=True)
class ShapeAnalysis:
    """Completed finite checks do not certify arbitrary SHACL satisfiability.

    Impossible means an explicit root has a supported per-invocation conflict;
    it does not assert that the shapes graph has nonempty validation targets.
    Other cases retain not_determined, including compatible local constraints.
    """

    local_conflicts: tuple[LocalConflict, ...]
    root_conflicts: tuple[RootConflict, ...]
    dependencies: tuple[SignedDependency, ...]
    components: tuple[ShapeComponent, ...]
    report: Report
    satisfiability: Literal["impossible", "not_determined"] = "not_determined"
    deactivated_shapes: tuple[Node, ...] = ()
