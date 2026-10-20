"""Explicit value relations distinct from exact RDF term identity."""

from dataclasses import dataclass
from typing import Literal as Choice

from rdflib import Literal

from bijux_ontology.diagnostics.reports import Report

ComparisonRegime = Choice["sparql-1.1", "owl2-datatype-map"]
ValueRelation = Choice["equal", "different", "indeterminate"]


@dataclass(frozen=True)
class LiteralValueComparison:
    """Original caller terms and the raw result of one named value comparison.

    Terms retain their ordinary RDFLib caller lifetime. This frozen result never
    replaces their spelling or promises to freeze caller-mutated term metadata.
    A completed different answer is successful evaluation, not a finding.
    """

    left: Literal
    right: Literal
    regime: ComparisonRegime
    relation: ValueRelation
    report: Report
