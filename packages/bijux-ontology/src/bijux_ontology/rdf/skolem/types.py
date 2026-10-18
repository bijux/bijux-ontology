"""Snapshot-bound evidence for explicit RDF identity replacement."""

from dataclasses import dataclass

from rdflib import BNode, URIRef

from bijux_ontology.errors import InputError
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.reports import Report


@dataclass(frozen=True)
class SkolemBinding:
    """One original owned blank node and its explicitly generated named identity."""

    original: BNode
    iri: URIRef

    def __post_init__(self) -> None:
        """Keep actual RDF object witnesses instead of lexical substitutes."""
        if not isinstance(self.original, BNode) or not isinstance(self.iri, URIRef):
            raise InputError("Skolem bindings require an actual BNode and URIRef")


@dataclass(frozen=True, init=False)
class SkolemMap:
    """Factory-owned correspondence between two exact in-memory snapshots.

    A serialized map, equal blank labels, or a cloned output does not establish
    this ownership. Shared operation maps require explicit owned BlankNodePairs.
    """

    original: PreparedSource
    output: PreparedSource
    base: str
    operation: str
    bindings: tuple[SkolemBinding, ...]

    def __init__(self) -> None:
        """Prevent public construction or dataclass replacement of ownership."""
        raise InputError("Skolem maps are created only by skolemize")

    @classmethod
    def _from_owned(
        cls,
        original: PreparedSource,
        output: PreparedSource,
        base: str,
        operation: str,
        bindings: tuple[SkolemBinding, ...],
    ) -> "SkolemMap":
        """Retain already qualified immutable correspondence and snapshot owners."""
        result = object.__new__(cls)
        for name, value in (
            ("original", original),
            ("output", output),
            ("base", base),
            ("operation", operation),
            ("bindings", bindings),
        ):
            object.__setattr__(result, name, value)
        return result


@dataclass(frozen=True)
class SkolemResult:
    """An atomic scoped candidate; named replacements change RDF identity."""

    source: PreparedSource | None
    mapping: SkolemMap | None
    report: Report
