"""Explicit OBOGraphs source parsing into owned RDF assertions."""

from dataclasses import dataclass, field

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import (
    Budget,
)
from bijux_ontology.json.values import array_value

from .constructs import (
    DEFINITION,
    NODE_KINDS,
    SYNONYM_TYPE,
    SYNONYMS,
    XREF,
    UnsupportedProjection,
    annotation_kind,
    keys,
    named,
    obj,
    text,
)


@dataclass
class ImportProjection:
    """Build only the selected explicit class/annotation meaning, under one budget."""

    budget: Budget
    literal_predicates: tuple[URIRef, ...]
    graph: Graph = field(default_factory=lambda: Graph(bind_namespaces="none"))
    roles: dict[URIRef, str] = field(default_factory=dict)

    def add(self, subject: Node, predicate: Node, value: Node) -> None:
        """Pay for each actual RDF assertion before adding it to the private graph."""
        self.budget.consume("work")
        self.budget.consume("triples")
        self.budget.consume("states")
        self.graph.add((subject, predicate, value))

    def role(self, value: object, expected: str) -> URIRef:
        """An edge endpoint's authored node role determines its interpretation."""
        identifier = named(value)
        if self.roles.get(identifier) != expected:
            raise UnsupportedProjection(
                "Every selected endpoint needs its explicit supported node role"
            )
        return identifier

    def restriction(self, record: dict[str, object]) -> BNode:
        """Encode one named-property existential, never an individual assertion."""
        keys(record, {"propertyId", "fillerId"})
        node = BNode()
        self.add(node, RDF.type, OWL.Restriction)
        self.add(node, OWL.onProperty, self.role(record.get("propertyId"), "OBJECT"))
        self.add(node, OWL.someValuesFrom, self.role(record.get("fillerId"), "CLASS"))
        return node

    def annotation(
        self, subject: Node, predicate: URIRef, value: Node, metadata: dict[str, object]
    ) -> None:
        """Keep one explicit reification boundary, including nested qualifiers."""
        self.add(subject, predicate, value)
        keys(metadata, {"xrefs", "meta", "synonymType"})
        qualifiers: list[tuple[URIRef, Node, dict[str, object]]] = []
        for xref in array_value(metadata.get("xrefs", [])):
            qualifiers.append((XREF, Literal(text(xref), normalize=False), {}))
        if "synonymType" in metadata:
            qualifiers.append((SYNONYM_TYPE, named(metadata["synonymType"]), {}))
        nested = obj(metadata.get("meta", {}))
        keys(nested, {"comments", "basicPropertyValues"})
        for comment in array_value(nested.get("comments", [])):
            qualifiers.append(
                (RDFS.comment, Literal(text(comment), normalize=False), {})
            )
        for item in array_value(nested.get("basicPropertyValues", [])):
            record = obj(item)
            keys(record, {"pred", "val", "xrefs", "meta"})
            prop = named(record.get("pred"))
            if prop not in self.literal_predicates:
                raise UnsupportedProjection(
                    "Generic annotation values require an "
                    "explicitly selected plain-literal predicate"
                )
            qualifiers.append(
                (
                    prop,
                    Literal(text(record.get("val")), normalize=False),
                    {
                        key: item
                        for key, item in record.items()
                        if key not in {"pred", "val"}
                    },
                )
            )
        if qualifiers:
            node = BNode()
            self.add(
                node,
                RDF.type,
                annotation_kind(isinstance(subject, BNode)),
            )
            self.add(node, OWL.annotatedSource, subject)
            self.add(node, OWL.annotatedProperty, predicate)
            self.add(node, OWL.annotatedTarget, value)
            for prop, term, extra in qualifiers:
                self.annotation(node, prop, term, extra)

    def metadata(self, subject: URIRef, record: dict[str, object]) -> None:
        """Interpret node/ontology metadata without borrowing qualifier identity."""
        keys(
            record,
            {
                "definition",
                "synonyms",
                "comments",
                "xrefs",
                "basicPropertyValues",
                "deprecated",
                "version",
            },
        )
        if "definition" in record:
            definition = obj(record["definition"])
            keys(definition, {"val", "xrefs", "meta", "pred"})
            if "pred" in definition and named(definition["pred"]) != DEFINITION:
                raise UnsupportedProjection(
                    "Definition predicate conflicts with the OBO mapping"
                )
            self.annotation(
                subject,
                DEFINITION,
                Literal(text(definition.get("val")), normalize=False),
                {
                    key: value
                    for key, value in definition.items()
                    if key not in {"val", "pred"}
                },
            )
        for item in array_value(record.get("synonyms", [])):
            synonym = obj(item)
            keys(synonym, {"val", "pred", "xrefs", "meta", "synonymType"})
            predicate = SYNONYMS.get(text(synonym.get("pred")))
            if predicate is None:
                raise UnsupportedProjection(
                    "Only the four explicit OBO synonym scopes are supported"
                )
            self.annotation(
                subject,
                predicate,
                Literal(text(synonym.get("val")), normalize=False),
                {
                    key: value
                    for key, value in synonym.items()
                    if key not in {"val", "pred"}
                },
            )
        for item in array_value(record.get("comments", [])):
            self.annotation(
                subject, RDFS.comment, Literal(text(item), normalize=False), {}
            )
        for item in array_value(record.get("xrefs", [])):
            xref = obj(item)
            keys(xref, {"val", "xrefs", "meta", "pred"})
            if "pred" in xref and named(xref["pred"]) != XREF:
                raise UnsupportedProjection(
                    "Xref predicate conflicts with the OBO mapping"
                )
            self.annotation(
                subject,
                XREF,
                Literal(text(xref.get("val")), normalize=False),
                {
                    key: value
                    for key, value in xref.items()
                    if key not in {"val", "pred"}
                },
            )
        for item in array_value(record.get("basicPropertyValues", [])):
            basic = obj(item)
            keys(basic, {"pred", "val", "xrefs", "meta"})
            predicate = named(basic.get("pred"))
            if predicate not in self.literal_predicates:
                raise UnsupportedProjection(
                    "Generic metadata requires an explicit plain-literal predicate"
                )
            self.annotation(
                subject,
                predicate,
                Literal(text(basic.get("val")), normalize=False),
                {
                    key: value
                    for key, value in basic.items()
                    if key not in {"pred", "val"}
                },
            )
        if "deprecated" in record:
            if type(record["deprecated"]) is not bool:
                raise InputError("Deprecated requires an original JSON boolean")
            self.add(
                subject,
                OWL.deprecated,
                Literal(record["deprecated"], datatype=XSD.boolean),
            )
        if "version" in record:
            if self.roles.get(subject) != "ONTOLOGY":
                raise UnsupportedProjection(
                    "Version metadata belongs to the selected ontology"
                )
            self.add(subject, OWL.versionIRI, named(record["version"]))

    def convert(self, record: dict[str, object], graph_id: URIRef) -> Graph:
        """Map only explicit class nodes, existential edges and genus/differentia."""
        keys(record, {"id", "nodes", "edges", "meta", "logicalDefinitionAxioms"})
        self.roles[graph_id] = "ONTOLOGY"
        self.add(graph_id, RDF.type, OWL.Ontology)
        nodes = array_value(record.get("nodes", []))
        seen = set()
        for item in nodes:
            node = obj(item)
            keys(node, {"id", "type", "propertyType", "lbl", "meta"})
            identifier = named(node.get("id"))
            if identifier in seen:
                raise InputError("Selected graph node IDs must be unique")
            if identifier == graph_id:
                raise UnsupportedProjection(
                    "Ontology and entity node identity need distinct selected roles"
                )
            seen.add(identifier)
            role = text(node.get("type"))
            if role == "PROPERTY":
                role = text(node.get("propertyType"))
            elif "propertyType" in node:
                raise UnsupportedProjection(
                    "Property kind belongs only to an authored property node"
                )
            if role not in NODE_KINDS:
                raise UnsupportedProjection(
                    "The selected profile needs class, "
                    "object or annotation property roles"
                )
            self.roles[identifier] = role
            self.add(
                identifier,
                RDF.type,
                NODE_KINDS[role],
            )
        for item in nodes:
            node = obj(item)
            identifier = named(node["id"])
            if "lbl" in node:
                self.add(
                    identifier, RDFS.label, Literal(text(node["lbl"]), normalize=False)
                )
            self.metadata(identifier, obj(node.get("meta", {})))
        self.metadata(graph_id, obj(record.get("meta", {})))
        for item in array_value(record.get("edges", [])):
            edge = obj(item)
            keys(edge, {"sub", "pred", "obj", "meta"})
            subject = self.role(edge.get("sub"), "CLASS")
            target: Node = self.role(edge.get("obj"), "CLASS")
            if edge.get("pred") != "is_a":
                target = self.restriction(
                    {"propertyId": edge.get("pred"), "fillerId": str(target)}
                )
            self.annotation(
                subject,
                RDFS.subClassOf,
                target,
                {"meta": edge["meta"]} if "meta" in edge else {},
            )
        for item in array_value(record.get("logicalDefinitionAxioms", [])):
            definition = obj(item)
            keys(definition, {"definedClassId", "genusIds", "restrictions", "meta"})
            owner = self.role(definition.get("definedClassId"), "CLASS")
            parts: list[Node] = [
                self.role(value, "CLASS")
                for value in array_value(definition.get("genusIds", []))
            ]
            parts.extend(
                self.restriction(obj(value))
                for value in array_value(definition.get("restrictions", []))
            )
            if not parts:
                raise InputError(
                    "A supported logical definition requires actual operands"
                )
            expression = parts[0]
            if len(parts) > 1:
                expression = BNode()
                self.add(expression, RDF.type, OWL.Class)
                head: Node = BNode()
                self.add(expression, OWL.intersectionOf, head)
                for index, part in enumerate(parts):
                    self.add(head, RDF.first, part)
                    rest = BNode() if index + 1 < len(parts) else RDF.nil
                    self.add(head, RDF.rest, rest)
                    head = rest
            self.annotation(
                owner,
                OWL.equivalentClass,
                expression,
                {"meta": definition["meta"]} if "meta" in definition else {},
            )
        return self.graph
