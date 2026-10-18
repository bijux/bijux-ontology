"""Validated RDF terms and lexical-preserving parser conversion."""

import pyoxigraph as ox
from rdflib import BNode, Literal, URIRef
from rdflib.namespace import RDF, XSD
from rdflib.term import Node

from ..errors import InputError
from .identifiers import absolute_iri, language_tag


class _LexicalStringLiteral(Literal):
    """Retain exact string-datatype spelling in an immutable RDFLib term.

    RDFLib rewrites these datatypes even with normalize=False. Its maintained
    constructor supplies metadata; only the owned lexical storage bypasses that
    rewrite. The compatibility boundary is limited to its four literal slots.
    """

    __slots__ = ()

    def __new__(
        cls, value: str, lang: str | None = None, datatype: str | None = None
    ) -> "_LexicalStringLiteral":
        """Own lexical text while retaining maintained literal metadata."""
        if lang is not None or datatype not in (
            str(XSD.token),
            str(XSD.normalizedString),
        ):
            raise InputError(
                "Exact string storage requires a supported string datatype"
            )
        reference = Literal(value, datatype=datatype, normalize=False)
        result = str.__new__(cls, value)
        object.__setattr__(result, "_language", reference.language)
        object.__setattr__(result, "_datatype", reference.datatype)
        object.__setattr__(result, "_value", reference.value)
        object.__setattr__(result, "_ill_typed", reference.ill_typed)
        return result

    def __setattr__(self, name: str, value: object) -> None:
        """Reject metadata changes after owned initialization."""
        raise AttributeError("Exact RDF literal metadata is immutable")

    def __delattr__(self, name: str) -> None:
        """Reject deletion of immutable term metadata."""
        raise AttributeError("Exact RDF literal metadata is immutable")

    def __reduce__(self) -> tuple[type[Literal], tuple[str, str | None, str | None]]:
        """Keep lexical spelling through Python copy and pickle transports."""
        return type(self), (str(self), self.language, str(self.datatype))


def validate_node(node: Node) -> None:
    """Reject unsupported RDF terms and invalid identifier metadata."""
    if isinstance(node, URIRef):
        absolute_iri(str(node))
    elif isinstance(node, Literal):
        if node.datatype is not None:
            absolute_iri(str(node.datatype))
        if node.language is not None:
            language_tag(node.language)
    elif not isinstance(node, BNode):
        raise InputError("Unsupported RDF term shape")


def validate_triple(subject: Node, predicate: Node, value: Node) -> None:
    """Validate subject and predicate roles alongside every supplied RDF term."""
    if not isinstance(subject, URIRef | BNode) or not isinstance(predicate, URIRef):
        raise InputError("RDF subjects must be resources and predicates must be IRIs")
    for node in (subject, predicate, value):
        validate_node(node)


def literal_term(
    value: str,
    *,
    datatype: str | None = None,
    language: str | None = None,
    preserve: bool = True,
) -> Literal:
    """Construct a validated RDF literal without silently changing lexical form."""
    if not isinstance(value, str) or type(preserve) is not bool:
        raise InputError("Literal construction requires text and boolean preservation")
    if datatype is not None:
        absolute_iri(datatype, location="datatype")
    if language is not None:
        language_tag(language)
        if datatype not in (None, str(RDF.langString)):
            raise InputError("A language literal has only rdf:langString datatype")
    literal = Literal(
        value,
        lang=language,
        datatype=None
        if language
        else URIRef(datatype)
        if datatype is not None
        else None,
        normalize=False,
    )
    if preserve and str(literal) != value:
        if datatype in (str(XSD.token), str(XSD.normalizedString)):
            return _LexicalStringLiteral(value, datatype=datatype)
        raise InputError(
            "Literal lexical preservation is unsupported by RDFLib for this term; "
            "explicitly select preserve=False to accept normalisation"
        )
    return literal


def oxigraph_term(
    node: object, nodes: dict[str, BNode], preserve_literals: bool
) -> Node:
    """Convert a parser term using one document-scoped blank-node mapping."""
    if isinstance(node, ox.NamedNode):
        return URIRef(node.value)
    if isinstance(node, ox.BlankNode):
        return nodes.setdefault(node.value, BNode())
    if isinstance(node, ox.Literal):
        if node.direction is not None:
            raise InputError("RDF 1.2 directional literals are not supported")
        return literal_term(
            node.value,
            language=node.language,
            datatype=node.datatype.value,
            preserve=preserve_literals,
        )
    raise InputError("Quoted triples or unrecognised RDF term shapes are unsupported")


def literal_datatype(node: Literal) -> URIRef:
    """Read the intrinsic RDF 1.1 datatype without changing supplied metadata."""
    return (
        node.datatype
        if node.datatype is not None
        else RDF.langString
        if node.language is not None
        else XSD.string
    )


def term_key(node: Node | None) -> tuple[str | None, ...]:
    """Preserve numeric lexical forms and canonicalise only implicit string typing.

    Blank-node keys are meaningful within their owned document scope. Comparing
    independent documents requires an isomorphism operation, not these labels.
    """
    if node is None:
        return ("default",)
    if isinstance(node, BNode):
        return "blank", str(node)
    if isinstance(node, URIRef):
        return "iri", absolute_iri(str(node))
    if isinstance(node, Literal):
        if node.language is not None:
            language_tag(node.language)
        datatype = literal_datatype(node)
        return (
            "literal",
            str(node),
            node.language.lower() if node.language is not None else None,
            absolute_iri(str(datatype)),
        )
    raise InputError("Unsupported RDF term shape")
