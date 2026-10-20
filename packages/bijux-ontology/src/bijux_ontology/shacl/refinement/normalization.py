"""Bounded conjunction branches over the existing typed shape representation."""

from dataclasses import dataclass

from rdflib import BNode, URIRef
from rdflib.term import Node

from bijux_ontology.absence import ABSENT
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget
from bijux_ontology.rdf.paths.types import RdfPath
from bijux_ontology.rdf.terms import term_key
from bijux_ontology.shacl.authoring.types import (
    ShaclCount,
    ShaclLogical,
    ShaclNodeShape,
    ShaclPropertyShape,
    ShaclShapeRef,
    ShaclValue,
)


class Unsupported(InputError):
    """The finite implication fragment cannot account for selected semantics."""


@dataclass(frozen=True)
class Obligation:
    """Conjoined necessary path bounds and exact universal/value requirements."""

    path: RdfPath
    minimum: int = 0
    maximum: int | None = None
    fixed: tuple[Node, ...] = ()
    allowed: tuple[Node, ...] | None = None
    datatypes: tuple[str, ...] = ()
    kinds: frozenset[str] = frozenset(("iri", "blank", "literal"))


_KINDS = {
    "iri": frozenset(("iri",)),
    "blank": frozenset(("blank",)),
    "literal": frozenset(("literal",)),
    "blank_or_iri": frozenset(("blank", "iri")),
    "blank_or_literal": frozenset(("blank", "literal")),
    "iri_or_literal": frozenset(("iri", "literal")),
}
Branch = tuple[Obligation, ...]
Definition = ShaclNodeShape | ShaclPropertyShape


def path_key(path: RdfPath, budget: Budget) -> tuple[object, ...]:
    """Compare captured syntax without comparing snapshot-local blank labels."""
    budget.consume("work")
    budget.consume("states")
    return (
        path.kind,
        str(path.node) if path.kind == "predicate" else None,
        tuple(path_key(child, budget) for child in path.children),
    )


def _terms(values: tuple[Node, ...], budget: Budget) -> dict[tuple[object, ...], Node]:
    """Retain exact RDF identities rather than datatype value equality."""
    result: dict[tuple[object, ...], Node] = {}
    for value in values:
        budget.consume("work")
        budget.consume("states")
        result[term_key(value)] = value
    return result


def merge(left: Branch, right: Branch, budget: Budget) -> Branch:
    """Conjoin repeated path obligations, without deciding satisfiability."""
    budget.consume("states", len(left) + len(right))
    result: dict[tuple[object, ...], Obligation] = {}
    for item in (*left, *right):
        key = path_key(item.path, budget)
        if key not in result:
            result[key] = item
            continue
        old = result[key]
        fixed = _terms(old.fixed + item.fixed, budget)
        allowed = None
        if old.allowed is None:
            allowed = item.allowed
        elif item.allowed is None:
            allowed = old.allowed
        else:
            other = _terms(item.allowed, budget)
            allowed = tuple(
                term
                for identity, term in _terms(old.allowed, budget).items()
                if identity in other
            )
        maximum = old.maximum
        if item.maximum is not None:
            maximum = item.maximum if maximum is None else min(maximum, item.maximum)
        budget.consume("states", len(old.datatypes) + len(item.datatypes))
        result[key] = Obligation(
            old.path,
            max(old.minimum, item.minimum, len(fixed)),
            maximum,
            tuple(fixed.values()),
            allowed,
            tuple(dict.fromkeys((*old.datatypes, *item.datatypes))),
            old.kinds & item.kinds,
        )
    return tuple(result.values())


class Branches:
    """Expand only supported acyclic node AND/OR; references stay original terms."""

    def __init__(
        self,
        definitions: tuple[Definition, ...],
        inactive: tuple[Node, ...],
        budget: Budget,
    ) -> None:
        self.budget = budget
        budget.consume("states", len(definitions) + len(inactive))
        self.definitions = {item.identifier: item for item in definitions}
        self.inactive = frozenset(inactive)

    def product(
        self, left: tuple[Branch, ...], right: tuple[Branch, ...]
    ) -> tuple[Branch, ...]:
        """Charge a finite logical product before allocating any branch slots."""
        self.budget.consume("states", len(left) * len(right))
        result = []
        for one in left:
            for two in right:
                self.budget.consume("work")
                result.append(merge(one, two, self.budget))
        return tuple(result)

    def property(self, shape: ShaclPropertyShape) -> Obligation:
        """Read ordinary counts and exact value obligations already parsed by T160."""
        assert isinstance(shape.path, RdfPath)
        item = Obligation(shape.path)
        for constraint in shape.constraints:
            self.budget.consume("work")
            if isinstance(constraint, ShaclCount):
                item = merge(
                    (item,),
                    (
                        Obligation(
                            shape.path,
                            constraint.minimum or 0,
                            constraint.maximum,
                        ),
                    ),
                    self.budget,
                )[0]
            elif isinstance(constraint, ShaclValue) and constraint.class_iri is None:
                item = merge(
                    (item,),
                    (
                        Obligation(
                            shape.path,
                            fixed=()
                            if constraint.has_value is ABSENT
                            else (constraint.has_value,),
                            allowed=constraint.in_values,
                            datatypes=()
                            if constraint.datatype is None
                            else (constraint.datatype,),
                            kinds=_KINDS[constraint.node_kind]
                            if constraint.node_kind
                            else _KINDS["iri"] | _KINDS["blank_or_literal"],
                        ),
                    ),
                    self.budget,
                )[0]
            else:
                raise Unsupported(
                    "Value-level logic or other property constraints are unsupported"
                )
        return item

    def expand(
        self, node: URIRef | BNode, active: frozenset[Node] = frozenset()
    ) -> tuple[Branch, ...]:
        """Keep recursion outside finite typed implication."""
        self.budget.consume("work")
        self.budget.consume("states")
        if len(active) > min(128, self.budget.limits.max_depth):
            raise Unsupported("Refinement logical depth is unsupported")
        if node in active:
            raise Unsupported("Recursive refinement is unsupported")
        if node in self.inactive:
            return ((),)
        definition = self.definitions.get(node)
        if definition is None:
            raise Unsupported("A required typed shape definition is unavailable")
        if isinstance(definition, ShaclPropertyShape):
            return ((self.property(definition),),)
        result: tuple[Branch, ...] = ((),)
        nested = active | {node}
        for reference in definition.properties:
            result = self.product(result, self.expand(reference.identifier, nested))
        for constraint in definition.constraints:
            self.budget.consume("work")
            if isinstance(constraint, ShaclShapeRef):
                result = self.product(
                    result, self.expand(constraint.identifier, nested)
                )
            elif isinstance(constraint, ShaclLogical) and constraint.operator in (
                "and",
                "or",
            ):
                branches: tuple[Branch, ...] = (
                    ((),) if constraint.operator == "and" else ()
                )
                for reference in constraint.shapes:
                    child = self.expand(reference.identifier, nested)
                    if constraint.operator == "and":
                        branches = self.product(branches, child)
                    else:
                        self.budget.consume("states", len(branches) + len(child))
                        branches = (*branches, *child)
                result = self.product(result, branches)
            else:
                raise Unsupported(
                    "Node constraints outside finite AND/OR are unsupported"
                )
        return result
