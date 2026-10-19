"""Policy-neutral original record values, presence and selected RDF scope."""

from dataclasses import dataclass, field
from typing import Literal as Coverage

from rdflib import BNode, Literal, URIRef
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.rdf.paths.traversal import PathIdentityUnavailable, path_values
from bijux_ontology.rdf.sources.metadata import SnapshotMetadata
from bijux_ontology.rdf.sources.selection import SelectedRead, select_read
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_node

FiniteSteps = tuple[tuple[URIRef, bool], ...]


def _bound_subject(subject: URIRef | BNode, budget: Budget) -> None:
    """Qualify a resource before graph lookup without requiring its presence."""
    if not isinstance(subject, URIRef | BNode):
        raise InputError("Record readers require an RDF resource subject")
    bound_node(subject, budget)


@dataclass(frozen=True)
class RecordPresence:
    """Selected subject/object occurrence and actual blank ownership are distinct."""

    present: bool
    owned: bool


@dataclass(frozen=True)
class RecordValues:
    """Original endpoints beside traversal coverage and exact literal evidence."""

    values: tuple[Node, ...]
    coverage: Coverage[
        "complete", "unsupported_path", "literal_identity", "foreign_blank"
    ]
    exact_literals: bool
    reason: str | None = None

    @property
    def complete(self) -> bool:
        """Only a completed traversal establishes absence or multiplicity."""
        return self.coverage == "complete"


@dataclass(frozen=True)
class RecordReader:
    """Read one already owned projection without applying a consumer policy."""

    source: PreparedSource
    selected: SelectedRead
    metadata: SnapshotMetadata
    # Positive witnesses retain their objects for this immutable selected graph.
    _owned_blanks: dict[int, BNode] = field(
        default_factory=dict, init=False, compare=False, repr=False
    )

    def presence(self, subject: URIRef | BNode, budget: Budget) -> RecordPresence:
        """Observe subject/object presence without treating an equal blank as owned."""
        _bound_subject(subject, budget)
        budget.consume("states")
        budget.consume("bytes", 32)
        graph = self.selected.read()
        if isinstance(subject, BNode):
            if self._owned_blanks.get(id(subject)) is subject:
                return RecordPresence(True, True)
            for triple in graph:
                budget.consume("work")
                if triple[0] is subject or triple[2] is subject:
                    self.selected.read()
                    budget.consume("states")
                    budget.consume("bytes", 32)
                    self._owned_blanks[id(subject)] = subject
                    return RecordPresence(True, True)
            self.selected.read()
            return RecordPresence(False, False)
        for pattern in ((subject, None, None), (None, None, subject)):
            for _ in graph.triples(pattern):
                budget.consume("work")
                self.selected.read()
                return RecordPresence(True, True)
        self.selected.read()
        return RecordPresence(False, True)

    def described(self, subject: URIRef | BNode, budget: Budget) -> bool:
        """Observe an outgoing description, preserving exact blank subject identity."""
        _bound_subject(subject, budget)
        graph = self.selected.read()
        if isinstance(subject, BNode) and not self.presence(subject, budget).owned:
            return False
        for triple in graph.triples((subject, None, None)):
            budget.consume("work")
            budget.consume("triples")
            if not isinstance(subject, BNode) or triple[0] is subject:
                self.selected.read()
                return True
        self.selected.read()
        return False

    def values(
        self, subject: URIRef | BNode, steps: FiniteSteps | None, budget: Budget
    ) -> RecordValues:
        """Reuse finite RDF traversal; incomplete coverage never becomes absence."""
        _bound_subject(subject, budget)
        budget.consume("states")
        budget.consume("bytes", 64)
        graph = self.selected.read()
        if isinstance(subject, BNode) and not self.presence(subject, budget).owned:
            return RecordValues((), "foreign_blank", False)
        if steps is None:
            return RecordValues((), "unsupported_path", False)
        faithful = self.metadata.literal_fidelity in ("source_terms", "supplied_terms")
        try:
            values = path_values(
                graph, subject, steps, budget, literal_identity=faithful
            )
        except PathIdentityUnavailable as error:
            self.selected.read()
            reason = str(error)
            bound_text(reason, budget)
            return RecordValues((), "literal_identity", False, reason)
        self.selected.read()
        return RecordValues(
            values,
            "complete",
            faithful or not any(isinstance(value, Literal) for value in values),
        )


def read_records(
    source: PreparedSource,
    budget: Budget,
    *,
    graph_name: GraphSelection | None = None,
    union: bool = False,
) -> RecordReader:
    """Capture one qualified source projection for an explicit record population."""
    selected = select_read(source, budget, graph_name=graph_name, union=union)
    metadata = SnapshotMetadata.capture(source, budget)
    selected.read()
    budget.consume("states")
    budget.consume("bytes", 112)
    return RecordReader(source, selected, metadata)
