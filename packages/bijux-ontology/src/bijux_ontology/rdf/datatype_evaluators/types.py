"""Explicit caller capabilities over exact custom datatype IRIs and RDF terms."""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal as Choice

from rdflib import BNode, Literal
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.absence import Absent
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.identifiers import absolute_iri

DatatypeRelation = Choice["equal", "subset", "overlap", "disjoint", "unknown"]
Regime = Choice["application", "rdf", "owl-dl"]
DatatypeScalar = bool | int | Decimal | str | bytes
Validator = Callable[[Literal], bool | None]
Converter = Callable[[Literal], DatatypeScalar | None]
RelationEvaluator = Callable[[str, Regime], DatatypeRelation | None]


def _text(value: str, budget: Budget) -> None:
    """Bound original metadata before encoding, hashing or retaining new keys."""
    if not isinstance(value, str):
        raise InputError("Datatype metadata must be text")
    budget.consume("work")
    if len(value) > budget.limits.max_bytes:
        raise LimitExceeded("Datatype metadata exceeds max_bytes")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise InputError("Datatype metadata requires valid Unicode") from error
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Datatype metadata exceeds max_bytes")
    budget.consume("bytes", size)


@dataclass(frozen=True)
class DatatypeEvaluator:
    """Separate supplied sample, conversion and actual-to-required capabilities.

    Validators are conjoined; an empty tuple supplies no validity capability.
    Callables retain their ordinary mutable state and in-process lifetime. The
    catalog never freezes their behavior or caches a successful invocation.
    """

    iri: str
    validators: tuple[Validator, ...] = ()
    converter: Converter | None = None
    relation: RelationEvaluator | None = None

    def __post_init__(self) -> None:
        """Require named custom identifiers and finite explicit callable tuples."""
        budget = Budget()
        if not isinstance(self.iri, str) or isinstance(self.iri, BNode | Literal):
            raise InputError("Datatype evaluators require a named IRI")
        _text(self.iri, budget)
        absolute_iri(self.iri)
        if self.iri.startswith(tuple(map(str, (XSD, RDF, RDFS, OWL)))):
            raise InputError("Custom evaluators cannot replace standard datatype IRIs")
        if (
            type(self.validators) is not tuple
            or len(self.validators) > Limits().max_states
        ):
            raise InputError("Validators require a bounded immutable tuple")
        for validator in self.validators:
            budget.consume("work")
            budget.consume("states")
            if not callable(validator):
                raise InputError("Datatype validators must be callable")
        if self.converter is not None and not callable(self.converter):
            raise InputError("Datatype conversion must be an explicit callable")
        if self.relation is not None and not callable(self.relation):
            raise InputError("Datatype inclusion must be an explicit callable")


@dataclass(frozen=True)
class DatatypeConversion:
    """Original exact RDF literal and a qualified bounded immutable scalar.

    Conversion does not establish a relation to another datatype or regime.
    ABSENT retains unavailable conversion distinctly from any scalar value.
    """

    literal: Literal
    value: DatatypeScalar | Absent
    report: Report
