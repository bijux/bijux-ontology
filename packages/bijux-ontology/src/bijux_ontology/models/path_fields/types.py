"""Explicit subject bindings for logical RDF path fields."""

from dataclasses import dataclass
from dataclasses import field as record_field

from rdflib import BNode, URIRef

from bijux_ontology.declarations.path_fields import PathFieldDeclaration
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Location
from bijux_ontology.rdf.identifiers import absolute_iri


@dataclass(frozen=True)
class PathFieldUsage:
    """Bind one logical field to a named or exact owned blank subject.

    A location is caller-established source evidence, never an inferred JSON
    path. Blank labels from another source do not establish correspondence.
    """

    field: PathFieldDeclaration
    subject: URIRef | BNode
    location: Location | None = record_field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        """Require a typed field and an explicit semantic subject binding."""
        if not isinstance(self.field, PathFieldDeclaration):
            raise InputError("Expected a typed PathFieldDeclaration")
        if not isinstance(self.subject, URIRef | BNode):
            raise InputError("Path field subjects require IRI or owned blank terms")
        if isinstance(self.subject, URIRef):
            absolute_iri(str(self.subject))
        if self.location is not None and not isinstance(self.location, Location):
            raise InputError("Expected an established Location")
