"""Finite named obligations over asserted ASK and SELECT query answers."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from rdflib import BNode, Literal, URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.queries.access import QueryResult, QueryScope
from bijux_ontology.rdf.queries.protocol import QueryTerm
from bijux_ontology.rdf.terms import literal_term


def _sequence(value: object) -> tuple[object, ...]:
    """Own a finite declaration sequence without accepting lazy rule programs."""
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise InputError("Query answer declarations require finite sequences")
    return tuple(value)


def _term(value: object) -> QueryTerm | None:
    """Own exact expected terms; blank labels have no cross-job identity."""
    if value is None:
        return None
    if isinstance(value, BNode):
        raise InputError("Expected answers cannot identify query-local blank nodes")
    if isinstance(value, URIRef):
        return URIRef(absolute_iri(str(value)))
    if isinstance(value, Literal):
        return literal_term(
            str(value),
            datatype=str(value.datatype) if value.datatype else None,
            language=value.language,
        )
    raise InputError("Expected answers require exact RDF terms or unbound None")


@dataclass(frozen=True)
class QueryAnswers:
    """An unordered finite set of exact solutions in the declared variable order.

    Duplicate rows do not change this set contract. Literal lexical forms remain
    distinct; language case and implicit xsd:string follow RDF term identity.
    Blank-node expectations need a query-level condition, not document labels.
    """

    variables: tuple[str, ...]
    rows: tuple[tuple[QueryTerm | None, ...], ...]

    def __post_init__(self) -> None:
        variables = _sequence(self.variables)
        if any(not isinstance(name, str) or not name for name in variables):
            raise InputError("Expected SELECT variables must be nonempty strings")
        if len(set(variables)) != len(variables):
            raise InputError("Expected SELECT variables must be unique")
        rows = []
        for raw in _sequence(self.rows):
            row = _sequence(raw)
            if len(row) != len(variables):
                raise InputError("Expected SELECT rows must match declared variables")
            rows.append(tuple(_term(value) for value in row))
        object.__setattr__(self, "variables", cast(tuple[str, ...], variables))
        object.__setattr__(self, "rows", tuple(rows))


@dataclass(frozen=True)
class QueryRule:
    """An ASK expectation, SELECT violation query or finite SELECT answer set.

    expected=None requires a SELECT with zero violating rows. A boolean requires
    an ASK with that answer. QueryAnswers requires its exact variable/answer set.
    Scope is explicit; source identifies an actual local query/specification file.
    """

    name: str
    query: str
    scope: QueryScope
    expected: bool | QueryAnswers | None = None
    source: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise InputError("Query rules require a nonempty name")
        if not isinstance(self.query, str) or not self.query.strip():
            raise InputError("Query rules require nonempty SPARQL text")
        if not isinstance(self.scope, QueryScope):
            raise InputError("Query rules require an explicit QueryScope")
        if (
            self.expected is not None
            and type(self.expected) is not bool
            and not isinstance(self.expected, QueryAnswers)
        ):
            raise InputError(
                "Expected query answer must be boolean, QueryAnswers or None"
            )
        if not isinstance(self.source, str):
            raise InputError("Query rule source must be an established filename")


@dataclass(frozen=True)
class QueryRuleResult:
    """Suite diagnostics beside named raw answers from queries actually executed."""

    report: Report
    results: tuple[tuple[str, QueryResult], ...] = ()
