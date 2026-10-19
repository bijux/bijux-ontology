"""Bounded regular-language compilation for typed vocabulary relation paths."""

from dataclasses import dataclass

from rdflib import BNode, Literal, URIRef, Variable
from rdflib.namespace import RDFS, SKOS

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri

from .types import (
    RelationChoice,
    RelationInverse,
    RelationRepeat,
    RelationSequence,
    RelationSpec,
    RelationStep,
)


def named(value: str, budget: Budget) -> URIRef:
    """Bound named metadata before validation, normalization or hashing."""
    if not isinstance(value, str) or isinstance(value, (BNode, Literal, Variable)):
        raise InputError("Relation paths require named IRI text")
    bound_text(value, budget)
    return URIRef(absolute_iri(str.__str__(value)))


@dataclass(frozen=True)
class Arc:
    """One charged local automaton transition, with None denoting epsilon."""

    step: RelationStep | None
    inverse: bool
    target: int


class Automaton:
    """Compile only the concrete finite relation-path specification."""

    def __init__(self, spec: RelationSpec, budget: Budget) -> None:
        self.budget = budget
        self.arcs: dict[int, list[Arc]] = {}
        self.start, self.end = self.compile(spec, False, 0)

    def state(self) -> int:
        """Pay each compiler state before allocating its transition container."""
        self.budget.consume("work")
        self.budget.consume("states")
        self.budget.consume("bytes", 128)
        node = len(self.arcs)
        self.arcs[node] = []
        return node

    def arc(
        self,
        source: int,
        target: int,
        step: RelationStep | None = None,
        inverse: bool = False,
    ) -> None:
        """Pay each epsilon or typed transition before retention."""
        self.budget.consume("work")
        self.budget.consume("states")
        self.budget.consume("bytes", 64)
        self.arcs[source].append(Arc(step, inverse, target))

    def compile(self, spec: RelationSpec, inverse: bool, depth: int) -> tuple[int, int]:
        """Expand repetition only after validating and bounding its counters."""
        self.budget.consume("work")
        if depth > self.budget.limits.max_depth:
            raise LimitExceeded("Relation language exceeds max_depth")
        start, end = self.state(), self.state()
        if isinstance(spec, RelationStep):
            predicate = named(spec.predicate, self.budget)
            bound_text(spec.role, self.budget)
            if spec.role not in ("class", "skos", "existential", "asserted"):
                raise InputError("Unknown relation edge role")
            if spec.role == "class" and predicate != RDFS.subClassOf:
                raise InputError("Class steps select asserted rdfs:subClassOf only")
            if spec.role == "skos" and predicate not in (
                SKOS.broader,
                SKOS.narrower,
                SKOS.broaderTransitive,
                SKOS.narrowerTransitive,
            ):
                raise InputError("SKOS steps select explicit hierarchy predicates")
            self.arc(start, end, RelationStep(str(predicate), spec.role), inverse)
        elif isinstance(spec, RelationInverse):
            first, last = self.compile(spec.path, not inverse, depth + 1)
            self.arc(start, first)
            self.arc(last, end)
        elif isinstance(spec, (RelationSequence, RelationChoice)):
            if type(spec.paths) is not tuple or not spec.paths:
                raise InputError("Sequence/choice need a nonempty tuple of paths")
            self.budget.consume("states", len(spec.paths))
            self.budget.consume("bytes", 16 * len(spec.paths))
            paths = (
                reversed(spec.paths)
                if inverse and isinstance(spec, RelationSequence)
                else iter(spec.paths)
            )
            previous = start
            for child in paths:
                first, last = self.compile(child, inverse, depth + 1)
                if isinstance(spec, RelationChoice):
                    self.arc(start, first)
                    self.arc(last, end)
                else:
                    self.arc(previous, first)
                    previous = last
            if isinstance(spec, RelationSequence):
                self.arc(previous, end)
        elif isinstance(spec, RelationRepeat):
            minimum, maximum = spec.minimum, spec.maximum
            if (
                type(minimum) is not int
                or minimum < 0
                or maximum is not None
                and (type(maximum) is not int or maximum < minimum)
            ):
                raise InputError("Repetition requires 0 <= minimum <= maximum")
            for count in (minimum, maximum):
                if count is not None and count > self.budget.limits.max_states:
                    raise LimitExceeded("Repetition expansion exceeds max_states")
            self.budget.consume("work", minimum + (maximum or 0))
            if maximum == 0:
                self.compile(spec.path, inverse, depth + 1)
            previous = start
            for _ in range(minimum):
                first, last = self.compile(spec.path, inverse, depth + 1)
                self.arc(previous, first)
                previous = last
            if maximum is None:
                self.arc(previous, end)
                first, last = self.compile(spec.path, inverse, depth + 1)
                self.arc(previous, first)
                self.arc(last, previous)
            else:
                for _ in range(maximum - minimum):
                    self.arc(previous, end)
                    first, last = self.compile(spec.path, inverse, depth + 1)
                    self.arc(previous, first)
                    previous = last
                self.arc(previous, end)
        else:
            raise InputError("Expected a typed relation-path specification")
        return start, end
