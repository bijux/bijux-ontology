"""Finite contextual obligations for reused and recursive model declarations."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from rdflib import Graph, URIRef

from bijux_ontology.declarations.types import FieldDeclaration, ModelDeclaration
from bijux_ontology.limits import Budget
from bijux_ontology.owl.properties import PropertyContract, property_contract
from bijux_ontology.rdf.literals import Regime


@dataclass(frozen=True)
class ModelObligation:
    """One distinct incoming type state and its finite occurrence references."""

    identifier: str
    types: tuple[str, ...]
    locations: tuple[str, ...]


class ModelContracts:
    """Own property caches and finite propagation within one checking operation."""

    def __init__(self, graph: Graph, regime: Regime, budget: Budget) -> None:
        self.graph = graph
        self.regime = regime
        self.budget = budget
        self._properties: dict[tuple[str, bool], PropertyContract] = {}

    def property(self, field: FieldDeclaration) -> PropertyContract:
        """Reuse an effective endpoint contract for identical property directions."""
        key = field.predicate, field.reverse
        if key not in self._properties:
            self._properties[key] = property_contract(
                self.graph,
                field.predicate,
                reverse=field.reverse,
                annotation_contracts=self.regime != "owl-dl",
                budget=self.budget,
            )
        return self._properties[key]

    def guaranteed_types(self, model: ModelDeclaration) -> tuple[str, ...]:
        """Combine only domains of fields explicitly guaranteed to be emitted."""
        types = set(model.types)
        for field in model.fields:
            self.budget.consume("work")
            if field.emission == "guaranteed" and not field.predicate.startswith("@"):
                types.update(
                    str(node)
                    for node in self.property(field).parents
                    if isinstance(node, URIRef)
                )
        return tuple(sorted(types))

    def obligations(
        self, registry: Mapping[str, ModelDeclaration]
    ) -> Iterator[ModelObligation]:
        """Solve finite incoming type states without unrolling document paths.

        Paths identify declaration edges, never recursively growing document
        paths. Repeated states retain all incoming edge locations after closure.
        """
        states: dict[tuple[str, tuple[str, ...]], set[str]] = {
            (identifier, ()): set() for identifier in registry
        }
        pending = list(states)
        solved: dict[tuple[str, tuple[str, ...]], tuple[str, ...]] = {}
        while pending:
            identifier, incoming = pending.pop()
            key = identifier, incoming
            if key in solved:
                continue
            self.budget.consume("states")
            model = registry[identifier]
            types = tuple(sorted(set(incoming) | set(self.guaranteed_types(model))))
            solved[key] = types
            for field in model.fields:
                self.budget.consume("work")
                if field.predicate.startswith("@"):
                    continue
                contract = self.property(field)
                for index, value in enumerate(field.values):
                    if (
                        value.model not in registry
                        or value.kind == "unknown"
                        or value.container not in (None, "@set", "@list")
                    ):
                        continue
                    assert value.model is not None
                    required = tuple(
                        sorted(
                            set(value.types)
                            | {
                                str(node)
                                for node in (
                                    ()
                                    if value.container == "@list"
                                    else contract.values
                                )
                                if isinstance(node, URIRef)
                            }
                        )
                    )
                    child = value.model, required
                    origin = field.location or f"{identifier}.{field.name}"
                    # The incoming state label keeps reused contextual edges distinct.
                    context = f" with {', '.join(incoming)}" if incoming else ""
                    edge = f"{origin}[{index}]{context}"
                    if child not in states:
                        states[child] = set()
                        pending.append(child)
                    states[child].add(edge)
        for (identifier, incoming), types in solved.items():
            if incoming:
                yield ModelObligation(
                    identifier, types, tuple(sorted(states[(identifier, incoming)]))
                )
