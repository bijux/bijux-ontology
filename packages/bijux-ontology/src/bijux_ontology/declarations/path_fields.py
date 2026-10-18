"""Logical output fields spanning explicitly owned RDF structures."""

from dataclasses import dataclass, field
from typing import Literal

from rdflib import URIRef
from rdflib.paths import Path

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Limits
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.paths import RdfPath, capture_path

from .types import OutputRequirement, SubjectIdentity, ValueDeclaration


@dataclass(frozen=True)
class IntermediateRequirement:
    """Constrain distinct nodes after an explicit one-based path step.

    Exclusive ownership counts distinct predecessors on that step's selected
    edge within the effective graph. Shared references carry no such bound.
    Neither policy makes a claim about assertions outside the selected graph.
    """

    step: int
    minimum: int | None = None
    maximum: int | None = None
    types: tuple[str, ...] = ()
    identity: SubjectIdentity = SubjectIdentity()
    ownership: Literal["shared", "exclusive"] = "shared"
    described: bool = True

    def __post_init__(self) -> None:
        """Require explicit finite bounds and immutable asserted type names."""
        if type(self.step) is not int or self.step < 1:
            raise InputError("Intermediate steps use positive one-based indices")
        if self.step > Limits().max_states:
            raise InputError("Intermediate step exceeds the bounded path inventory")
        for count in (self.minimum, self.maximum):
            if count is not None and (type(count) is not int or count < 0):
                raise InputError("Intermediate bounds require nonnegative integers")
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise InputError("Intermediate minimum exceeds maximum")
        if type(self.types) is not tuple or len(self.types) > Limits().max_states:
            raise InputError("Intermediate types require a bounded immutable tuple")
        for iri in self.types:
            absolute_iri(iri)
        object.__setattr__(self, "types", tuple(str(iri) for iri in self.types))
        if not isinstance(self.identity, SubjectIdentity):
            raise InputError("Expected an explicit intermediate subject identity")
        if (
            self.ownership not in ("shared", "exclusive")
            or type(self.described) is not bool
        ):
            raise InputError(
                "Expected explicit intermediate ownership and description policies"
            )


@dataclass(frozen=True)
class PathFieldDeclaration:
    """A separate logical path field, never an ontology property declaration.

    Native evaluation supports finite predicate, sequence and inverse paths.
    Captured alternatives/repetitions remain explicit unsupported obligations.
    Counts concern distinct endpoints; intermediate requirements are independent.
    """

    name: str
    path: URIRef | Path | RdfPath
    values: tuple[ValueDeclaration, ...]
    output: OutputRequirement = field(
        default=OutputRequirement(allow_null=True, allow_empty=True), kw_only=True
    )
    intermediates: tuple[IntermediateRequirement, ...] = field(default=(), kw_only=True)

    def __post_init__(self) -> None:
        """Capture maintained path intent and finite declaration inventories."""
        if not isinstance(self.name, str) or not self.name.strip():
            raise InputError("Path fields require a nonempty logical name")
        object.__setattr__(self, "name", str(self.name))
        for inventory, kind in (
            (self.values, ValueDeclaration),
            (self.intermediates, IntermediateRequirement),
        ):
            if type(inventory) is not tuple or len(inventory) > Limits().max_states:
                raise InputError(
                    "Path field inventories require bounded immutable tuples"
                )
            if any(not isinstance(value, kind) for value in inventory):
                raise InputError("Expected typed path field declarations")
        if not self.values or not isinstance(self.output, OutputRequirement):
            raise InputError(
                "Path fields require value alternatives and an output requirement"
            )
        if len({value.step for value in self.intermediates}) != len(self.intermediates):
            raise InputError("Intermediate step requirements must be unique")
        object.__setattr__(self, "path", capture_path(self.path))
