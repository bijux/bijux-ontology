"""Owned typed reusable component definitions and explicit shape applications."""

from dataclasses import dataclass
from typing import Literal as Kind

from rdflib import BNode, Literal, URIRef
from rdflib.term import Node

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.roles.classification import bound_text
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.reports import Report
from bijux_ontology.shacl.authoring.parameters import ShaclParameter
from bijux_ontology.shacl.capabilities import ShaclConstraintRequirement
from bijux_ontology.shacl.checking import ShaclResult
from bijux_ontology.shacl.profiles.resources import _capture


@dataclass(frozen=True)
class ComponentValidator:
    """A generic ASK or role-specific SELECT, without executable substitutions."""

    role: Kind["generic", "node", "property"] = "generic"
    ask: str | None = None
    select: str | None = None

    def __post_init__(self) -> None:
        """Own bounded original query text and enforce its declared validator role."""
        budget = Budget()
        if self.role not in ("generic", "node", "property"):
            raise InputError("Unknown component validator role")
        bodies = tuple(body for body in (self.ask, self.select) if body is not None)
        if len(bodies) != 1:
            raise InputError("A component validator requires exactly one query body")
        bound_text(bodies[0], budget)
        if not bodies[0].strip():
            raise InputError("Component query body cannot be empty")
        if (self.role == "generic") != (self.ask is not None):
            raise InputError("Generic validators require ASK; role validators SELECT")
        for field in ("ask", "select"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, str(value))


@dataclass(frozen=True)
class ShaclComponent:
    """A named component, reusable parameters, validator roles and RDF prefixes."""

    iri: str
    parameters: tuple[ShaclParameter, ...]
    validators: tuple[ComponentValidator, ...]
    prefixes: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        """Freeze finite supplied records; operation limits qualify their metadata."""
        budget = Budget()
        if isinstance(self.iri, BNode | Literal):
            raise InputError("Component identifiers require named IRI text")
        bound_text(self.iri, budget)
        object.__setattr__(self, "iri", str(self.iri))
        for name, expected in (
            ("parameters", ShaclParameter),
            ("validators", ComponentValidator),
        ):
            items = _capture(getattr(self, name), budget)
            if any(not isinstance(item, expected) for item in items):
                raise InputError("Component inventories require typed declarations")
            object.__setattr__(self, name, items)
        prefixes = []
        for item in _capture(self.prefixes, budget):
            pair = _capture(item, budget) if isinstance(item, tuple | list) else ()
            if len(pair) != 2:
                raise InputError("Component prefixes require prefix/namespace pairs")
            if isinstance(pair[1], BNode | Literal):
                raise InputError("Prefix namespaces require named IRI text")
            for text in pair:
                if not isinstance(text, str):
                    raise InputError("Component prefix metadata requires text")
                bound_text(text, budget)
            prefixes.append((str(pair[0]), str(pair[1])))
        object.__setattr__(self, "prefixes", tuple(prefixes))


@dataclass(frozen=True)
class ComponentUse:
    """Apply one component to one exact existing shape with explicit RDF bindings."""

    shape: URIRef | BNode
    component: str
    bindings: tuple[tuple[str, Node], ...]

    def __post_init__(self) -> None:
        """Own binding inventories without equating foreign anonymous identities."""
        budget = Budget()
        if not isinstance(self.shape, URIRef | BNode):
            raise InputError("Component uses require an exact RDF shape resource")
        if isinstance(self.component, BNode | Literal):
            raise InputError("Component references require named IRI text")
        bound_text(self.component, budget)
        object.__setattr__(self, "component", str(self.component))
        pairs = []
        for item in _capture(self.bindings, budget):
            pair = _capture(item, budget) if isinstance(item, tuple | list) else ()
            if (
                len(pair) != 2
                or not isinstance(pair[0], str)
                or not isinstance(pair[1], Node)
            ):
                raise InputError("Component bindings require parameter/RDF term pairs")
            if isinstance(pair[0], BNode | Literal):
                raise InputError("Binding parameters require named IRI text")
            bound_text(pair[0], budget)
            pairs.append((str(pair[0]), pair[1]))
        object.__setattr__(self, "bindings", tuple(pairs))


@dataclass(frozen=True)
class ComponentShapes:
    """Structural artifact beside optional maintained query qualification."""

    source: PreparedSource | None
    roots: tuple[URIRef | BNode, ...]
    requirements: tuple[ShaclConstraintRequirement, ...]
    structural_report: Report
    report: Report


@dataclass(frozen=True)
class ComponentCheck:
    """Raw maintained conformance beside required compilation and execution scope."""

    result: ShaclResult | None
    report: Report
