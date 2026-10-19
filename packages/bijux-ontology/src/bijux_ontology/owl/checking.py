"""Selected OWL declarations, ontology headers and annotated-axiom encodings."""

from collections.abc import Sequence
from typing import Literal

from rdflib import BNode, Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS
from rdflib.term import Node

from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.limits import Budget, IssueCollector

_OBJECT_ONLY = frozenset(
    (
        OWL.InverseFunctionalProperty,
        OWL.TransitiveProperty,
        OWL.SymmetricProperty,
        OWL.AsymmetricProperty,
        OWL.ReflexiveProperty,
        OWL.IrreflexiveProperty,
    )
)
_CONFLICTS = (
    (OWL.ObjectProperty, OWL.DatatypeProperty),
    (OWL.ObjectProperty, OWL.AnnotationProperty),
    (OWL.DatatypeProperty, OWL.AnnotationProperty),
    (OWL.Class, RDFS.Datatype),
    (RDFS.Class, RDFS.Datatype),
)


def _location(node: Node, source: str) -> str:
    return (
        f"{source}: {node}" if isinstance(node, URIRef) else f"{source}: anonymous node"
    )


def check_declarations(
    scopes: Sequence[tuple[str, Graph]],
    regime: Literal["rdf", "owl-dl"],
    budget: Budget,
    issues: IssueCollector,
) -> int:
    """Check selected OWL DL category conflicts while retaining declaration counts."""
    declarations: dict[Node, dict[Node, set[str]]] = {}
    for source, graph in scopes:
        for node, kind in sorted(
            graph.subject_objects(RDF.type),
            key=lambda pair: (pair[0].n3(), pair[1].n3()),
        ):
            budget.consume("work")
            declarations.setdefault(node, {}).setdefault(kind, set()).add(source)
    if regime != "owl-dl":
        return len(declarations)
    for node, kinds in sorted(declarations.items(), key=lambda item: item[0].n3()):
        for first, second in _CONFLICTS:
            if first in kinds and second in kinds:
                sources = ", ".join(sorted(kinds[first] | kinds[second]))
                issues.append(
                    Issue(
                        "ontology.declaration_conflict",
                        "Selected OWL DL declaration categories conflict",
                        _location(node, sources),
                        actual=f"{first}, {second}",
                    ),
                )
        if OWL.DatatypeProperty in kinds:
            for characteristic in sorted(_OBJECT_ONLY & kinds.keys(), key=str):
                sources = ", ".join(
                    sorted(kinds[OWL.DatatypeProperty] | kinds[characteristic])
                )
                issues.append(
                    Issue(
                        "ontology.characteristic_conflict",
                        "Datatype property has an object-only characteristic",
                        _location(node, sources),
                        actual=str(characteristic),
                    ),
                )
    return len(declarations)


def check_headers(
    graph: Graph, source: str, budget: Budget, issues: IssueCollector
) -> int:
    """Check the named header and version IRI encoding of supplied ontologies."""
    headers = set(graph.subjects(RDF.type, OWL.Ontology)) | set(
        graph.subjects(OWL.versionIRI)
    )
    for node in sorted(headers, key=lambda value: value.n3()):
        budget.consume("work")
        versions = tuple(
            sorted(graph.objects(node, OWL.versionIRI), key=lambda value: value.n3())
        )
        if len(versions) > 1:
            issues.append(
                Issue(
                    "ontology.version_ambiguous",
                    "Ontology header has multiple version IRIs",
                    _location(node, source),
                ),
            )
        if versions and (
            (node, RDF.type, OWL.Ontology) not in graph or not isinstance(node, URIRef)
        ):
            issues.append(
                Issue(
                    "ontology.version_header",
                    "A version IRI needs a named ontology header",
                    _location(node, source),
                ),
            )
        for version in versions:
            if not isinstance(version, URIRef):
                issues.append(
                    Issue(
                        "ontology.version_shape",
                        "Ontology version must be an IRI",
                        _location(node, source),
                        actual=str(version),
                    ),
                )
    return len(headers)


def check_axiom_annotations(
    graph: Graph, source: str, budget: Budget, issues: IssueCollector
) -> int:
    """Check annotated statements against their supplied source graph."""
    nodes = set(graph.subjects(RDF.type, OWL.Axiom)) | set(
        graph.subjects(RDF.type, OWL.Annotation)
    )
    for node in sorted(nodes, key=lambda value: value.n3()):
        budget.consume("work")
        components = [
            tuple(graph.objects(node, predicate))
            for predicate in (
                OWL.annotatedSource,
                OWL.annotatedProperty,
                OWL.annotatedTarget,
            )
        ]
        if any(len(values) != 1 for values in components):
            issues.append(
                Issue(
                    "ontology.annotation_shape",
                    "Annotated axiom requires exactly one source, property and target",
                    _location(node, source),
                ),
            )
            continue
        subject, predicate, target = (values[0] for values in components)
        if not isinstance(subject, URIRef | BNode) or not isinstance(predicate, URIRef):
            issues.append(
                Issue(
                    "ontology.annotation_shape",
                    "Annotated statement has invalid resource/property roles",
                    _location(node, source),
                ),
            )
        elif (subject, predicate, target) not in graph:
            issues.append(
                Issue(
                    "ontology.annotation_statement_missing",
                    "Annotated statement is absent from its supplied source",
                    _location(node, source),
                ),
            )
    return len(nodes)
