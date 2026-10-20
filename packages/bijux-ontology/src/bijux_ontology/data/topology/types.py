"""Explicit record focus bindings and caller-established output locations."""

from dataclasses import dataclass, field

from rdflib import BNode, URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Location
from bijux_ontology.rdf.identifiers import absolute_iri


@dataclass(frozen=True)
class TopologyRecord:
    """Bind a selected RDF resource to a stable record identifier.

    Blank subjects require exact ownership by the operation's PreparedSource.
    An optional Location is caller-provided diagnostic evidence, not a claim
    that RDF identity establishes a particular JSON serialization occurrence.
    """

    identifier: str
    subject: URIRef | BNode
    location: Location | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        """Validate binding shape without guessing source or output paths."""
        if not isinstance(self.identifier, str) or not self.identifier:
            raise InputError("Topology records require nonempty identifiers")
        object.__setattr__(self, "identifier", str(self.identifier))
        if not isinstance(self.subject, URIRef | BNode):
            raise InputError("Topology subjects require RDF resource terms")
        if isinstance(self.subject, URIRef):
            absolute_iri(str(self.subject))
        if self.location is not None and not isinstance(self.location, Location):
            raise InputError("Topology output locations require a typed Location")
