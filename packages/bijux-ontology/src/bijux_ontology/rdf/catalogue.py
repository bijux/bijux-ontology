"""Finite RDF/RDFS declaration roles and reserved RDF/XSD names."""

from rdflib.namespace import RDF, RDFS, XSD

_RDF_CLASSES = (
    RDF.Property,
    RDF.List,
    RDF.Statement,
    RDF.Alt,
    RDF.Bag,
    RDF.Seq,
    RDFS.Resource,
    RDFS.Class,
    RDFS.Literal,
    RDFS.Container,
    RDFS.ContainerMembershipProperty,
    RDFS.Datatype,
)
_RDF_PROPERTIES = (
    RDF.type,
    RDF.subject,
    RDF.predicate,
    RDF.object,
    RDF.first,
    RDF.rest,
    RDF.value,
    RDFS.subClassOf,
    RDFS.subPropertyOf,
    RDFS.domain,
    RDFS.range,
)
_CATALOG: dict[str, frozenset[str]] = {
    **{str(term): frozenset((str(RDFS.Class),)) for term in _RDF_CLASSES},
    **{
        str(term): frozenset((str(RDF.Property),))
        for term in (
            *_RDF_PROPERTIES,
            RDFS.label,
            RDFS.comment,
            RDFS.seeAlso,
            RDFS.isDefinedBy,
        )
    },
    **{
        str(term): frozenset((str(RDFS.Datatype),))
        for term in (RDF.langString, RDF.XMLLiteral, RDF.HTML, RDF.JSON)
    },
    str(RDF.nil): frozenset((str(RDF.List),)),
}


def builtin_kinds(iri: str) -> frozenset[str]:
    """Return known RDF/RDFS/XSD roles, including canonical container properties."""
    if iri.startswith(str(XSD)) and iri in XSD:
        return frozenset((str(RDFS.Datatype),))
    suffix = iri.removeprefix(str(RDF))
    if (
        iri.startswith(str(RDF))
        and suffix.startswith("_")
        and len(suffix) > 1
        and suffix[1] in "123456789"
        and all("0" <= char <= "9" for char in suffix[1:])
    ):
        return frozenset((str(RDF.Property),))
    return _CATALOG.get(iri, frozenset())


def reserved_name_known(iri: str) -> bool | None:
    """Recognise RDF/RDFS/XSD spelling without assigning declaration roles."""
    if iri.startswith(str(RDF) + "_"):
        return bool(builtin_kinds(iri))
    for namespace in (RDF, RDFS, XSD):
        if iri.startswith(str(namespace)):
            return iri in namespace
    return None
