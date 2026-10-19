"""Selected OWL 2 builtin roles and finite declaration-role implications."""

from rdflib.namespace import OWL, RDF, RDFS

_OWL_CLASSES = (
    OWL.Class,
    OWL.Restriction,
    OWL.Ontology,
    OWL.DataRange,
    OWL.AllDifferent,
    OWL.AllDisjointClasses,
    OWL.AllDisjointProperties,
    OWL.Axiom,
    OWL.Annotation,
    OWL.NamedIndividual,
    OWL.ObjectProperty,
    OWL.DatatypeProperty,
    OWL.AnnotationProperty,
    OWL.FunctionalProperty,
    OWL.InverseFunctionalProperty,
    OWL.TransitiveProperty,
    OWL.SymmetricProperty,
    OWL.AsymmetricProperty,
    OWL.ReflexiveProperty,
    OWL.IrreflexiveProperty,
)
_ANNOTATION_PROPERTIES = (
    RDFS.label,
    RDFS.comment,
    RDFS.seeAlso,
    RDFS.isDefinedBy,
    OWL.versionInfo,
    OWL.priorVersion,
    OWL.backwardCompatibleWith,
    OWL.incompatibleWith,
    OWL.deprecated,
)
_OWL_SYNTAX_PROPERTIES = (
    OWL.imports,
    OWL.versionIRI,
    OWL.equivalentClass,
    OWL.equivalentProperty,
    OWL.disjointWith,
    OWL.propertyDisjointWith,
    OWL.sameAs,
    OWL.differentFrom,
    OWL.inverseOf,
    OWL.unionOf,
    OWL.intersectionOf,
    OWL.complementOf,
    OWL.disjointUnionOf,
    OWL.oneOf,
    OWL.members,
    OWL.distinctMembers,
    OWL.datatypeComplementOf,
    OWL.onDatatype,
    OWL.withRestrictions,
    OWL.onProperty,
    OWL.someValuesFrom,
    OWL.allValuesFrom,
    OWL.hasValue,
    OWL.hasSelf,
    OWL.cardinality,
    OWL.minCardinality,
    OWL.maxCardinality,
    OWL.qualifiedCardinality,
    OWL.minQualifiedCardinality,
    OWL.maxQualifiedCardinality,
    OWL.onClass,
    OWL.onDataRange,
    OWL.propertyChainAxiom,
    OWL.hasKey,
    OWL.annotatedSource,
    OWL.annotatedProperty,
    OWL.annotatedTarget,
    OWL.sourceIndividual,
    OWL.assertionProperty,
    OWL.targetIndividual,
    OWL.targetValue,
)
_OBJECT_CHARACTERISTICS = (
    OWL.InverseFunctionalProperty,
    OWL.TransitiveProperty,
    OWL.SymmetricProperty,
    OWL.AsymmetricProperty,
    OWL.ReflexiveProperty,
    OWL.IrreflexiveProperty,
)
_CATALOG: dict[str, frozenset[str]] = {
    **{str(term): frozenset((str(RDFS.Class),)) for term in _OWL_CLASSES},
    **{str(term): frozenset((str(RDF.Property),)) for term in _OWL_SYNTAX_PROPERTIES},
    **{
        str(term): frozenset((str(OWL.AnnotationProperty), str(RDF.Property)))
        for term in _ANNOTATION_PROPERTIES
    },
    str(OWL.Thing): frozenset((str(OWL.Class),)),
    str(OWL.Nothing): frozenset((str(OWL.Class),)),
    str(OWL.topObjectProperty): frozenset((str(OWL.ObjectProperty),)),
    str(OWL.bottomObjectProperty): frozenset((str(OWL.ObjectProperty),)),
    str(OWL.topDataProperty): frozenset((str(OWL.DatatypeProperty),)),
    str(OWL.bottomDataProperty): frozenset((str(OWL.DatatypeProperty),)),
    str(OWL.real): frozenset((str(RDFS.Datatype),)),
    str(OWL.rational): frozenset((str(RDFS.Datatype),)),
}


def builtin_kinds(iri: str) -> frozenset[str]:
    """Return selected OWL builtin roles without implying logical consistency."""
    return _CATALOG.get(iri, frozenset())


def declaration_roles(actual: frozenset[str], *, rdf_classes: bool) -> frozenset[str]:
    """Apply only the named declaration implications used by structural checks."""
    inferred = set(actual)
    if (
        inferred.intersection(str(item) for item in (OWL.Class, RDFS.Datatype))
        and rdf_classes
    ):
        inferred.add(str(RDFS.Class))
    if str(OWL.Class) in inferred:
        inferred.add(str(RDFS.Class))
    if str(RDFS.Class) in inferred:
        inferred.add(str(OWL.Class))
    if inferred.intersection(
        str(item) for item in (*_OBJECT_CHARACTERISTICS, OWL.ObjectProperty)
    ):
        inferred.add(str(OWL.ObjectProperty))
    if inferred.intersection(
        str(item)
        for item in (
            OWL.ObjectProperty,
            OWL.DatatypeProperty,
            OWL.AnnotationProperty,
            OWL.FunctionalProperty,
        )
    ):
        inferred.add(str(RDF.Property))
    return frozenset(inferred)


def reserved_name_known(iri: str) -> bool | None:
    """Recognise reserved OWL spelling independently of the supported role subset."""
    return iri in OWL if iri.startswith(str(OWL)) else None
