"""Explicit root-edge selection and immutable positional RDF list edits."""

from dataclasses import dataclass

from rdflib import BNode, Literal, URIRef

from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.migrations.candidates import MigrationCandidate
from bijux_ontology.rdf.terms import bound_node

ListValue = URIRef | BNode | Literal


def _terms(values: object, budget: Budget) -> tuple[ListValue, ...]:
    """Require immutable finite term inventories before retaining references."""
    if type(values) is not tuple:
        raise InputError("List migration values require immutable tuples")
    budget.consume("states", len(values))
    budget.consume("bytes", 64 + 16 * len(values))
    result: list[ListValue] = []
    for value in values:
        budget.consume("work")
        if not isinstance(value, URIRef | BNode | Literal):
            raise InputError("List values require exact RDF terms")
        bound_node(value, budget)
        if isinstance(value, URIRef):
            absolute_iri(value)
        result.append(value)
    return tuple(result)


def _index(value: int, budget: Budget) -> None:
    """Bound integer work before any positional arithmetic or formatting."""
    if type(value) is not int or value < 0:
        raise InputError("List indices require nonnegative integers")
    budget.consume("work", value.bit_length() + 1)


@dataclass(frozen=True)
class ListTarget:
    """Name the exact incoming root edge and physical graph to edit.

    Dataset inputs require an explicit graph: None means only the default graph.
    Blank terms must be actual immutable objects from the supplied snapshot.
    Internal first/rest edges and list-cell owners are deliberately unsupported.
    """

    subject: URIRef | BNode
    predicate: URIRef
    head: URIRef | BNode
    graph: URIRef | BNode | None | Absent = ABSENT

    def __post_init__(self) -> None:
        """Check resource positions without guessing blank-label ownership."""
        budget = Budget()
        if not isinstance(self.subject, URIRef | BNode) or not isinstance(
            self.head, URIRef | BNode
        ):
            raise InputError("List subjects and heads require RDF resources")
        for value in (self.subject, self.predicate, self.head, self.graph):
            if value is None or value is ABSENT:
                continue
            if not isinstance(value, URIRef | BNode):
                raise InputError("List target positions require RDF resources")
            bound_node(value, budget)
            if isinstance(value, URIRef):
                absolute_iri(value)
        if not isinstance(self.predicate, URIRef):
            raise InputError("List target predicates require named IRIs")


@dataclass(frozen=True)
class ListInsert:
    """Insert exact members immediately before one original position or the end."""

    index: int
    values: tuple[ListValue, ...]

    def __post_init__(self) -> None:
        """Own a bounded tuple while retaining exact RDF term objects."""
        budget = Budget()
        _index(self.index, budget)
        object.__setattr__(self, "values", _terms(self.values, budget))


@dataclass(frozen=True)
class ListRemove:
    """Remove distinct positions counted in the original selected sequence."""

    indices: tuple[int, ...]

    def __post_init__(self) -> None:
        """Require finite distinct original indices, excluding boolean values."""
        _indices(self.indices, Budget())


@dataclass(frozen=True)
class ListReorder:
    """Supply a permutation of all original positions, including duplicates' slots."""

    indices: tuple[int, ...]

    def __post_init__(self) -> None:
        """Retain finite distinct positional intent; length is checked on the list."""
        _indices(self.indices, Budget())


def _indices(values: object, budget: Budget) -> tuple[int, ...]:
    """Qualify immutable index inventories before hashing or building permutations."""
    if type(values) is not tuple:
        raise InputError("List index inventories require immutable tuples")
    budget.consume("states", len(values))
    budget.consume("bytes", 64 + 16 * len(values))
    seen: set[int] = set()
    for value in values:
        budget.consume("work")
        _index(value, budget)
        if value in seen:
            raise InputError("List index inventories cannot repeat positions")
        seen.add(value)
    return values


@dataclass(frozen=True)
class ListMigrationCandidate(MigrationCandidate):
    """An ordinary checked migration with completed positional edit counts."""

    original_size: int
    candidate_size: int
    copied_cells: int
