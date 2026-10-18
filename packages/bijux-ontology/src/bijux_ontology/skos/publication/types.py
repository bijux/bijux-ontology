"""Explicit consumer top-concept publication obligations."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

from rdflib import BNode, URIRef
from rdflib import Literal as RdfLiteral
from rdflib.term import Node

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Limits
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.results import Edge


def _iri(value: object) -> str:
    if not isinstance(value, str) or isinstance(value, BNode | RdfLiteral):
        raise InputError("Publication identifiers require named IRI strings")
    if len(value) > Limits().max_bytes:
        raise LimitExceeded("Publication IRI exceeds max_bytes")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise InputError("Publication IRI requires valid Unicode text") from error
    if size > Limits().max_bytes:
        raise LimitExceeded("Publication IRI exceeds max_bytes")
    absolute_iri(value)
    return value


def _inventory(values: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(
        cast(object, values), (str, bytes)
    ):
        raise InputError("Publication inventories require finite IRI sequences")
    length = len(values)
    if length > Limits().max_states:
        raise LimitExceeded("Publication inventory exceeds max_states")
    owned = tuple(_iri(values[index]) for index in range(length))
    if len(set(owned)) != len(owned):
        raise InputError("Publication inventories require unique IRIs")
    return owned


@dataclass(frozen=True)
class TopConceptPolicy:
    """Per-scheme required/allowed roots and an authored direct broader policy.

    ``forbid-within-scheme`` permits proved other-scheme parents. Missing parent
    membership remains unresolved. No mapping implication or OWL claim is made.
    ``allowed=None`` disables the allowlist; an empty tuple permits no roots.
    """

    scheme: str
    required: tuple[str, ...] = ()
    allowed: tuple[str, ...] | None = None
    broader: Literal["allow", "forbid", "forbid-within-scheme"] = "allow"

    def __post_init__(self) -> None:
        _iri(self.scheme)
        required = _inventory(self.required)
        allowed = _inventory(self.allowed) if self.allowed is not None else None
        if self.broader not in ("allow", "forbid", "forbid-within-scheme"):
            raise InputError("Select an explicit supported root broader policy")
        if not required and allowed is None and self.broader == "allow":
            raise InputError("Top-concept policy requires an explicit obligation")
        if allowed is not None and not set(required) <= set(allowed):
            raise InputError("Required top concepts must be allowed")
        object.__setattr__(self, "required", required)
        object.__setattr__(self, "allowed", allowed)


@dataclass(frozen=True)
class TopConceptCheck:
    """Normalized published roots retain their original inverse assertions."""

    scheme: URIRef
    roots: tuple[Node, ...]
    assertions: tuple[Edge, ...]
    report: Report
