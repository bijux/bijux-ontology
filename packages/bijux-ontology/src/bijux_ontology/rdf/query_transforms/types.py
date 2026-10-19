"""Explicit owned SPARQL text and exact RDF parameter bindings."""

from dataclasses import dataclass
from typing import Literal as Kind
from typing import cast

from rdflib import BNode, Literal, URIRef

from bijux_ontology.errors import InputError
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget, bound_text
from bijux_ontology.rdf.terms import bound_node, literal_term


@dataclass(frozen=True)
class SparqlBinding:
    """One native variable binding; blank parameters retain actual source identity."""

    name: str
    value: URIRef | BNode | Literal

    def __post_init__(self) -> None:
        """Own mutable literal metadata without copying an owned blank identity."""
        budget = Budget()
        if type(self.name) is not str or not self.name:
            raise InputError("SPARQL bindings require nonempty plain variable names")
        bound_text(self.name, budget)
        bound_node(self.value, budget)
        if isinstance(self.value, Literal):
            value = literal_term(
                str(self.value),
                datatype=str(self.value.datatype) if self.value.datatype else None,
                language=self.value.language,
            )
            object.__setattr__(self, "value", value)
        elif isinstance(self.value, URIRef):
            object.__setattr__(self, "value", URIRef(self.value))
        elif not isinstance(self.value, BNode):
            raise InputError("SPARQL bindings require RDF IRI, blank or literal terms")


@dataclass(frozen=True)
class SparqlProgram:
    """One caller-selected CONSTRUCT projection or finite local UPDATE operation."""

    kind: Kind["CONSTRUCT", "UPDATE"]
    text: str
    bindings: tuple[SparqlBinding, ...] = ()

    def __post_init__(self) -> None:
        """Capture finite immutable parameters without parsing or executing text."""
        budget = Budget()
        kind = cast(object, self.kind)
        if type(kind) is not str or kind not in ("CONSTRUCT", "UPDATE"):
            raise InputError("SPARQL transformations require CONSTRUCT or UPDATE")
        if type(self.text) is not str:
            raise InputError("SPARQL program text must be a plain string")
        bound_text(self.text, budget)
        if not self.text.strip():
            raise InputError("SPARQL program text must not be empty")
        object.__setattr__(
            self, "bindings", capture_items(self.bindings, SparqlBinding, budget)
        )
