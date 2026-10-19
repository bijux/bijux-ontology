"""Owned checked projection outputs and established framed-document paths."""

import json
from dataclasses import dataclass

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.json.paths import SourcePath
from bijux_ontology.rdf.snapshots import PreparedSource


@dataclass(frozen=True)
class FrameNode:
    """One named retained entity and its actual describing output object paths."""

    identifier: str
    graph: str | None
    paths: tuple[SourcePath, ...]


@dataclass(frozen=True)
class FrameOmission:
    """Explicit RDF scope allowance, without invented source-property pointers.

    None predicates permits excluding the whole named record description. A
    finite tuple excludes those predicates from preservation truth on the named
    seed only; attached anonymous descendants have no blanket exemption.
    """

    identifier: str
    graph: str | None
    predicates: tuple[str, ...] | None = None


@dataclass(frozen=True)
class FramedDocument:
    """A qualified selected projection, never whole-dataset equivalence.

    Accepted output and RDF are withheld if required retention or output binding
    is incomplete. Paths address this output only, not the original compact JSON.
    base is the explicit application base, or the effective URL for URL input;
    subsequent JSON-LD checks must use it when resolving relative output IDs.
    """

    document_json: str | None
    base: str | None
    source: PreparedSource | None
    nodes: tuple[FrameNode, ...]
    omissions: tuple[FrameOmission, ...]
    report: Report

    @property
    def document(self) -> object:
        """Return an independent export of the checked framed document."""
        return (
            json.loads(self.document_json) if self.document_json is not None else None
        )

    def bind(self, identifier: str, *, graph: str | None = None) -> FrameNode:
        """Bind a retained entity, keeping multiple actual embeddings explicit."""
        found = [
            node
            for node in self.nodes
            if node.identifier == identifier and node.graph == graph
        ]
        if len(found) != 1:
            raise InputError("Expected one retained framed entity in this graph")
        return found[0]
