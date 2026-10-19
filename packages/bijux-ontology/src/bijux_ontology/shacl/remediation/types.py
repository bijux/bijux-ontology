"""Application-authored alternatives driven by actual SHACL findings."""

from collections.abc import Callable
from dataclasses import dataclass

from rdflib import URIRef

from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.migrations.candidates import MigrationCandidate
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.shacl.execution.results import ShaclResult
from bijux_ontology.shacl.findings import ShaclFinding


@dataclass(frozen=True)
class RemediationStrategy:
    """Propose one independent alternative for an exact shape/component pair.

    The trusted Python callback receives a private source and actual findings.
    Return an owned proposed snapshot, or None when required values are unknown.
    Multiple strategies express alternatives; they are never silently composed.
    """

    name: str
    source_shape: URIRef
    constraint_component: URIRef
    propose: Callable[[PreparedSource, tuple[ShaclFinding, ...]], PreparedSource | None]


@dataclass(frozen=True)
class RemediationAlternative:
    """One reviewable patch with its complete required postcondition evidence."""

    strategy: str
    candidate: MigrationCandidate


@dataclass(frozen=True)
class RemediationResult:
    """Keep baseline failures, independent alternatives and unanswered findings."""

    baseline: ShaclResult | None
    alternatives: tuple[RemediationAlternative, ...]
    unresolved: tuple[ShaclFinding, ...]
    report: Report
