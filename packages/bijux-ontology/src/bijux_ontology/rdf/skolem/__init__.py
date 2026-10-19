"""Opt-in scoped RDF identity replacement with snapshot-bound reversal."""

from bijux_ontology.rdf.skolem.checking import deskolemize, skolemize
from bijux_ontology.rdf.skolem.types import SkolemBinding, SkolemMap, SkolemResult

__all__ = ["SkolemBinding", "SkolemMap", "SkolemResult", "deskolemize", "skolemize"]
