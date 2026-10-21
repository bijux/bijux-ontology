"""Incremental selected topology path facts and exact local edge witnesses."""

import json
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

from rdflib import BNode, URIRef
from rdflib.term import Node

from bijux_ontology.data.records.reading import RecordReader
from bijux_ontology.declarations.topology import TopologyContract, TopologyRelation
from bijux_ontology.diagnostics.errors import LimitExceeded
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.rdf.paths.traversal import finite_path_steps
from bijux_ontology.rdf.paths.types import capture_path
from bijux_ontology.rdf.terms import term_key

from .capture import RecordCorpus, _Focus


@dataclass(frozen=True)
class _Link:
    """An established selected path, never a fabricated asserted RDF triple."""

    source: _Focus
    target: _Focus | None
    endpoint: Node
    relation: TopologyRelation
    path: str


@dataclass(frozen=True)
class _Gap:
    """One unresolved selected path, separate from a completed empty traversal."""

    relation: TopologyRelation
    path: str
    focus: _Focus | None = None
    reason: str | None = None


def _context(reader: RecordReader, budget: Budget) -> tuple[dict[str, object], int]:
    """Retain exact operation scope, not guessed graph names or source paths."""
    source = reader.metadata
    name = reader.selected.graph_name
    kind = reader.selected.kind
    for value in (
        source.document_iri,
        source.effective_url,
        source.literal_fidelity,
        str(name) if name is not None else None,
    ):
        if value is not None:
            bound_text(value, budget)

    graph = (
        term_key(name if isinstance(name, URIRef | BNode) else URIRef(name))
        if kind == "named" and name is not None
        else None
    )
    result: dict[str, object] = {
        "document_iri": source.document_iri,
        "effective_url": source.effective_url,
        "graph_kind": kind,
        "graph_name": graph,
    }
    size = 128 + 12 * (
        len(source.document_iri or "")
        + len(source.effective_url or "")
        + len(str(name or ""))
    )
    budget.consume("work", size)
    budget.consume("bytes", size)
    return result, size


@dataclass
class RelationEvidence:
    """One growing graph of actual paths, with independent role coverage.

    Reading yields each known link before requesting later data. Checking owns
    whether an index fact violates a policy and whether gaps permit absence.
    """

    context: dict[str, object]
    context_size: int
    reach_unknown: bool
    owner_unknown: bool
    adjacency: dict[str, dict[str, _Link]] = field(default_factory=dict)
    reachable_edges: dict[str, set[str]] = field(default_factory=dict)
    owners: dict[str, dict[str, _Link]] = field(default_factory=dict)
    checked: int = 0

    def links(
        self, corpus: RecordCorpus, contract: TopologyContract, operation: Budget
    ) -> Iterator[_Link | _Gap]:
        """Read only qualified finite paths; publish each established endpoint once."""
        relations = []
        for relation in contract.relations:
            bound_text(relation.name, operation)
            path = capture_path(relation.path, budget=operation)
            steps = finite_path_steps(path, operation)
            readable = path.readable
            bound_text(readable, operation)
            relations.append((relation, steps, readable))
            if steps is None:
                yield _Gap(relation, readable)
        for focus in corpus.nodes.values():
            for relation, steps, readable in relations:
                if steps is None:
                    continue
                observed = corpus.reader.values(focus.record.subject, steps, operation)
                if not observed.complete:
                    yield _Gap(relation, readable, focus, observed.reason)
                    continue
                endpoints = observed.values
                for endpoint in endpoints:
                    operation.consume("work")
                    target = corpus.nodes.get(term_key(endpoint))
                    yield _Link(focus, target, endpoint, relation, readable)
            self.checked += 1

    def unresolved(self, relation: TopologyRelation) -> None:
        """Retain role-specific coverage gaps without deciding an absence claim."""
        self.reach_unknown = self.reach_unknown or relation.reachability
        self.owner_unknown = self.owner_unknown or relation.ownership

    def reachable_link(self, link: _Link, operation: Budget) -> None:
        """Index one actual selected reachability relation."""
        assert link.target is not None
        operation.consume("states")
        self.reachable_edges.setdefault(link.source.record.identifier, set()).add(
            link.target.record.identifier
        )

    def owner_link(self, link: _Link, operation: Budget) -> dict[str, _Link]:
        """Retain one witness per distinct actual incoming owner."""
        assert link.target is not None
        incoming = self.owners.setdefault(link.target.record.identifier, {})
        source_id = link.source.record.identifier
        if source_id not in incoming:
            operation.consume("states")
            incoming[source_id] = link
        return incoming

    def acyclic_link(self, link: _Link, operation: Budget) -> None:
        """Publish one known constrained arc after its existing-path witness is used."""
        assert link.target is not None
        operation.consume("states")
        self.adjacency.setdefault(link.source.record.identifier, {})[
            link.target.record.identifier
        ] = link

    def path(self, start: str, goal: str, budget: Budget) -> tuple[_Link, ...] | None:
        """Find an already established path for one incremental cycle witness."""
        if start == goal:
            return ()
        queue = deque([start])
        parents: dict[str, _Link] = {}
        seen = {start}
        while queue:
            node = queue.popleft()
            budget.consume("work")
            for target, link in self.adjacency.get(node, {}).items():
                budget.consume("work")
                if target in seen:
                    continue
                budget.consume("states", 2)
                seen.add(target)
                parents[target] = link
                if target == goal:
                    result = []
                    current = goal
                    while current != start:
                        budget.consume("work")
                        budget.consume("states")
                        item = parents[current]
                        result.append(item)
                        current = item.source.record.identifier
                    return tuple(reversed(result))
                queue.append(target)
        return None

    def evidence(
        self,
        focuses: Sequence[_Focus],
        links: Sequence[_Link],
        budget: Budget,
    ) -> str:
        """Reserve all derived evidence before materializing nested JSON records."""
        budget.consume("states", 8 * (1 + len(focuses) + len(links)))
        selected: dict[str, _Focus] = {}
        for focus in focuses:
            selected[focus.record.identifier] = focus
        for link in links:
            selected[link.source.record.identifier] = link.source
            if link.target is not None:
                selected[link.target.record.identifier] = link.target
        size = 512 + self.context_size
        for focus in selected.values():
            size += 256 + 12 * (
                len(focus.record.identifier)
                + len(focus.where)
                + sum(len(part or "") for part in term_key(focus.record.subject))
            )
        for link in links:
            size += 256 + 12 * (
                len(link.source.record.identifier)
                + len(link.target.record.identifier if link.target else "")
                + len(link.relation.name)
                + len(link.path)
                + sum(len(part or "") for part in term_key(link.endpoint))
            )
        if size > budget.limits.max_bytes:
            raise LimitExceeded("Topology evidence exceeds max_bytes")
        budget.consume("work", size)
        budget.consume("bytes", size)
        data = {
            "source": self.context,
            "records": [
                {
                    "identifier": f.record.identifier,
                    "subject": term_key(f.record.subject),
                    "output_location": f.where
                    if f.record.location is not None
                    else None,
                }
                for f in selected.values()
            ],
            "paths": [
                {
                    "source": link.source.record.identifier,
                    "target": link.target.record.identifier if link.target else None,
                    "endpoint": term_key(link.endpoint),
                    "relation": link.relation.name,
                    "path": link.path,
                }
                for link in links
            ],
        }
        text = json.dumps(
            data, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
        if len(text) > budget.limits.max_bytes:
            raise LimitExceeded("Topology evidence exceeds max_bytes")
        return text

    def reached(self, corpus: RecordCorpus, operation: Budget) -> set[str]:
        """Traverse only established selected reachability edges from actual roots."""
        valid_ids = {focus.record.identifier for focus in corpus.nodes.values()}
        reached = corpus.root_set & valid_ids
        operation.consume("states", len(reached))
        queue = deque(reached)
        while queue:
            current = queue.popleft()
            operation.consume("work")
            for target_id in self.reachable_edges.get(current, ()):
                operation.consume("work")
                if target_id not in reached:
                    operation.consume("states")
                    reached.add(target_id)
                    queue.append(target_id)
        return reached
