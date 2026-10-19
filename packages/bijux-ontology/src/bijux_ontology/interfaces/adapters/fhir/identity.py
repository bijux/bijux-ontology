"""Shared exact code identities; no inferred namespaces or business versions."""

from dataclasses import dataclass

from rdflib import URIRef

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri


@dataclass(frozen=True)
class FhirCodeIdentity:
    """Original system, optional business version and code spelling."""

    system: str
    version: str | None
    code: str


@dataclass(frozen=True)
class FhirCodeBinding:
    """One caller-supplied reversible association with an exact RDF IRI."""

    identity: FhirCodeIdentity
    iri: URIRef


def capture_code_bindings(
    bindings: tuple[FhirCodeBinding, ...], budget: Budget
) -> tuple[FhirCodeBinding, ...]:
    """Qualify a finite bijection without normalizing any identity component.

    This validates identity storage, not a terminology resource's FHIR rules.
    Missing versions remain missing and never select a latest supplied version.
    """
    if type(bindings) is not tuple:
        raise InputError("FHIR code bindings require an immutable tuple")
    budget.consume("states", len(bindings))
    identities: set[FhirCodeIdentity] = set()
    iris: set[URIRef] = set()
    for binding in bindings:
        budget.consume("work")
        if type(binding) is not FhirCodeBinding:
            raise InputError("Expected an explicit FhirCodeBinding")
        identity = binding.identity
        if type(identity) is not FhirCodeIdentity:
            raise InputError("Expected an exact FhirCodeIdentity")
        for value in (identity.system, identity.code):
            if type(value) is not str or not value:
                raise InputError("FHIR system and code require nonempty text")
            bound_text(value, budget)
        if identity.version is not None:
            if type(identity.version) is not str or not identity.version:
                raise InputError("FHIR business version requires nonempty text")
            bound_text(identity.version, budget)
        absolute_iri(identity.system)
        if type(binding.iri) is not URIRef:
            raise InputError("FHIR bindings require named RDF IRIs")
        bound_text(binding.iri, budget)
        absolute_iri(str(binding.iri))
        if identity in identities or binding.iri in iris:
            raise InputError("FHIR code bindings must be bijective")
        budget.consume("states", 2)
        identities.add(identity)
        iris.add(binding.iri)
    return bindings
