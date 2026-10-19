"""Explicit primary-record obligations and inspectable physical graph scope."""

from dataclasses import dataclass
from typing import Literal

from rdflib import BNode, URIRef
from rdflib import Literal as RDFLiteral

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.shacl.evidence.results import ShaclResult


def capture(
    values: object, budget: Budget, *, count: int | None = None
) -> tuple[object, ...]:
    """Own one bounded indexed sequence without accepting arbitrary iterators."""
    from collections.abc import Sequence

    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        raise InputError("Partition inventories require finite sequences")
    count = len(values) if count is None else count
    budget.consume("states", count)
    budget.consume("bytes", 64 + 16 * count)
    result = []
    for index in range(count):
        budget.consume("work")
        try:
            result.append(values[index])
        except IndexError as error:
            raise InputError("Partition inventory changed during capture") from error
    return tuple(result)


def graph_name(value: object, budget: Budget) -> GraphSelection | None:
    """Retain owned blank objects and normalize only unambiguous named selectors."""
    if value is None:
        return None
    if isinstance(value, BNode):
        bound_node(value, budget)
        return value
    if not isinstance(value, str) or isinstance(value, RDFLiteral):
        raise InputError("Graph selections require named or owned blank graphs")
    bound_text(value, budget)
    return "@default" if value == "@default" else str(absolute_iri(value))


@dataclass(frozen=True)
class PartitionObligation:
    """Select an actual graph contract with qualified cross-record completeness."""

    name: str
    shapes: PreparedSource
    use_shapes: tuple[str | URIRef, ...]
    graph: Literal["primary", "merged"] = "primary"
    requires_complete_primary: bool = False

    def __post_init__(self) -> None:
        """Own finite declarations; execution establishes target inventory."""
        budget = Budget()
        bound_text(self.name, budget)
        if not self.name or not isinstance(self.shapes, PreparedSource):
            raise InputError("Obligations require a name and prepared shapes")
        if (
            self.graph not in ("primary", "merged")
            or type(self.requires_complete_primary) is not bool
        ):
            raise InputError("Unknown partition obligation scope")
        selected = []
        for node in capture(self.use_shapes, budget):
            if not isinstance(node, str) or isinstance(node, BNode | RDFLiteral):
                raise InputError("Partition obligations require named authored roots")
            bound_text(node, budget)
            selected.append(absolute_iri(str(node)))
        object.__setattr__(self, "name", str(self.name))
        object.__setattr__(self, "use_shapes", tuple(selected))


@dataclass(frozen=True)
class PartitionPlan:
    """Bind exact primary records and supporting partitions of one owned source.

    primary_complete declares the intended supplied corpus boundary. Complete
    graph capture and actual target inventory must independently qualify it.
    """

    primary: GraphSelection | None
    support: tuple[GraphSelection, ...]
    focus_nodes: tuple[URIRef | BNode, ...]
    obligations: tuple[PartitionObligation, ...]
    primary_complete: bool = False

    def __post_init__(self) -> None:
        """Capture only typed owned declarations, never infer record or graph roles."""
        budget = Budget()
        if type(self.primary_complete) is not bool:
            raise InputError("primary_complete requires an explicit boolean")
        object.__setattr__(self, "primary", graph_name(self.primary, budget))
        names = []
        for value in capture(self.support, budget):
            name = graph_name(value, budget)
            if name is None:
                raise InputError("Use @default for supporting default graph scope")
            names.append(name)
        focus = []
        for node in capture(self.focus_nodes, budget):
            if not isinstance(node, URIRef | BNode):
                raise InputError("Primary records require named or owned subjects")
            bound_node(node, budget)
            focus.append(node)
        obligations = capture(self.obligations, budget)
        if any(not isinstance(item, PartitionObligation) for item in obligations):
            raise InputError("Expected typed partition obligations")
        object.__setattr__(self, "support", tuple(names))
        object.__setattr__(self, "focus_nodes", tuple(focus))
        object.__setattr__(self, "obligations", obligations)


@dataclass(frozen=True)
class PartitionScope:
    """Physical participation and effective requirements, including inferred scope.

    primary_complete records caller corpus intent; the report independently
    qualifies actual full-graph target inventory and input fidelity.
    """

    primary: GraphSelection | None
    support: tuple[GraphSelection, ...]
    graph: Literal["primary", "merged"]
    focus_nodes: tuple[URIRef | BNode, ...]
    requires_complete_primary: bool
    primary_complete: bool
    document_iri: str | None
    effective_url: str | None


@dataclass(frozen=True)
class PartitionResult:
    """Raw executions and primary target discovery beside required scope truth."""

    name: str
    scope: PartitionScope
    primary: PreparedSource
    effective: PreparedSource
    results: tuple[ShaclResult, ...]
    discovery: ShaclResult | None
    report: Report


@dataclass(frozen=True)
class PartitionValidation:
    """Required obligation reports without silently validating support submissions."""

    results: tuple[PartitionResult, ...]
    report: Report
