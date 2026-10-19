"""Exact RDF IRI and language-tag validation."""

from pyoxigraph import Literal as OxLiteral
from pyoxigraph import NamedNode

from bijux_ontology.diagnostics.errors import InputError


def absolute_iri(value: str, *, location: str = "") -> str:
    """Validate an absolute RDF IRI without changing its spelling or scheme."""
    if not isinstance(value, str):
        raise InputError("Expected an absolute IRI string", location=location)
    try:
        NamedNode(value)
    except ValueError as error:
        raise InputError(
            f"Invalid absolute IRI: {value!r}", location=location
        ) from error
    return value


def language_tag(value: str, *, location: str = "") -> str:
    """Validate an RDF language tag using the maintained RDF term implementation."""
    if not isinstance(value, str):
        raise InputError("Expected a language tag string", location=location)
    try:
        OxLiteral("", language=value)
    except ValueError as error:
        raise InputError(
            f"Invalid language tag: {value!r}", location=location
        ) from error
    return value
