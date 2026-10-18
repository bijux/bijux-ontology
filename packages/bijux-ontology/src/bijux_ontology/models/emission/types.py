"""Typed source and graph-qualified model usage selections."""

from __future__ import annotations

from dataclasses import KW_ONLY, dataclass
from typing import cast

from rdflib import BNode, URIRef

from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.declarations.types import SubjectIdentity
from bijux_ontology.errors import InputError
from bijux_ontology.jsonld.observations import SourcePath
from bijux_ontology.rdf.identifiers import absolute_iri


@dataclass(frozen=True)
class ModelUsage:
    """Bind a source object or an explicitly graph-qualified named reference.

    ``subject`` constrains the source object's effective identity. ``reference``
    selects an entity directly; its path is only an application diagnostic anchor.
    ``graph=ABSENT`` follows a source binding, None requires the default graph.
    Named references require an explicit graph. ``entity`` joins descriptions in
    that graph; ordinary source bindings retain each object's field guarantees.
    ``closed`` makes an absent referenced description a finding instead of unknown.
    """

    model: str
    path: SourcePath = ()
    _: KW_ONLY
    subject: SubjectIdentity | None = None
    reference: str | None = None
    graph: str | Absent | None = ABSENT
    entity: bool = False
    closed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise InputError("Model usage requires a model identifier")
        path = tuple(self.path)
        if isinstance(cast(object, self.path), (str, bytes)) or any(
            type(part) not in (str, int) or (isinstance(part, int) and part < 0)
            for part in path
        ):
            raise InputError("Model usage requires a key/index path")
        object.__setattr__(self, "path", path)
        if self.subject is not None and (not isinstance(self.subject, SubjectIdentity)):
            raise InputError("Expected a typed subject identity contract")
        if self.graph is not None and self.graph is not ABSENT:
            absolute_iri(self.graph, location="graph")
        if type(self.entity) is not bool or type(self.closed) is not bool:
            raise InputError("entity and closed must be booleans")
        if self.reference is not None:
            absolute_iri(self.reference, location="reference")
            if self.graph is ABSENT:
                raise InputError("Named reference bindings require an explicit graph")
            if self.subject is not None and self.subject.expected_iri not in (
                None,
                self.reference,
            ):
                raise InputError("Reference and expected subject identities conflict")


@dataclass(frozen=True)
class RdfModelUsage:
    """Bind a model to an IRI or an exact owned snapshot blank subject.

    IRIs identify resources by name. Blank subjects must be immutable term objects
    obtained from this PreparedSource's graph/dataset exports; matching labels
    from another source never establish correspondence. location is a caller
    diagnostic anchor and has no effect on RDF identity or source selection.
    """

    model: str
    subject: URIRef | BNode
    _: KW_ONLY
    location: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise InputError("RDF model usage requires a model identifier")
        if not isinstance(self.subject, URIRef | BNode):
            raise InputError("RDF model subjects must be URIRef or owned BNode terms")
        if isinstance(self.subject, URIRef):
            absolute_iri(str(self.subject))
        if not isinstance(self.location, str):
            raise InputError("RDF model usage location must be a string")
