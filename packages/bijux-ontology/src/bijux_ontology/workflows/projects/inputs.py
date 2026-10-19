"""Typed runtime SHACL data, separate from portable JSON project configuration."""

from collections.abc import Mapping
from dataclasses import dataclass
from itertools import chain

from rdflib import BNode, URIRef

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, bound_text
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.shacl.profiles.resources import _capture


@dataclass(frozen=True)
class ShaclJobInput:
    """Apply named shape roots to exact terms in one existing owned data snapshot."""

    source: PreparedSource
    focus_nodes: tuple[URIRef | BNode, ...]
    use_shapes: tuple[URIRef, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.source, PreparedSource):
            raise InputError("Runtime SHACL data requires an owned snapshot")
        budget = Budget()
        focus = _capture(self.focus_nodes, budget)
        shapes = _capture(self.use_shapes, budget)
        if not focus or not shapes:
            raise InputError("Runtime SHACL inputs require focus nodes and shape roots")
        for node in focus:
            if not isinstance(node, URIRef | BNode):
                raise InputError("Runtime SHACL focus selections require RDF resources")
            bound_node(node, budget)
        for shape in shapes:
            if not isinstance(shape, URIRef):
                raise InputError("Runtime SHACL roots require named IRIs")
            bound_node(shape, budget)
        object.__setattr__(self, "focus_nodes", focus)
        object.__setattr__(self, "use_shapes", shapes)


def capture_job_inputs(
    values: Mapping[str, ShaclJobInput] | None, budget: Budget
) -> dict[str, ShaclJobInput]:
    """Own finite named overrides before the ordinary project runner dispatches."""
    if values is None:
        return {}
    if not isinstance(values, Mapping):
        raise InputError("Runtime job inputs require a finite named mapping")
    budget.consume("states", len(values))
    budget.consume("bytes", 64 + 32 * len(values))
    result = {}
    for name, value in values.items():
        budget.consume("work")
        if not isinstance(name, str) or not name:
            raise InputError("Runtime job inputs require nonempty job names")
        bound_text(name, budget)
        if not isinstance(value, ShaclJobInput):
            raise InputError("Runtime job inputs require typed SHACL selections")
        budget.consume("states", len(value.focus_nodes) + len(value.use_shapes))
        for node in chain(value.focus_nodes, value.use_shapes):
            bound_node(node, budget)
        result[name] = value
    return result
