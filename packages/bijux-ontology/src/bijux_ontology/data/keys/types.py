"""Explicit record bindings and faithful optional key backend artifacts."""

from dataclasses import dataclass, field

from rdflib import BNode, URIRef

from bijux_ontology.data.query_rules.types import QueryRule
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource


@dataclass(frozen=True)
class KeyRecord:
    """Bind one selected subject to an owned snapshot and explicit graph scope.

    URI subjects identify named records. Blank subjects must be exact term
    objects from this snapshot; equal labels from another source are not proof.
    """

    identifier: str
    source: PreparedSource
    subject: URIRef | BNode
    graph_name: GraphSelection | None = field(default=None, kw_only=True)
    union: bool = field(default=False, kw_only=True)

    def __post_init__(self) -> None:
        """Validate binding shape without exporting/copying an owned graph."""
        if not isinstance(self.identifier, str) or not self.identifier:
            raise InputError("Key records require nonempty identifiers")
        object.__setattr__(self, "identifier", str(self.identifier))
        if not isinstance(self.source, PreparedSource):
            raise InputError("Key records require owned PreparedSource snapshots")
        if not isinstance(self.subject, URIRef | BNode):
            raise InputError("Key records require exact RDF resource subjects")
        if isinstance(self.subject, URIRef):
            absolute_iri(str(self.subject))
        if type(self.union) is not bool:
            raise InputError("Key record union must be boolean")
        if self.graph_name is not None and not isinstance(
            self.graph_name, str | URIRef | BNode
        ):
            raise InputError("Key record graph selection must be explicit RDF scope")
        if self.graph_name is not None and self.union:
            raise InputError("Select one named graph or the explicit union")


@dataclass(frozen=True)
class KeyProjection:
    """Owned executable artifacts beside their projection qualification report."""

    report: Report
    rules: tuple[QueryRule, ...] = ()
    shapes: PreparedSource | None = None
