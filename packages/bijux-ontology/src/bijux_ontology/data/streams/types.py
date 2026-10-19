"""Exact retained statement evidence and separate streaming validation scopes."""

from dataclasses import dataclass
from typing import Literal, TypeAlias

from rdflib import BNode, URIRef

from bijux_ontology.rdf.queries.protocol import QueryTerm
from bijux_ontology.reports import Issue, Report

GlobalCheck: TypeAlias = Literal["shapes", "identity", "cardinality"]


@dataclass(frozen=True)
class StreamStatement:
    """One actual parser occurrence, indexed from one within this source only.

    Blank terms share identity only among this result's retained statements.
    None denotes the default graph, not a union or an absent graph inventory.
    """

    index: int
    subject: URIRef | BNode
    predicate: URIRef
    value: QueryTerm
    graph: URIRef | BNode | None


@dataclass(frozen=True)
class StreamFinding:
    """One original validator issue tied to its exact statement and graph roles."""

    statement: StreamStatement
    issue: Issue


@dataclass(frozen=True)
class StreamCheck:
    """Finite statement-local evidence, never a global dataset certificate.

    No complete data graph or all-subject blank index is retained. The digest is
    present only after the reader reached EOF. Completion of parsing, selected
    local checks and any explicitly required global scope remain independent.
    """

    document_iri: str | None
    effective_url: str | None
    statements: int
    selected: int
    bytes_read: int
    source_sha256: str | None
    findings: tuple[StreamFinding, ...]
    parse_report: Report
    local_report: Report
    global_report: Report | None
    report: Report
