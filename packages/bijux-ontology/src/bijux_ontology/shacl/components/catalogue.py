"""Retain the exact authored parameter and validator choices for explicit uses."""

from collections.abc import Mapping
from dataclasses import dataclass

from rdflib import BNode, URIRef
from rdflib.namespace import SH
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget
from bijux_ontology.shacl.authoring.parameters import ShaclParameter

from .types import ComponentValidator

ROLES = {
    "generic": SH.validator,
    "node": SH.nodeValidator,
    "property": SH.propertyValidator,
}


@dataclass(frozen=True)
class Validator:
    """Bind a typed query to the exact validator resource emitted into RDF."""

    node: Node
    predicate: URIRef
    definition: ComponentValidator


@dataclass(frozen=True)
class Definition:
    """Share already qualified names and paths across every explicit use."""

    parameters: Mapping[URIRef, ShaclParameter]
    names: frozenset[str]
    validators: tuple[Validator, ...]


@dataclass(frozen=True)
class Query:
    """Qualify one declared query in the same shape role used by the backend."""

    validator: Validator
    property_role: bool
    path: Node | None
    require_execution: bool


@dataclass(frozen=True)
class Use:
    """Preserve resolved binding terms and role choices without rediscovery."""

    shape: URIRef | BNode
    definition: Definition
    bindings: tuple[Node, ...]
    queries: tuple[Query, ...]


def bind_use(
    shape: URIRef | BNode,
    definition: Definition,
    bindings: list[Node],
    *,
    property_shape: bool,
    path: Node | None,
    budget: Budget,
) -> Use:
    """Distinguish all syntax jobs from the actually applicable validator role.

    An unused property validator receives the existing symbolic path only for
    syntax qualification. It adds no selected execution requirement.
    """
    role = SH.propertyValidator if property_shape else SH.nodeValidator
    selected = None
    for validator in definition.validators:
        budget.consume("work")
        if validator.predicate == role or (
            validator.predicate == SH.validator and selected is None
        ):
            selected = validator
    if selected is None:
        raise InputError("No applicable validator exists for this shape role")
    budget.consume("work", len(definition.validators))
    budget.consume("states", 1 + len(definition.validators))
    budget.reserve_storage(128 + 96 * len(definition.validators) + 8 * len(bindings))
    queries = []
    for validator in definition.validators:
        property_role = validator.predicate == SH.propertyValidator or (
            validator.predicate == SH.validator and property_shape
        )
        query_path = path if property_role else None
        if property_role and query_path is None:
            query_path = URIRef("urn:bijux:ontology:component-path")
        queries.append(
            Query(validator, property_role, query_path, validator is selected)
        )
    return Use(shape, definition, tuple(bindings), tuple(queries))
