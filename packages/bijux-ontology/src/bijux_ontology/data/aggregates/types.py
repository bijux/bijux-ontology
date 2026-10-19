"""Explicit complete groups and exact arithmetic beside original contributions."""

from dataclasses import dataclass
from fractions import Fraction

from rdflib import Literal
from rdflib.term import Node

from bijux_ontology.data.keys.types import KeyRecord
from bijux_ontology.declarations.aggregates import AggregateContract
from bijux_ontology.reports import Report


@dataclass(frozen=True)
class AggregateGroup:
    """One supplied population and actual declared value or caller-selected bound.

    complete concerns all required partitions, not just successful extraction.
    Every occurrence needs a unique identifier, including repeated descriptions
    of one entity. Named entities deduplicate by IRI; blank entities retain their
    exact owned source/scope identity. RDF graphs themselves have set semantics.
    """

    identifier: str
    members: tuple[KeyRecord, ...]
    expected: Literal
    complete: bool


@dataclass(frozen=True)
class AggregateContribution:
    """One original record description and its exact selected RDF value/unit."""

    record: KeyRecord
    values: tuple[Node, ...]


@dataclass(frozen=True)
class AggregateAnswer:
    """An exact rational result, independently qualified against its population."""

    group: str
    expected: Literal
    actual: Fraction | None
    contributions: tuple[AggregateContribution, ...]
    excluded: tuple[str, ...]
    report: Report


@dataclass(frozen=True)
class AggregateCheck:
    """Finite group answers, never a claim about unselected corpus partitions."""

    contract: AggregateContract | None
    groups: tuple[AggregateAnswer, ...]
    report: Report
