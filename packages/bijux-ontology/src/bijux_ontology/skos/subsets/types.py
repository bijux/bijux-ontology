"""Named controlled concept sets retain membership and lifecycle proof scope."""

from dataclasses import dataclass, replace

from rdflib import URIRef

from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.declarations.types import FieldDeclaration
from bijux_ontology.declarations.value_constraints import ValueConstraints
from bijux_ontology.errors import InputError
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.collections.types import CollectionOccurrence
from bijux_ontology.vocabulary.navigation import Membership
from bijux_ontology.vocabulary.results import Deprecation

from ..publication.types import ConceptReachability


@dataclass(frozen=True)
class ConceptSubset:
    """Known eligible names and exclusions, independent of missing membership.

    Witnesses retain scheme assertions, hierarchy paths or nested collection
    occurrences, including proved prefixes of an incomplete selection. Lifecycle
    records preserve why an otherwise known member was allowed or unresolved.
    """

    values: tuple[URIRef, ...]
    excluded: tuple[URIRef, ...]
    closed: bool
    report: Report
    witnesses: tuple[Membership | ConceptReachability | CollectionOccurrence, ...]
    lifecycle: tuple[tuple[URIRef, Deprecation], ...] = ()

    def bind(self, field: FieldDeclaration) -> FieldDeclaration:
        """Bind normal resource alternatives without replacing existing predicates.

        An existing enumeration must be resolved explicitly by the caller; this
        operation never overwrites it or invents an open-set intersection.
        """
        if not isinstance(field, FieldDeclaration):
            raise InputError("Concept binding requires a FieldDeclaration")
        enumeration = ValueEnumeration(
            self.values, excluded=self.excluded, closed=self.closed, report=self.report
        )
        values = []
        resource = False
        for value in field.values:
            if value.kind not in ("iri", "object"):
                values.append(value)
                continue
            resource = True
            constraints = value.constraints or ValueConstraints()
            if constraints.enumeration is not None:
                raise InputError(
                    "Concept binding cannot replace an existing enumeration"
                )
            values.append(
                replace(
                    value, constraints=replace(constraints, enumeration=enumeration)
                )
            )
        if not resource:
            raise InputError("Concept binding requires a resource value alternative")
        return replace(field, values=tuple(values))
