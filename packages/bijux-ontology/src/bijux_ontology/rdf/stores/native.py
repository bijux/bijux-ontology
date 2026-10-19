"""Exact term qualification at the maintained native database boundary."""

import pyoxigraph as ox
from rdflib import BNode, Literal, URIRef
from rdflib.namespace import RDF, XSD

from bijux_ontology.errors import InputError
from bijux_ontology.rdf.queries.literals import LiteralScopeUnsupported
from bijux_ontology.rdf.queries.protocol import QueryTerm
from bijux_ontology.rdf.terms import literal_term


def rdf_term(value: object) -> QueryTerm | None:
    """Keep supported ordinary RDF lexical terms; reject native extensions."""
    if isinstance(value, ox.DefaultGraph):
        return None
    if isinstance(value, ox.NamedNode):
        return URIRef(value.value)
    if isinstance(value, ox.BlankNode):
        return BNode(value.value)
    if isinstance(value, ox.Literal):
        if value.direction is not None:
            raise LiteralScopeUnsupported(
                "Directional literals need an explicit RDF profile"
            )
        return literal_term(
            value.value,
            datatype=None if value.language else value.datatype.value,
            language=value.language,
        )
    raise LiteralScopeUnsupported("The disk store supports ordinary RDF terms only")


def native_term(value: QueryTerm | None) -> object:
    """Encode an already qualified owned RDF selector without label rebinding."""
    if value is None:
        return ox.DefaultGraph()
    if isinstance(value, URIRef):
        return ox.NamedNode(str(value))
    if isinstance(value, BNode):
        return ox.BlankNode(str(value))
    if isinstance(value, Literal):
        return (
            ox.Literal(str(value), language=value.language)
            if value.language
            else ox.Literal(
                str(value), datatype=ox.NamedNode(str(value.datatype or XSD.string))
            )
        )
    raise InputError("Unsupported native RDF selector")


def qualify_literal(value: ox.Literal, probe: ox.Store) -> None:
    """Use the actual native store to detect lexical normalization before adoption.

    The database canonicalizes some datatype lexical forms. Such inputs are
    unsupported, never silently converted to different RDF terms. Plain and
    language strings have intrinsic RDF identity; other terms get a real
    maintained round trip through a constant-sized scratch store.
    """
    rdf_term(value)
    if value.datatype.value in (str(XSD.string), str(RDF.langString)):
        return
    probe.clear()
    probe.add(
        ox.Quad(
            ox.NamedNode("urn:bijux:store:subject"),
            ox.NamedNode("urn:bijux:store:predicate"),
            value,
        )
    )
    returned = next(iter(probe)).object
    if returned != value:
        raise LiteralScopeUnsupported(
            "The native store changes this literal's RDF lexical identity"
        )
