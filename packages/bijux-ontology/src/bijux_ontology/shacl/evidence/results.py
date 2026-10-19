"""Raw SHACL conformance and owned evidence independent of execution lifetime."""

from rdflib import Graph

from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.shacl.bindings import TermSelection
from bijux_ontology.shacl.evidence.findings import ShaclFinding
from bijux_ontology.shacl.targets.coverage import ShaclCoverage


class ShaclResult:
    """Raw conformance and owned standards-shaped evidence beside generic policy.

    Function counters record invocation attempts, including SPARQL errors;
    they do not assert that every invocation returned a bound RDF term.
    """

    def __init__(
        self,
        report: Report,
        *,
        conforms: bool | None = None,
        backend: str = "unavailable",
        graph: Graph | None = None,
        findings: tuple[ShaclFinding, ...] = (),
        root_findings: tuple[ShaclFinding, ...] = (),
        findings_complete: bool = True,
        registered_functions: tuple[str, ...] = (),
        function_calls: tuple[tuple[str, int], ...] = (),
        mode: str = "targets",
        focus_nodes: tuple[TermSelection, ...] = (),
        shapes: tuple[TermSelection, ...] = (),
        preflight: bool = False,
        shapes_count: int | None = None,
        coverage: ShaclCoverage | None = None,
        conformance_report: Report | None = None,
        constraints: Report | None = None,
    ) -> None:
        self.report = report
        self.conforms = conforms
        self.backend = backend
        self.findings = findings
        self.root_findings = root_findings
        self.findings_complete = findings_complete
        self.registered_functions = registered_functions
        self.function_calls = function_calls
        self.mode = mode
        self.focus_nodes = focus_nodes
        self.shapes = shapes
        self.preflight = preflight
        self.shapes_count = shapes_count
        self.coverage = coverage
        self.conformance_report = conformance_report or report
        self.constraints = constraints
        self._graph = Graph()
        if graph is not None:
            self._graph += graph

    def report_graph(self) -> Graph:
        """Export a mutable copy with RDF result identities preserved."""
        graph = Graph()
        graph += self._graph
        return graph
