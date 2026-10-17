"""Compose RDF and OWL declaration roles under the selected structural regime."""

from typing import Literal

from rdflib.namespace import RDF, RDFS

from bijux_ontology.owl.builtins import builtin_kinds as owl_builtin_kinds
from bijux_ontology.owl.builtins import declaration_roles
from bijux_ontology.owl.builtins import reserved_name_known as owl_name_known
from bijux_ontology.rdf.catalogue import builtin_kinds as rdf_builtin_kinds
from bijux_ontology.rdf.catalogue import reserved_name_known as rdf_name_known

from ..errors import InputError

Regime = Literal["rdf", "owl-dl"]


def validate_regime(regime: str) -> None:
    """Reject an unimplemented semantic family before checking any term."""
    if regime not in ("rdf", "owl-dl"):
        raise InputError("Supported structural regimes are 'rdf' and 'owl-dl'")


def builtin_kinds(iri: str, regime: Regime = "rdf") -> frozenset[str]:
    """Return selected builtin roles, without treating known names as proofs."""
    validate_regime(regime)
    if iri == str(RDFS.Literal) and regime == "owl-dl":
        return frozenset((str(RDFS.Datatype),))
    return rdf_builtin_kinds(iri) | owl_builtin_kinds(iri)


def kind_matches(
    actual: frozenset[str], expected: tuple[str, ...], regime: Regime = "rdf"
) -> bool:
    """Compose the selected RDF/OWL role implications before comparing roles."""
    validate_regime(regime)
    inferred = declaration_roles(actual, rdf_classes=regime == "rdf")
    if str(RDFS.ContainerMembershipProperty) in inferred:
        inferred |= {str(RDF.Property)}
    return not expected or bool(inferred.intersection(expected))


def reserved_name_known(iri: str) -> bool | None:
    """Recognise RDF/RDFS/OWL/XSD names without asserting their semantics."""
    known = rdf_name_known(iri)
    return known if known is not None else owl_name_known(iri)
