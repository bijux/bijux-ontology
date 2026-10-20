"""Finite class axioms and qualified annotations under the OBOGraphs mapping."""

from dataclasses import dataclass, field

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD
from rdflib.term import Node

from bijux_ontology._json import array_value, object_value, text_value
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.execution.inputs import capture
from bijux_ontology.limits import Budget, IssueCollector, bound_text, retain_report
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.structure import iter_rdf_list
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.annotations.graphs.reading import (
    inspect_annotation_graph,
)
from bijux_ontology.vocabulary.annotations.graphs.types import AnnotationStatement
from bijux_ontology.vocabulary.inspection import VocabularyView
from bijux_ontology.vocabulary.loading import load_vocabulary
from bijux_ontology.vocabulary.obo.reading import read_obo_records
from bijux_ontology.vocabulary.restrictions.projection import project_restrictions

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
_ANCHORS = {RDF.type, OWL.annotatedSource, OWL.annotatedProperty, OWL.annotatedTarget}
Triple = tuple[Node, Node, Node]


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
                OWL.Annotation if isinstance(subject, BNode) else OWL.Axiom,
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
            if role not in {"CLASS", "OBJECT", "ANNOTATION"}:
                raise UnsupportedProjection(
                    "The selected profile needs class, "
                    "object or annotation property roles"
                )
            self.roles[identifier] = role
            self.add(
                identifier,
                RDF.type,
                {
                    "CLASS": OWL.Class,
                    "OBJECT": OWL.ObjectProperty,
                    "ANNOTATION": OWL.AnnotationProperty,
                }[role],
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


@dataclass
class ExportProjection:
    """Account for actual selected assertions while producing only supported meaning."""

    source: PreparedSource
    physical: URIRef | BNode | None
    graph: Graph
    budget: Budget
    issues: IssueCollector
    literal_predicates: tuple[URIRef, ...]
    represented: set[Triple] = field(default_factory=set)
    reports: list[Report] = field(default_factory=list)

    def adopt(self, report: Report) -> None:
        """Attach required native evidence only after current-budget qualification."""
        retain_report(report, self.budget, self.issues)
        self.reports.append(report)

    def plain(self, value: Node) -> str:
        """The selected JSON profile preserves only untagged plain literal text."""
        if (
            not isinstance(value, Literal)
            or value.language
            or value.datatype is not None
            or not str(value).strip()
        ):
            raise UnsupportedProjection(
                "Literal language/datatype or empty "
                "text has no exact selected JSON mapping"
            )
        return str(value)

    def annotation(
        self, root: AnnotationStatement
    ) -> tuple[dict[str, object], set[Triple]]:
        """Inspect native annotation graphs without merging reifier boundaries."""
        selected = inspect_annotation_graph(
            self.source,
            (root,),
            graph=self.physical,
            annotation_predicates=(*self.literal_predicates, SYNONYM_TYPE),
            budget=self.budget,
        )
        self.adopt(selected.report)
        if not selected.report.verified:
            raise UnsupportedProjection(
                "Annotation attachment is not completely qualified"
            )

        def encode(
            statement: AnnotationStatement, depth: int
        ) -> tuple[dict[str, object], set[Triple]]:
            """Map a nested attachment only after every child is representable."""
            self.budget.consume("work")
            if depth > self.budget.limits.max_depth:
                raise LimitExceeded("Annotation projection exceeds max_depth")
            matched = [
                item for item in selected.attachments if item.annotated == statement
            ]
            used: set[Triple] = {
                (statement.subject, statement.predicate, statement.value)
            }
            if not matched:
                return {}, used
            if len(matched) != 1:
                raise UnsupportedProjection(
                    "Multiple annotation boundaries "
                    "cannot collapse into one metadata object"
                )
            attachment = matched[0]
            expected = OWL.Annotation if depth else OWL.Axiom
            if attachment.status != "attached" or attachment.kind != expected:
                raise UnsupportedProjection(
                    "The exact annotation kind has no selected JSON representation"
                )
            result: dict[str, object] = {}
            basics: list[dict[str, object]] = []
            xrefs: list[str] = []
            for qualifier in attachment.qualifiers:
                self.budget.consume("work")
                extra, child_used = encode(qualifier, depth + 1)
                if (
                    qualifier.predicate == SYNONYM_TYPE
                    and not extra
                    and not depth
                    and root.predicate in SYNONYMS.values()
                    and isinstance(qualifier.value, URIRef)
                ):
                    if "synonymType" in result:
                        raise UnsupportedProjection(
                            "Multiple synonym types cannot collapse into one field"
                        )
                    result["synonymType"] = str(qualifier.value)
                elif qualifier.predicate == XREF and not extra:
                    xrefs.append(self.plain(qualifier.value))
                elif qualifier.predicate in self.literal_predicates:
                    basics.append(
                        {
                            "pred": str(qualifier.predicate),
                            "val": self.plain(qualifier.value),
                            **extra,
                        }
                    )
                else:
                    raise UnsupportedProjection(
                        "A qualifier lacks its exact selected RDF term mapping"
                    )
                used.update(child_used)
            for assertion in attachment.statements:
                if assertion.predicate in _ANCHORS:
                    used.add((assertion.subject, assertion.predicate, assertion.value))
            if xrefs:
                result["xrefs"] = xrefs
            if basics:
                result["meta"] = {"basicPropertyValues": basics}
            return result, used

        return encode(root, 0)

    def metadata(
        self, subject: URIRef, view: VocabularyView
    ) -> tuple[dict[str, object], str | None]:
        """Use native OBO records for roles and exact per-assertion qualifiers."""
        metadata: dict[str, object] = {}
        label = None
        obo = read_obo_records(view, str(subject), budget=self.budget)
        self.adopt(obo.report)
        definitions = [item for item in obo.records if item.role == "definition"]
        for record in obo.records:
            self.budget.consume("work")
            if record.role == "definition" and len(definitions) != 1:
                continue
            root = statement(
                record.assertion.subject,
                record.assertion.predicate,
                record.assertion.object,
            )
            try:
                lexical_value = self.plain(root.value)
                extra, used = self.annotation(root)
            except UnsupportedProjection:
                continue
            if record.role == "definition":
                metadata["definition"] = {"val": lexical_value, **extra}
            else:
                scope = next(
                    key
                    for key, predicate in SYNONYMS.items()
                    if predicate == root.predicate
                )
                existing = metadata.setdefault("synonyms", [])
                assert isinstance(existing, list)
                existing.append({"pred": scope, "val": lexical_value, **extra})
            self.represented.update(used)
        labels = tuple(self.graph.objects(subject, RDFS.label))
        deprecated = tuple(self.graph.objects(subject, OWL.deprecated))
        for predicate, value in self.graph.predicate_objects(subject):
            self.budget.consume("work")
            if (subject, predicate, value) in self.represented:
                continue
            if predicate == OWL.deprecated:
                if (
                    len(deprecated) == 1
                    and isinstance(value, Literal)
                    and value.datatype == XSD.boolean
                    and str(value) in {"true", "false"}
                ):
                    metadata["deprecated"] = str(value) == "true"
                    self.represented.add((subject, predicate, value))
                continue
            if predicate not in self.literal_predicates:
                continue
            try:
                lexical = self.plain(value)
                extra, used = self.annotation(statement(subject, predicate, value))
            except UnsupportedProjection:
                continue
            if predicate == RDFS.label and len(labels) == 1 and not extra:
                label = lexical
            else:
                field_name = "xrefs" if predicate == XREF else "basicPropertyValues"
                existing = metadata.setdefault(field_name, [])
                assert isinstance(existing, list)
                existing.append(
                    {
                        "val": lexical,
                        **({} if predicate == XREF else {"pred": str(predicate)}),
                        **extra,
                    }
                )
            self.represented.update(used)
        return metadata, label

    def restriction(
        self, node: Node, classes: set[URIRef], properties: set[URIRef]
    ) -> tuple[dict[str, object], set[Triple]]:
        """Recognize only the exact named-property existential RDF pattern."""
        if not isinstance(node, BNode):
            raise UnsupportedProjection(
                "A logical restriction needs its actual anonymous expression"
            )
        statements = set(self.graph.triples((node, None, None)))
        props, fillers = (
            tuple(self.graph.objects(node, OWL.onProperty)),
            tuple(self.graph.objects(node, OWL.someValuesFrom)),
        )
        if (
            len(props) != 1
            or len(fillers) != 1
            or props[0] not in properties
            or fillers[0] not in classes
        ):
            raise UnsupportedProjection(
                "Only explicitly declared "
                "named-property existential restrictions are supported"
            )
        expected: set[Triple] = {
            (node, RDF.type, OWL.Restriction),
            (node, OWL.onProperty, props[0]),
            (node, OWL.someValuesFrom, fillers[0]),
        }
        if statements != expected:
            raise UnsupportedProjection(
                "Additional restriction constructors "
                "or metadata are not silently projected"
            )
        return {"propertyId": str(props[0]), "fillerId": str(fillers[0])}, expected

    def logical(
        self, expression: Node, classes: set[URIRef], properties: set[URIRef]
    ) -> tuple[dict[str, object], set[Triple]]:
        """Read supported equivalence operands through the existing bounded RDF list."""
        used: set[Triple] = set()
        if isinstance(expression, URIRef) and expression in classes:
            return {"genusIds": [str(expression)]}, used
        if not isinstance(expression, BNode):
            raise UnsupportedProjection(
                "Logical definition requires named genera and existential restrictions"
            )
        if (expression, RDF.type, OWL.Restriction) in self.graph:
            restriction, used = self.restriction(expression, classes, properties)
            return {"genusIds": [], "restrictions": [restriction]}, used
        heads = tuple(self.graph.objects(expression, OWL.intersectionOf))
        if len(heads) != 1:
            raise UnsupportedProjection(
                "Only an explicit intersection has genus/differentia meaning"
            )
        outgoing = set(self.graph.triples((expression, None, None)))
        permitted = {
            (expression, OWL.intersectionOf, heads[0]),
            (expression, RDF.type, OWL.Class),
        }
        if outgoing - permitted:
            raise UnsupportedProjection(
                "Other expression constructors cannot disappear from an intersection"
            )
        used.update(outgoing)
        parts = tuple(
            iter_rdf_list(
                self.graph,
                heads[0],
                max_items=self.budget.limits.max_states,
                budget=self.budget,
            )
        )
        if len(parts) < 2:
            raise UnsupportedProjection(
                "An explicit intersection requires at least two operands"
            )
        cell = heads[0]
        genus: list[str] = []
        restrictions: list[dict[str, object]] = []
        for part in parts:
            self.budget.consume("work")
            tail = tuple(self.graph.objects(cell, RDF.rest))[0]
            used.update({(cell, RDF.first, part), (cell, RDF.rest, tail)})
            if (cell, RDF.type, RDF.List) in self.graph:
                used.add((cell, RDF.type, RDF.List))
            cell = tail
            if isinstance(part, URIRef) and part in classes:
                genus.append(str(part))
            else:
                restriction, structure = self.restriction(part, classes, properties)
                restrictions.append(restriction)
                used.update(structure)
        return {"genusIds": genus, "restrictions": restrictions}, used

    def convert(self, graph_id: URIRef) -> dict[str, object]:
        """Export supported statements and retain all unconsumed source assertions."""
        kinds = {
            "CLASS": OWL.Class,
            "OBJECT": OWL.ObjectProperty,
            "ANNOTATION": OWL.AnnotationProperty,
        }
        roles: dict[URIRef, str] = {}
        for role, kind in kinds.items():
            for subject in self.graph.subjects(RDF.type, kind):
                self.budget.consume("work")
                if isinstance(subject, URIRef):
                    if subject in roles:
                        raise UnsupportedProjection(
                            "Entity role punning has no selected node encoding"
                        )
                    roles[subject] = role
        if (graph_id, RDF.type, OWL.Ontology) not in self.graph:
            raise InputError(
                "The selected graph ID requires its actual ontology declaration"
            )
        if graph_id in roles:
            raise UnsupportedProjection(
                "Ontology and entity node identity need distinct selected roles"
            )
        graph_name = (
            self.physical
            if self.physical is not None
            else "@default"
            if self.source.is_dataset
            else None
        )
        view = VocabularyView(
            load_vocabulary(self.source, graph_name=graph_name, budget=self.budget),
            budget=self.budget,
        )
        self.represented.add((graph_id, RDF.type, OWL.Ontology))
        nodes = []
        for subject, role in sorted(roles.items(), key=lambda item: str(item[0])):
            node: dict[str, object] = {
                "id": str(subject),
                "type": "CLASS" if role == "CLASS" else "PROPERTY",
            }
            if role != "CLASS":
                node["propertyType"] = role
            self.represented.add((subject, RDF.type, kinds[role]))
            metadata, label = self.metadata(subject, view)
            if metadata:
                node["meta"] = metadata
            if label is not None:
                node["lbl"] = label
            nodes.append(node)
        graph_meta, graph_label = self.metadata(graph_id, view)
        if graph_label is not None:
            basics = graph_meta.setdefault("basicPropertyValues", [])
            assert isinstance(basics, list)
            basics.append({"pred": str(RDFS.label), "val": graph_label})
        versions = tuple(self.graph.objects(graph_id, OWL.versionIRI))
        if len(versions) == 1 and isinstance(versions[0], URIRef):
            graph_meta["version"] = str(versions[0])
            self.represented.add((graph_id, OWL.versionIRI, versions[0]))
        classes = {subject for subject, role in roles.items() if role == "CLASS"}
        properties = {subject for subject, role in roles.items() if role == "OBJECT"}
        edges: list[dict[str, object]] = []
        definitions: list[dict[str, object]] = []
        for subject in sorted(classes, key=str):
            selected = project_restrictions(view, str(subject), budget=self.budget)
            self.adopt(selected.report)
            for projection in selected.items:
                if (
                    projection.status != "supported"
                    or projection.quantifier != OWL.someValuesFrom
                    or projection.axiom.predicate != RDFS.subClassOf
                    or projection.axiom.object != projection.expression
                ):
                    continue
                try:
                    restriction, structure = self.restriction(
                        projection.expression, classes, properties
                    )
                    extra, annotations = self.annotation(
                        statement(subject, RDFS.subClassOf, projection.expression)
                    )
                except UnsupportedProjection:
                    continue
                edge = {
                    "sub": str(subject),
                    "pred": restriction["propertyId"],
                    "obj": restriction["fillerId"],
                }
                if extra:
                    if set(extra) - {"meta"}:
                        continue
                    edge["meta"] = extra["meta"]
                edges.append(edge)
                self.represented.update(structure | annotations)
            for target in self.graph.objects(subject, RDFS.subClassOf):
                if target not in classes:
                    continue
                try:
                    extra, used = self.annotation(
                        statement(subject, RDFS.subClassOf, target)
                    )
                except UnsupportedProjection:
                    continue
                edge = {"sub": str(subject), "pred": "is_a", "obj": str(target)}
                if extra:
                    if set(extra) - {"meta"}:
                        continue
                    edge["meta"] = extra["meta"]
                edges.append(edge)
                self.represented.update(used)
            for expression in self.graph.objects(subject, OWL.equivalentClass):
                try:
                    definition, used = self.logical(expression, classes, properties)
                    extra, annotations = self.annotation(
                        statement(subject, OWL.equivalentClass, expression)
                    )
                except (UnsupportedProjection, InputError):
                    continue
                if set(extra) - {"meta"}:
                    continue
                definitions.append(
                    {"definedClassId": str(subject), **definition, **extra}
                )
                self.represented.update(used | annotations)
        result: dict[str, object] = {
            "id": str(graph_id),
            "nodes": nodes,
            "edges": edges,
        }
        if graph_meta:
            result["meta"] = graph_meta
        if definitions:
            result["logicalDefinitionAxioms"] = definitions
        return {"graphs": [result]}
