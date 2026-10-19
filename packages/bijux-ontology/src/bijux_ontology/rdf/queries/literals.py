"""Explicit literal-construction limits in the maintained SPARQL backend."""

from collections.abc import Iterable, Mapping

from rdflib import Literal, URIRef
from rdflib.namespace import XSD
from rdflib.plugins.sparql.parserutils import CompValue

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget

_NORMALIZED = frozenset((XSD.token, XSD.normalizedString))


class LiteralScopeUnsupported(InputError):
    """A backend constructor cannot establish exact RDF literal spelling."""


def check_literal_scope(algebra: object, budget: Budget) -> None:
    """Reject lossy query constants and constructors using maintained algebra.

    Data terms can remain exact, but RDFLib's query parser and STRDT/type casts
    reconstruct these string datatypes. Dynamic STRDT datatypes cannot prove
    that this boundary is absent. No lexical regex or global parser patch is used.
    """
    pending = [(algebra, 0)]
    while pending:
        value, depth = pending.pop()
        budget.consume("work")
        budget.consume("states")
        if depth > budget.limits.max_depth:
            raise LimitExceeded("SPARQL literal qualification exceeds max_depth")
        if isinstance(value, Literal):
            if value.datatype in _NORMALIZED:
                raise LiteralScopeUnsupported(
                    "RDFLib query literals normalize token/normalizedString spelling"
                )
        elif isinstance(value, CompValue):
            if value.name == "Function" and value.get("iri") in _NORMALIZED:
                raise LiteralScopeUnsupported(
                    "RDFLib casts normalize token/normalizedString spelling"
                )
            if value.name == "Builtin_STRDT":
                datatype = value.get("arg2")
                if not isinstance(datatype, URIRef) or datatype in _NORMALIZED:
                    raise LiteralScopeUnsupported(
                        "STRDT requires a fixed datatype whose lexical spelling "
                        "the RDFLib backend preserves"
                    )
            pending.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, Mapping):
            pending.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
            pending.extend((child, depth + 1) for child in value)
