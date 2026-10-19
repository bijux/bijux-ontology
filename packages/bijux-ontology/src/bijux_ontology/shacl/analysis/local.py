"""Finite local constraints and established per-shape prerequisites."""

from dataclasses import dataclass
from typing import Literal as Kind

from rdflib import URIRef
from rdflib.namespace import RDF, SH
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import IssueCollector
from bijux_ontology.rdf.structure import iter_rdf_list
from bijux_ontology.rdf.terms import bound_node, term_key
from bijux_ontology.shacl.parameters.reading import ParameterReader, count_value
from bijux_ontology.shacl.profiles.types import Triple

from .types import LocalConflict


@dataclass
class ShapeState:
    """Only established local prerequisites used by finite implication rules."""

    active: bool = True
    valid: bool = True
    property_shape: bool = False
    presence: bool = True


class LocalConstraints:
    """Retain paid local facts incrementally under the coordinator's allowance."""

    def __init__(self, parameters: ParameterReader, issues: IssueCollector) -> None:
        """Reuse the exact source reader, budget and diagnostic collector."""
        self.parameters = parameters
        self.budget = parameters.budget
        self.issues = issues
        self.conflicts: list[LocalConflict] = []
        self.states: dict[Node, ShapeState] = {}
        self.completed = 0

    def count(
        self, shape: Node, predicate: URIRef
    ) -> tuple[int | None, tuple[Triple, ...]]:
        """Keep exact cardinality witnesses beside the static proof policy."""
        value = self.parameters.one(shape, predicate)
        if value is None:
            return None, ()
        return count_value(value, self.budget), ((shape, predicate, value),)

    def conflict(
        self,
        shape: Node,
        reason: Kind["count_bounds", "fixed_count", "fixed_allowed", "allowed_count"],
        witnesses: tuple[Triple, ...],
        allowed: tuple[Node, ...] | None = None,
    ) -> None:
        """Retain one direct proof independently of optional invocation context."""
        self.budget.consume("states", 1 + len(witnesses) + len(allowed or ()))
        self.conflicts.append(LocalConflict(shape, reason, witnesses, allowed))

    def scan(self, shape: Node) -> None:
        """Check direct local cardinality/fixed-value/enumeration constraints only."""
        state = ShapeState()
        self.budget.consume("states")
        self.states[shape] = state
        try:
            state.active = not self.parameters.boolean(shape, SH.deactivated)
            if not state.active:
                self.completed += 1
                return
            paths = self.parameters.values(shape, SH.path, single=True)
            kinds = self.parameters.values(shape, RDF.type)
            state.property_shape = bool(paths)
            if (
                SH.PropertyShape in kinds
                and not paths
                or SH.NodeShape in kinds
                and paths
            ):
                raise InputError("SHACL shape role and property path conflict")
            minimum, min_witness = self.count(shape, SH.minCount)
            maximum, max_witness = self.count(shape, SH.maxCount)
            if not state.property_shape and (
                minimum is not None or maximum is not None
            ):
                raise InputError("SHACL count parameters require a property shape")
            fixed = self.parameters.values(shape, SH.hasValue)
            self.budget.consume("states", len(fixed))
            fixed_keys = {term_key(value) for value in fixed}
            fixed_witness = tuple((shape, SH.hasValue, value) for value in fixed)
            lower = max(minimum or 0, len(fixed_keys))
            state.presence = not state.property_shape or lower > 0
            if minimum is not None and maximum is not None and minimum > maximum:
                self.conflict(shape, "count_bounds", min_witness + max_witness)
            if maximum is not None and len(fixed_keys) > maximum:
                self.conflict(shape, "fixed_count", fixed_witness + max_witness)
            heads = self.parameters.values(shape, SH["in"], single=True)
            if heads:
                allowed = set()
                allowed_values = []
                witnesses: list[Triple] = [(shape, SH["in"], heads[0])]
                if (RDF.nil, RDF.first, None) in self.parameters.graph or (
                    RDF.nil,
                    RDF.rest,
                    None,
                ) in self.parameters.graph:
                    raise InputError("SHACL rdf:nil must not have list structure")
                for value in iter_rdf_list(
                    self.parameters.graph,
                    heads[0],
                    max_items=self.budget.limits.max_states,
                    budget=self.budget,
                ):
                    self.budget.consume("states", 2)
                    key = term_key(value)
                    if key not in allowed:
                        allowed.add(key)
                        allowed_values.append(value)
                if fixed_keys - allowed:
                    self.conflict(
                        shape,
                        "fixed_allowed",
                        tuple(witnesses) + fixed_witness,
                        tuple(allowed_values),
                    )
                required = lower if state.property_shape else max(1, len(fixed_keys))
                if required > len(allowed):
                    self.conflict(
                        shape,
                        "allowed_count",
                        tuple(witnesses) + min_witness + fixed_witness,
                        tuple(allowed_values),
                    )
            if not state.property_shape and len(fixed_keys) > 1:
                self.conflict(shape, "fixed_count", fixed_witness)
            self.completed += 1
        except InputError as error:
            if isinstance(error, LimitExceeded):
                raise
            state.valid = False
            bound_node(shape, self.budget)
            self.issues.append(
                Issue(
                    "shacl.analysis_metadata_unverified",
                    str(error),
                    str(shape),
                    kind="unverified",
                )
            )
