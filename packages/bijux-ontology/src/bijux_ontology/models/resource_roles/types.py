"""Application interpretation requirements over original RDF resource terms."""

from dataclasses import dataclass

from rdflib import BNode, URIRef

from bijux_ontology.declarations.resource_roles import ResourceRole
from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.terms import bound_node, bound_text


@dataclass(frozen=True)
class RoleUse:
    """One caller-selected resource and its interpretation, with a source anchor."""

    term: URIRef | BNode
    contract: ResourceRole
    location: str = ""

    def __post_init__(self) -> None:
        """Validate finite original terms without serializing them into prose."""
        if not isinstance(self.term, URIRef | BNode):
            raise InputError("Resource role uses require an IRI or blank resource")
        if not isinstance(self.contract, ResourceRole):
            raise InputError("Expected an explicit ResourceRole")
        budget = Budget()
        bound_text(self.location, budget)
        bound_node(self.term, budget)
