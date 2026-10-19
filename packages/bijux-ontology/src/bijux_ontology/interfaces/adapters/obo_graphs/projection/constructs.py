"""Finite OBOGraphs role, annotation and explicit source-value qualification."""

from types import MappingProxyType

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import OWL, RDFS
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.inputs import capture
from bijux_ontology.execution.limits import (
    Budget,
    bound_text,
)
from bijux_ontology.json.values import object_value, text_value
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.vocabulary.annotations.graphs.types import AnnotationStatement

DEFINITION = URIRef("http://purl.obolibrary.org/obo/IAO_0000115")


XREF = URIRef("http://www.geneontology.org/formats/oboInOwl#hasDbXref")


SYNONYM_TYPE = URIRef("http://www.geneontology.org/formats/oboInOwl#hasSynonymType")


SYNONYMS = {
    "hasExactSynonym": URIRef(
        "http://www.geneontology.org/formats/oboInOwl#hasExactSynonym"
    ),
    "hasBroadSynonym": URIRef(
        "http://www.geneontology.org/formats/oboInOwl#hasBroadSynonym"
    ),
    "hasNarrowSynonym": URIRef(
        "http://www.geneontology.org/formats/oboInOwl#hasNarrowSynonym"
    ),
    "hasRelatedSynonym": URIRef(
        "http://www.geneontology.org/formats/oboInOwl#hasRelatedSynonym"
    ),
}


_BUILTINS = (RDFS.label, RDFS.comment, XREF)


class UnsupportedProjection(Exception):
    """An actual supplied feature is outside the selected meaning profile."""


def obj(value: object) -> dict[str, object]:
    """Require original JSON objects instead of silently coercing native models."""
    return object_value(value, location="")


def text(value: object) -> str:
    """Require original nonempty strings for identifiers and lexical values."""
    return text_value(value, location="")


def named(value: object) -> URIRef:
    """Interpret only explicit absolute IRIs; no CURIE or context guessing."""
    return URIRef(absolute_iri(text(value)))


def keys(record: dict[str, object], allowed: set[str]) -> None:
    """Do not let schema-permitted extra fields disappear during interpretation."""
    if set(record) - allowed:
        raise UnsupportedProjection(
            "Source contains fields outside the declared OBOGraphs subset"
        )


def statement(subject: Node, predicate: Node, value: Node) -> AnnotationStatement:
    """Narrow actual RDF roles before constructing an existing typed statement."""
    if (
        not isinstance(subject, URIRef | BNode)
        or not isinstance(predicate, URIRef)
        or not isinstance(value, URIRef | BNode | Literal)
    ):
        raise InputError("Selected assertions require supported RDF term roles")
    return AnnotationStatement(subject, predicate, value)


def literals(values: tuple[URIRef, ...], budget: Budget) -> tuple[URIRef, ...]:
    """Qualify explicit plain-literal annotation predicates before key creation."""
    selected = []
    for value in capture(values, budget):
        if type(value) is not URIRef:
            raise InputError("Literal metadata predicates require named RDF IRIs")
        bound_text(value, budget)
        absolute_iri(str(value))
        selected.append(value)
    return tuple(dict.fromkeys((*_BUILTINS, *selected)))


NODE_KINDS = MappingProxyType(
    {
        "CLASS": OWL.Class,
        "OBJECT": OWL.ObjectProperty,
        "ANNOTATION": OWL.AnnotationProperty,
    }
)


def annotation_kind(nested: bool) -> URIRef:
    """Each nested qualifier owns an annotation, while its root owns an axiom."""
    return OWL.Annotation if nested else OWL.Axiom
