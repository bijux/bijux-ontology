"""Explicit typed parameter declarations and finite author-owned binding slots."""

from dataclasses import dataclass
from typing import Literal

from rdflib import Literal as RdfLiteral
from rdflib import URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.limits import Limits
from bijux_ontology.shacl.authoring.types import ShaclNodeShape, ShaclPropertyShape
from bijux_ontology.workflows.projects.bundles import PreparedBundle
from bijux_ontology.workflows.projects.profiles.execution import EffectiveProfile
from bijux_ontology.workflows.projects.profiles.types import ValidationProfile


def _inventory(*values: object) -> None:
    """Keep authored containers finite and immutable before preparation."""
    for value in values:
        if type(value) is not tuple or len(value) > Limits().max_states:
            raise InputError("Profile parameter inventories require bounded tuples")


@dataclass(frozen=True)
class ProfileParameter:
    """Declare one required parameter; enum values are exact author-owned strings."""

    name: str
    kind: Literal["rdf_term", "integer", "enum", "resource"]
    choices: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Retain a finite immutable enum inventory."""
        _inventory(self.choices)


@dataclass(frozen=True)
class ProfileResource:
    """Select one owned locally closed RDF bundle and explicit named-term inventory."""

    bundle: PreparedBundle
    source: str
    terms: tuple[str, ...]

    def __post_init__(self) -> None:
        """Require immutable term intent without inferring vocabulary membership."""
        _inventory(self.terms)


@dataclass(frozen=True)
class ProfileShape:
    """Declare the complete typed baseline for one existing shapes resource."""

    resource: str
    definitions: tuple[ShaclNodeShape | ShaclPropertyShape, ...]

    def __post_init__(self) -> None:
        """Own the complete finite definition inventory."""
        _inventory(self.definitions)


@dataclass(frozen=True)
class ConstraintSlot:
    """Bind a finite supported attribute on an explicitly indexed constraint."""

    parameter: str
    resource: str
    shape: URIRef
    constraint: int
    attribute: Literal[
        "has_value",
        "in_values",
        "class_iri",
        "datatype",
        "node_kind",
        "minimum",
        "maximum",
    ]


@dataclass(frozen=True)
class InventorySlot:
    """Bind a resource's explicit terms into one ordinary project inventory file."""

    parameter: str
    path: str


@dataclass(frozen=True)
class ResourceSlot:
    """Rebind an existing local RDF alias to the selected actual descriptor."""

    parameter: str
    alias: str


@dataclass(frozen=True)
class ParameterizedProfile:
    """Finite explicit slots over one ordinary profile, never executable templates."""

    profile: ValidationProfile
    parameters: tuple[ProfileParameter, ...]
    shapes: tuple[ProfileShape, ...] = ()
    constraints: tuple[ConstraintSlot, ...] = ()
    inventories: tuple[InventorySlot, ...] = ()
    resources: tuple[ResourceSlot, ...] = ()

    def __post_init__(self) -> None:
        """Keep all authored slot inventories independent of mutable caller lists."""
        _inventory(
            self.parameters,
            self.shapes,
            self.constraints,
            self.inventories,
            self.resources,
        )


@dataclass(frozen=True)
class ProfileInstance:
    """Prepared ordinary artifacts and composition, separate from data conformance."""

    profile: ValidationProfile | None
    effective: EffectiveProfile | None
    report: Report


ParameterValue = URIRef | RdfLiteral | int | str | ProfileResource
