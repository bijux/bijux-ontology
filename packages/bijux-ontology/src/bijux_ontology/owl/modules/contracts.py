"""Authored module signatures independent of provider wiring and logical evaluation."""

from dataclasses import dataclass
from typing import Literal

from bijux_ontology.declarations.types import TermDeclaration
from bijux_ontology.owl.obligations import OntologyObligation


@dataclass(frozen=True)
class ModulePropertyRequirement:
    """Require an effective named endpoint guarantee, not property nonemptiness.

    Domain/range constraints are conjunctive: one sufficient included endpoint
    establishes this selected guarantee. Disjoint datatype comparisons alone
    cannot disprove an OWL range entailment, because the property may be empty.
    """

    property: str
    position: Literal["domain", "range"]
    relation: Literal["subsumption", "datatype_inclusion"]
    required: str
    location: str = ""


@dataclass(frozen=True)
class ModuleRequirement:
    """One external public interface with independently selected proof questions."""

    name: str
    exports: tuple[TermDeclaration, ...]
    obligations: tuple[OntologyObligation, ...] = ()
    properties: tuple[ModulePropertyRequirement, ...] = ()


@dataclass(frozen=True)
class ModuleContract:
    """Named exports and explicit external requirements, without inferred owners."""

    name: str
    exports: tuple[TermDeclaration, ...]
    requires: tuple[ModuleRequirement, ...] = ()
