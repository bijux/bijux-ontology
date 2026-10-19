"""Finite lazy candidate construction over existing typed SHACL records."""

from collections.abc import Iterator
from itertools import combinations

from rdflib import BNode, Literal, URIRef
from rdflib.term import Node

from bijux_ontology.absence import ABSENT
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.rdf.paths import RdfPath
from bijux_ontology.rdf.terms import bound_node, term_key
from bijux_ontology.shacl.authoring.types import (
    ShaclCount,
    ShaclLogical,
    ShaclNodeShape,
    ShaclPropertyShape,
    ShaclShapeRef,
    ShaclValue,
)
from bijux_ontology.shacl.refinement.normalization import Branches

from .profile import PreparedExamples, Triple, UnsupportedExample, Value, reserve

State = tuple[Triple, ...]


def _key(value: Node, budget: Budget) -> tuple[str | None, ...]:
    """Reserve lexical key copies before invoking exact RDF term identity."""
    bound_node(value, budget)
    reserve(budget)
    return term_key(value)


class Candidates:
    """Choose caller values and nested branches without assigning SHACL truth."""

    def __init__(self, prepared: PreparedExamples, budget: Budget) -> None:
        """Reuse the existing finite obligation merger for flat property guidance."""
        self.prepared, self.budget = prepared, budget
        reserve(budget, len(prepared.definitions))
        self.branches = Branches(tuple(prepared.definitions.values()), (), budget)

    def _sets(
        self, shape: ShaclPropertyShape, focus: Node, state: State
    ) -> Iterator[tuple[Node, ...]]:
        """Choose exact distinct pool values while retaining existing facts."""
        assert isinstance(shape.path, RdfPath) and isinstance(shape.path.node, URIRef)
        predicate = shape.path.node
        reserve(self.budget, len(shape.constraints) + 1)
        flat = tuple(
            item
            for item in shape.constraints
            if isinstance(item, ShaclCount)
            or isinstance(item, ShaclValue)
            and item.class_iri is None
        )
        # The reused merger revisits earlier terms when conjoining rules. Pay
        # for that bounded worst-case key workspace before asking it to merge.
        for _ in flat:
            for item in flat:
                self.budget.consume("work")
                if isinstance(item, ShaclValue):
                    if item.has_value is not ABSENT:
                        bound_node(item.has_value, self.budget)
                        reserve(self.budget)
                    for admitted in item.in_values or ():
                        bound_node(admitted, self.budget)
                        reserve(self.budget)
                    if item.datatype is not None:
                        bound_text(item.datatype, self.budget)
        obligation = self.branches.property(
            ShaclPropertyShape(shape.identifier, shape.path, flat)
        )
        existing: dict[tuple[str | None, ...], Node] = {}
        self.budget.consume("work", len(self.prepared.seed) + len(state))
        reserve(self.budget, len(self.prepared.seed) + len(state))
        for subject, edge, value in (*self.prepared.seed, *state):
            if subject == focus and edge == predicate:
                existing[_key(value, self.budget)] = value
        reserve(
            self.budget,
            len(self.prepared.pool)
            + len(obligation.fixed)
            + len(obligation.allowed or ())
            + len(existing),
        )
        pool = {_key(value, self.budget): value for value in self.prepared.pool}
        allowed = (
            None
            if obligation.allowed is None
            else {_key(value, self.budget) for value in obligation.allowed}
        )
        if allowed is not None and not set(existing) <= allowed:
            return
        selected = dict(existing)
        for value in obligation.fixed:
            identity = _key(value, self.budget)
            if (
                identity not in existing
                and identity not in pool
                or allowed is not None
                and identity not in allowed
            ):
                return
            selected[identity] = value
        choices: list[Value] = []
        for key, value in pool.items():
            self.budget.consume("work")
            kind = "literal" if isinstance(value, Literal) else "iri"
            if (
                key not in selected
                and (allowed is None or key in allowed)
                and kind in obligation.kinds
            ):
                choices.append(value)
        lower = max(0, obligation.minimum - len(selected))
        upper = (
            len(choices)
            if obligation.maximum is None
            else min(len(choices), obligation.maximum - len(selected))
        )
        if lower > upper:
            return
        for size in range(lower, upper + 1):
            reserve(self.budget, len(choices) + size + len(selected) + 1)
            iterator = combinations(choices, size)
            while True:
                # combinations allocates its next immutable tuple in __next__.
                # Reserve it before advancing, including the terminal attempt.
                self.budget.consume("work")
                reserve(self.budget, size + len(selected) + 1)
                try:
                    additional = next(iterator)
                except StopIteration:
                    break
                yield (*selected.values(), *additional)

    def _sequence(
        self,
        references: tuple[ShaclShapeRef, ...],
        focus: Node,
        state: State,
        position: int,
        depth: int,
    ) -> Iterator[State]:
        """Conjoin typed references lazily without allocating a Cartesian product."""
        self.budget.consume("work")
        if position == len(references):
            yield state
            return
        for candidate in self.expand(
            references[position].identifier, focus, state, depth + 1
        ):
            yield from self._sequence(
                references, focus, candidate, position + 1, depth + 1
            )

    def _constraints(
        self,
        constraints: tuple[ShaclCount | ShaclValue | ShaclLogical | ShaclShapeRef, ...],
        focus: Node,
        state: State,
        position: int,
        depth: int,
    ) -> Iterator[State]:
        """Use original branches; native validation checks every value rule."""
        self.budget.consume("work")
        if depth > min(128, self.budget.limits.max_depth):
            raise UnsupportedExample(
                "Example construction exceeds the supported finite depth"
            )
        if position == len(constraints):
            yield state
            return
        constraint = constraints[position]
        if isinstance(constraint, ShaclShapeRef):
            candidates = self.expand(constraint.identifier, focus, state, depth + 1)
        elif isinstance(constraint, ShaclLogical):
            if constraint.operator == "and":
                candidates = self._sequence(
                    constraint.shapes, focus, state, 0, depth + 1
                )
            else:
                candidates = self._alternatives(
                    constraint.shapes, focus, state, depth + 1
                )
        else:
            candidates = iter((state,))
        for candidate in candidates:
            yield from self._constraints(
                constraints, focus, candidate, position + 1, depth + 1
            )

    def _alternatives(
        self,
        references: tuple[ShaclShapeRef, ...],
        focus: Node,
        state: State,
        depth: int,
    ) -> Iterator[State]:
        """Try another OR branch after a contradictory local branch yields no values."""
        for reference in references:
            self.budget.consume("work")
            yield from self.expand(reference.identifier, focus, state, depth + 1)

    def _values(
        self,
        constraints: tuple[ShaclCount | ShaclValue | ShaclLogical | ShaclShapeRef, ...],
        values: tuple[Node, ...],
        state: State,
        position: int,
        depth: int,
    ) -> Iterator[State]:
        """Apply selected nested value-shape construction to each actual chosen term."""
        self.budget.consume("work")
        if position == len(values):
            yield state
            return
        for candidate in self._constraints(
            constraints, values[position], state, 0, depth + 1
        ):
            yield from self._values(
                constraints, values, candidate, position + 1, depth + 1
            )

    def expand(
        self, shape: URIRef | BNode, focus: Node, state: State = (), depth: int = 0
    ) -> Iterator[State]:
        """Build direct-path edges using only explicit caller pool terms."""
        self.budget.consume("work")
        if depth > min(128, self.budget.limits.max_depth):
            raise UnsupportedExample(
                "Example construction exceeds the supported finite depth"
            )
        definition = self.prepared.definitions.get(shape)
        if definition is None:
            raise UnsupportedExample(
                "A required original shape definition is unavailable"
            )
        reserve(self.budget, len(definition.constraints) + 1)
        constraints = tuple(
            item
            for item in definition.constraints
            if isinstance(item, ShaclCount | ShaclValue | ShaclLogical | ShaclShapeRef)
        )
        if isinstance(definition, ShaclNodeShape):
            for candidate in self._sequence(
                definition.properties, focus, state, 0, depth + 1
            ):
                yield from self._constraints(
                    constraints, focus, candidate, 0, depth + 1
                )
            return
        assert isinstance(definition.path, RdfPath) and isinstance(
            definition.path.node, URIRef
        )
        for selected in self._sets(definition, focus, state):
            if not isinstance(focus, URIRef | BNode) and selected:
                # An original seed blank resource is a valid RDF subject; a
                # literal cannot become a subject for a nested property.
                continue
            reserve(self.budget, len(state) + len(selected))
            self.budget.consume(
                "work", len(selected) * (len(self.prepared.seed) + len(state) + 1)
            )
            triples: State = tuple(
                (focus, definition.path.node, value)
                for value in selected
                if (focus, definition.path.node, value) not in self.prepared.seed
                and (focus, definition.path.node, value) not in state
            )
            current = (*state, *triples)
            reserve(self.budget, len(constraints) + 1)
            nested = tuple(
                item
                for item in constraints
                if isinstance(item, ShaclLogical | ShaclShapeRef)
            )
            for candidate in self._values(nested, selected, current, 0, depth + 1):
                yield candidate
