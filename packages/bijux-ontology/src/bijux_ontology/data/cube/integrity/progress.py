"""Finite integrity families over the same owned normalized Cube facts."""

from rdflib.term import Node

from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import (
    IssueCollector,
    retain_report,
)
from bijux_ontology.rdf.terms import bound_node

from ..facts import CubeFacts


class CubeIntegrity:
    """One allowance and current rule diagnostics beside shared Cube facts."""

    def __init__(self, read: CubeFacts, closed: frozenset[str]) -> None:
        self.read = read
        self.graph = read.graph
        self.budget = read.budget
        self.closed = closed
        self.issues = IssueCollector(self.budget)

    def finding(self, code: str, subject: Node, message: str) -> None:
        """Retain the actual located integrity violation under the shared allowance."""
        bound_node(subject, self.budget)
        self.issues.append(Issue("cube." + code, message, str(subject)))

    def unknown(self, subject: Node, message: str) -> None:
        """Unavailable scope or capabilities cannot count as completed conformance."""
        bound_node(subject, self.budget)
        self.issues.append(
            Issue(
                "cube.constraint_unverified", message, str(subject), kind="unverified"
            )
        )

    def accept(self, report: Report) -> None:
        """Qualify native child evidence before retaining any diagnostic references."""
        retain_report(report, self.budget, self.issues)
