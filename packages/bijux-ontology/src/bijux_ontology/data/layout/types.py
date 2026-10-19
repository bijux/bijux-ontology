"""Finite application graph inventories and asserted content-placement intent."""

from dataclasses import dataclass
from typing import Literal, TypeVar

from rdflib import BNode, URIRef

from bijux_ontology._jsonpaths import bound_index
from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, bound_text
from bijux_ontology.rdf.terms import bound_node

_Item = TypeVar("_Item")


@dataclass(frozen=True)
class GraphSelector:
    """Select exact graph terms or a finite IRI prefix/suffix and term class.

    None denotes the default graph. Blank names must be exact owned snapshot
    terms. Patterns are literal string boundaries, never regex or IRI denotation.
    An empty names tuple uses kind/pattern selection; its default selects all.
    """

    names: tuple[URIRef | BNode | None, ...] = ()
    kind: Literal["any", "iri", "blank", "default"] = "any"
    iri_prefix: str | None = None
    iri_suffix: str | None = None

    def __post_init__(self) -> None:
        """Own only finite typed intent without interpreting graph contents."""
        qualify_selector(self, Budget())


@dataclass(frozen=True)
class GraphRequirement:
    """Count selected graphs, including empty graphs; maximum zero forbids them."""

    name: str
    graphs: GraphSelector
    minimum: int = 1
    maximum: int | None = None

    def __post_init__(self) -> None:
        """Validate finite bounds and the explicit graph selection."""
        qualify_requirement(self, Budget())


@dataclass(frozen=True)
class ContentPlacement:
    """Route selected asserted statements exclusively to selected partitions.

    Predicate, rdf:type-object and subject selections form a union. Empty
    selectors select every statement. Counts apply to matching statements inside
    all selected partitions, not RDF entities or inferred types. Subject blanks
    require exact source ownership. No class closure or graph entailment is used.
    """

    name: str
    graphs: GraphSelector
    predicates: tuple[URIRef, ...] = ()
    types: tuple[URIRef, ...] = ()
    subjects: tuple[URIRef | BNode, ...] = ()
    minimum: int = 0
    maximum: int | None = None

    def __post_init__(self) -> None:
        """Retain immutable exact resource inventories and application counts."""
        qualify_placement(self, Budget())


@dataclass(frozen=True)
class DatasetLayout:
    """An explicit allowed inventory beside independent presence and routing rules.

    Closed inventories include the always-present default graph. The allowed
    selectors do not imply required presence. Requirements and placements remain
    applicable in an open inventory; unrelated legal graph names are accepted.
    """

    allowed: tuple[GraphSelector, ...] = ()
    requirements: tuple[GraphRequirement, ...] = ()
    placements: tuple[ContentPlacement, ...] = ()
    closed: bool = False

    def __post_init__(self) -> None:
        """Validate immutable inventories without reading any RDF source."""
        qualify_layout(self, Budget())


def _tuple(value: tuple[_Item, ...], budget: Budget) -> tuple[_Item, ...]:
    """Require owned finite tuples before scanning or retaining metadata."""
    if type(value) is not tuple:
        raise InputError("Layout inventories require immutable tuples")
    budget.consume("states", len(value))
    budget.consume("bytes", 64 + 16 * len(value))
    return value


def _counts(minimum: int, maximum: int | None, budget: Budget) -> None:
    """Bound integer metadata before comparing or rendering counts."""
    for value in (minimum, maximum):
        if value is None:
            continue
        if type(value) is not int or value < 0:
            raise InputError("Layout bounds require nonnegative integers")
        bound_index(value, budget)
    if maximum is not None and maximum < minimum:
        raise InputError("Layout maximum must be at least minimum")


def qualify_selector(value: GraphSelector, budget: Budget) -> None:
    """Bound public selector metadata before hashing or using term equality."""
    if not isinstance(value, GraphSelector):
        raise InputError("Expected a typed GraphSelector")
    bound_text(value.kind, budget)
    if value.kind not in ("any", "iri", "blank", "default"):
        raise InputError("Unknown graph term class")
    names = _tuple(value.names, budget)
    for name in names:
        budget.consume("work")
        if name is not None:
            if not isinstance(name, URIRef | BNode):
                raise InputError("Exact graph names require RDF resource terms or None")
            bound_node(name, budget)
    for text in (value.iri_prefix, value.iri_suffix):
        if text is not None:
            if type(text) is not str:
                raise InputError("Graph patterns require ordinary text")
            bound_text(text, budget)
    patterned = value.iri_prefix is not None or value.iri_suffix is not None
    if names and (value.kind != "any" or patterned):
        raise InputError("Choose exact names or graph class/pattern selection")
    if patterned and value.kind != "iri":
        raise InputError("IRI boundary patterns require kind='iri'")


def qualify_requirement(value: GraphRequirement, budget: Budget) -> None:
    """Requalify caller-owned graph obligations under the actual operation."""
    if not isinstance(value, GraphRequirement):
        raise InputError("Expected a typed GraphRequirement")
    bound_text(value.name, budget)
    if not value.name:
        raise InputError("Graph requirements need a nonempty name")
    qualify_selector(value.graphs, budget)
    _counts(value.minimum, value.maximum, budget)


def qualify_placement(value: ContentPlacement, budget: Budget) -> None:
    """Bound exact statement selection before RDF matching or report formatting."""
    if not isinstance(value, ContentPlacement):
        raise InputError("Expected a typed ContentPlacement")
    bound_text(value.name, budget)
    if not value.name:
        raise InputError("Content placements need a nonempty name")
    qualify_selector(value.graphs, budget)
    for items, kind in (
        (value.predicates, URIRef),
        (value.types, URIRef),
        (value.subjects, (URIRef, BNode)),
    ):
        for item in _tuple(items, budget):
            budget.consume("work")
            if not isinstance(item, kind):
                raise InputError("Content selection requires exact RDF resource terms")
            bound_node(item, budget)
    _counts(value.minimum, value.maximum, budget)


def qualify_layout(value: DatasetLayout, budget: Budget) -> None:
    """Requalify finite public contracts before any source reads or comparisons."""
    if not isinstance(value, DatasetLayout) or type(value.closed) is not bool:
        raise InputError("Expected a typed DatasetLayout and boolean closure")
    for selector in _tuple(value.allowed, budget):
        budget.consume("work")
        qualify_selector(selector, budget)
    for requirement in _tuple(value.requirements, budget):
        budget.consume("work")
        qualify_requirement(requirement, budget)
    for placement in _tuple(value.placements, budget):
        budget.consume("work")
        qualify_placement(placement, budget)
