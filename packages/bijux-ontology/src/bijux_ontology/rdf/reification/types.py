"""Original RDF reifier occurrences and opt-in application constraints."""

from dataclasses import dataclass
from typing import Literal as Choice

from rdflib import BNode, URIRef
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.roles.types import Statement
from bijux_ontology.rdf.snapshots import PreparedSource


@dataclass(frozen=True)
class ReificationRecord:
    """One original resource occurrence in one selected physical graph.

    Components retain distinct intrinsic RDF terms, never numeric value equality.
    All physical outgoing assertions remain in statements. Annotations are the
    non-component outgoing assertions; no OWL annotation-property role is inferred.
    A statement is exposed only for one complete, role-valid component tuple.
    Separate nodes describing the same tuple remain separate records.
    """

    node: URIRef | BNode
    graph: URIRef | BNode | None
    subjects: tuple[Node, ...]
    predicates: tuple[Node, ...]
    objects: tuple[Node, ...]
    explicit_type: bool
    statements: tuple[Statement, ...]
    annotations: tuple[Statement, ...]
    statement: Statement | None
    status: Choice["complete", "incomplete", "ambiguous", "invalid_roles"]


@dataclass(frozen=True)
class ReificationInspection:
    """Exact raw selected occurrences, missing selectors and extraction completion.

    A completed inspection inventories malformed records without accepting them.
    Missing nodes have no outgoing occurrence in any authorized selected graph;
    they are not assigned an invented graph or synthesized component assertion.
    """

    source: PreparedSource | None
    records: tuple[ReificationRecord, ...]
    missing: tuple[URIRef | BNode, ...]
    report: Report


@dataclass(frozen=True)
class ReificationContract:
    """Caller-selected application rules, never additional RDF semantic axioms.

    Subject resource and predicate IRI roles are always required. Statement
    uniqueness, when selected, concerns distinct reifiers in each physical graph.
    It is not inferred from shared components and never merges record identities.
    """

    require_complete: bool = True
    unique_components: bool = True
    require_type: bool = False
    unique_statements: bool = False

    def __post_init__(self) -> None:
        """Require explicit boolean policies and at least one actual obligation."""
        values = (
            self.require_complete,
            self.unique_components,
            self.require_type,
            self.unique_statements,
        )
        if any(type(value) is not bool for value in values) or not any(values):
            raise InputError("Select at least one boolean reification application rule")


@dataclass(frozen=True)
class ReificationCheck:
    """Actual extraction plus raw application findings, without a rewritten graph."""

    inspection: ReificationInspection
    contract: ReificationContract
    report: Report
