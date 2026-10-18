"""Closed local profile plans and explicit per-record applicability evidence."""

from dataclasses import dataclass

from rdflib import BNode, URIRef
from rdflib.term import Node

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.reports import Report
from bijux_ontology.workflows.projects.contracts import ContractResult
from bijux_ontology.workflows.projects.profiles.execution import EffectiveProfile
from bijux_ontology.workflows.projects.profiles.loading import _items, _text


@dataclass(frozen=True)
class ProfileJobBinding:
    """Bind one actual selected layer job's data alias and entire named root set."""

    layer: str
    job: str
    data_resource: str
    shapes: tuple[URIRef, ...]

    def __post_init__(self) -> None:
        """Own finite metadata without accepting blank labels as named roots."""
        budget = Budget()
        for name in ("layer", "job", "data_resource"):
            object.__setattr__(
                self, name, _text(getattr(self, name), budget, iri=name == "layer")
            )
        shapes = []
        for shape in _items(self.shapes, budget):
            if not isinstance(shape, URIRef):
                raise InputError("Routed shape roots require named RDF IRIs")
            bound_node(shape, budget)
            shapes.append(shape)
        if not shapes or len(set(shapes)) != len(shapes):
            raise InputError("Routed roots require a nonempty distinct inventory")
        object.__setattr__(self, "shapes", tuple(shapes))


@dataclass(frozen=True)
class ProfilePlan:
    """An already composed local profile with finite explicit runtime bindings."""

    profile: EffectiveProfile
    bindings: tuple[ProfileJobBinding, ...]

    def __post_init__(self) -> None:
        """Capture typed bindings; execution still qualifies the original jobs."""
        if not isinstance(self.profile, EffectiveProfile) or not isinstance(
            self.profile.report, Report
        ):
            raise InputError("Routing plans require an effective local profile")
        bindings = _items(self.bindings, Budget())
        if any(not isinstance(value, ProfileJobBinding) for value in bindings):
            raise InputError("Routing plans require typed job bindings")
        object.__setattr__(self, "bindings", bindings)


@dataclass(frozen=True)
class RoutedPlan:
    """One mandatory baseline or selected domain execution and its raw result."""

    profile: str
    baseline: bool
    result: ContractResult | None
    report: Report


@dataclass(frozen=True)
class RecordRoute:
    """The original selected record, actual declarations and all required plans."""

    record: URIRef | BNode
    declarations: tuple[Node, ...]
    applicability: Report
    plans: tuple[RoutedPlan, ...]
    report: Report


@dataclass(frozen=True)
class ProfileRouting:
    """Completed record evidence beside an explicit required unstarted tail."""

    records: tuple[RecordRoute, ...]
    pending: int
    report: Report
