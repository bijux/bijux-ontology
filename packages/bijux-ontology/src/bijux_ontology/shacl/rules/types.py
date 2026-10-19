"""Separate asserted and derived RDF with explicit materialization acceptance."""

from dataclasses import dataclass
from typing import Literal

from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.migrations.candidates import MigrationCandidate
from bijux_ontology.rdf.snapshots import PreparedSource

RuleMode = Literal["ordered-pass", "global-fixed-point"]


@dataclass(frozen=True)
class RuleMaterialization:
    """A completed rule stage and required application checks, never a partial closure.

    Ordered-pass describes one complete ordered program execution. Global closure
    additionally requires an entire no-change pass in the same worker graph.
    Candidate export uses the ordinary explicit migration acceptance gate.
    """

    asserted: PreparedSource | None
    derived: PreparedSource | None
    candidate: MigrationCandidate | None
    mode: RuleMode
    passes: int
    report: Report
