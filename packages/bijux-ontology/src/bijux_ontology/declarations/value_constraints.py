"""Explicit application predicates over actual RDF values, independent of OWL."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from rdflib import BNode, URIRef
from rdflib import Literal as RDFLiteral

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Limits
from bijux_ontology.rdf.identifiers import absolute_iri

from .enumerations import ValueEnumeration


@dataclass(frozen=True)
class ValueConstraints:
    """Conjoined predicates; enums use RDF identity, intervals numeric values.

    String lengths count original Unicode code points. Patterns use RE2 UTF-8
    syntax with explicit fullmatch/search semantics. Namespaces are exact IRI
    spelling prefixes. No predicate normalizes or replaces a supplied RDF term.
    """

    enumeration: tuple[URIRef | RDFLiteral, ...] | ValueEnumeration | None = None
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    minimum_inclusive: bool = True
    maximum_inclusive: bool = True
    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    pattern_mode: Literal["fullmatch", "search"] = "fullmatch"
    iri_namespaces: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Require finite immutable inventories and exact finite numeric bounds."""
        if self.enumeration is not None and not isinstance(
            self.enumeration, ValueEnumeration
        ):
            if (
                type(self.enumeration) is not tuple
                or len(self.enumeration) > Limits().max_states
            ):
                raise InputError("Enumeration requires a bounded immutable term tuple")
            for term in self.enumeration:
                if not isinstance(term, URIRef | RDFLiteral):
                    raise InputError(
                        "Enumeration values must be named IRIs or literals"
                    )
                if isinstance(term, URIRef):
                    absolute_iri(str(term))
        if (
            type(self.iri_namespaces) is not tuple
            or len(self.iri_namespaces) > Limits().max_states
        ):
            raise InputError("IRI namespaces require a bounded immutable tuple")
        for namespace in self.iri_namespaces:
            if not isinstance(namespace, str) or isinstance(
                namespace, RDFLiteral | BNode
            ):
                raise InputError("IRI namespaces must be exact IRI strings")
            absolute_iri(namespace)
        for bound in (self.minimum, self.maximum):
            if bound is not None and (
                not isinstance(bound, Decimal) or not bound.is_finite()
            ):
                raise InputError("Numeric bounds must be finite Decimal values")
        for flag in (self.minimum_inclusive, self.maximum_inclusive):
            if type(flag) is not bool:
                raise InputError("Interval inclusivity must be boolean")
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise InputError("Numeric minimum exceeds maximum")
        for length in (self.min_length, self.max_length):
            if length is not None and (type(length) is not int or length < 0):
                raise InputError("String lengths must be nonnegative integers")
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise InputError("String minimum length exceeds maximum")
        if self.pattern is not None and not isinstance(self.pattern, str):
            raise InputError("A pattern must be text")
        if self.pattern_mode not in ("fullmatch", "search"):
            raise InputError("Pattern mode must be fullmatch or search")
