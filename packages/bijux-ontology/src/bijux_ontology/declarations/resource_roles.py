"""Explicit resource interpretation contracts without merging logical facets."""

from dataclasses import KW_ONLY, dataclass
from typing import Literal as Choice

from rdflib import BNode, Literal

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri

ResourceKind = Choice["class", "concept", "instance"]


def _kind(kind: ResourceKind) -> None:
    """Keep interpretation choices finite instead of accepting arbitrary markers."""
    if not isinstance(kind, str) or kind not in ("class", "concept", "instance"):
        raise InputError("Select class, concept or instance resource interpretation")


def _iri(iri: str, budget: Budget) -> None:
    """Require exact named identifiers before retaining declaration metadata."""
    if not isinstance(iri, str) or isinstance(iri, BNode | Literal):
        raise InputError("Role contracts require named IRI strings")
    bound_text(iri, budget)
    absolute_iri(iri)


@dataclass(frozen=True)
class RoleBridge:
    """One explicitly chosen link from a source facet to a separately typed target.

    Following this link never copies instance facts or changes the emitted IRI.
    Its meaning comes from the caller contract, not the predicate's spelling.
    """

    predicate: str
    _: KW_ONLY
    source_role: ResourceKind = "concept"
    reverse: bool = False

    def __post_init__(self) -> None:
        """Bound the exact predicate and finite source-role/direction choices."""
        _iri(self.predicate, Budget())
        _kind(self.source_role)
        if type(self.reverse) is not bool:
            raise InputError("Role bridge direction must be a boolean")


@dataclass(frozen=True)
class ResourceRole:
    """Choose a facet; existing resource types remain independent obligations.

    Instance types require actual asserted or supported named-type witnesses.
    Empty types permits any established individual witness. Punning prohibition
    is an application policy, never a claim that OWL class/individual reuse is
    invalid. Full profile validation is a separate requested capability.
    """

    kind: ResourceKind
    _: KW_ONLY
    types: tuple[str, ...] = ()
    type_mode: Choice["asserted", "named"] = "asserted"
    punning: Choice["allow", "forbid"] = "forbid"
    bridge: RoleBridge | None = None

    def __post_init__(self) -> None:
        """Require finite explicit instance classes and structurally valid policy."""
        _kind(self.kind)
        if type(self.types) is not tuple:
            raise InputError("Role instance classes require an immutable tuple")
        budget = Budget()
        if len(self.types) > budget.limits.max_states:
            raise InputError("Role class inventory exceeds max_states")
        for iri in self.types:
            budget.consume("states")
            _iri(iri, budget)
        if self.kind != "instance" and self.types:
            raise InputError("Only instance interpretation accepts required classes")
        if not isinstance(self.type_mode, str) or self.type_mode not in (
            "asserted",
            "named",
        ):
            raise InputError("Select asserted or named instance-type witnesses")
        if not isinstance(self.punning, str) or self.punning not in ("allow", "forbid"):
            raise InputError("Select an explicit allow/forbid punning policy")
        if self.bridge is not None and not isinstance(self.bridge, RoleBridge):
            raise InputError("Expected an explicit RoleBridge")
