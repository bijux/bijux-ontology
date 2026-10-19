"""Caller-authored record topology policies, separate from ontology entailment."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import cast

from rdflib import URIRef
from rdflib.paths import Path

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.limits import Limits
from bijux_ontology.rdf.paths import RdfPath, capture_path


@dataclass(frozen=True)
class TopologyRelation:
    """Select a path's participation in three independent application policies.

    Selecting a reference does not make it containment. Only ownership=True
    contributes incoming owners; only acyclic=True forbids its selected cycles.
    Acyclic relations form one joint constrained relation graph.
    """

    name: str
    path: URIRef | Path | RdfPath
    reachability: bool = field(default=True, kw_only=True)
    ownership: bool = field(default=False, kw_only=True)
    acyclic: bool = field(default=False, kw_only=True)

    def __post_init__(self) -> None:
        """Own maintained path syntax and explicit boolean policy roles."""
        if not isinstance(self.name, str) or not self.name.strip():
            raise InputError("Topology relations require nonempty names")
        if any(
            type(value) is not bool
            for value in (self.reachability, self.ownership, self.acyclic)
        ):
            raise InputError("Topology relation roles must be booleans")
        if not (self.reachability or self.ownership or self.acyclic):
            raise InputError("A topology relation must participate in a policy")
        object.__setattr__(self, "name", str(self.name))
        object.__setattr__(self, "path", capture_path(self.path))


@dataclass(frozen=True)
class TopologyContract:
    """Uniform obligations over explicit selected record bindings.

    Roots are exempt from minimum ownership. Explicit shared targets are exempt
    from maximum ownership; reference cycles remain legal unless selected as
    acyclic. Counts are distinct owner records, not predicate/path multiplicity.
    """

    relations: tuple[TopologyRelation, ...]
    require_reachable: bool = field(default=True, kw_only=True)
    min_owners: int | None = field(default=None, kw_only=True)
    max_owners: int | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        """Capture finite policies and reject contradictory or empty obligations."""
        values = cast(object, self.relations)
        if not isinstance(values, Sequence) or isinstance(values, str | bytes):
            raise InputError("Topology relations require a finite typed sequence")
        count = len(values)
        if not count or count > Limits().max_states:
            raise InputError("Topology relations require a bounded nonempty inventory")
        owned = tuple(values[index] for index in range(count))
        if any(not isinstance(value, TopologyRelation) for value in owned):
            raise InputError("Expected typed TopologyRelation declarations")
        if len({value.name for value in owned}) != count:
            raise InputError("Topology relation names must be unique")
        if type(self.require_reachable) is not bool:
            raise InputError("Topology reachability requirement must be boolean")
        for value in (self.min_owners, self.max_owners):
            if value is not None and (type(value) is not int or value < 0):
                raise InputError("Ownership bounds must be nonnegative integers")
        if (
            self.min_owners is not None
            and self.max_owners is not None
            and self.min_owners > self.max_owners
        ):
            raise InputError("Minimum ownership cannot exceed maximum ownership")
        if self.require_reachable and not any(value.reachability for value in owned):
            raise InputError("Reachability requires a selected reachability relation")
        counts = self.min_owners is not None or self.max_owners is not None
        if counts and not any(value.ownership for value in owned):
            raise InputError("Owner bounds require a selected ownership relation")
        if not (
            self.require_reachable or counts or any(value.acyclic for value in owned)
        ):
            raise InputError("A topology contract must require a selected obligation")
        object.__setattr__(self, "relations", owned)
