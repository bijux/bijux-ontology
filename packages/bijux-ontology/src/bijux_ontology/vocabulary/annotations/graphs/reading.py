"""Bounded exact OWL annotation closures over one authorized physical graph."""

import threading
from collections.abc import Sequence
from typing import Literal as Choice
from typing import cast

from rdflib import BNode, Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS
from rdflib.term import Node

from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.rdf.roles.classification import classify_statement, declaration_role
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, term_key
from bijux_ontology.reports import Issue, Report

from .types import (
    AnnotationAttachment,
    AnnotationGraph,
    AnnotationPath,
    AnnotationResource,
    AnnotationStatement,
    AnnotationValue,
)

_ANCHORS = (OWL.annotatedSource, OWL.annotatedProperty, OWL.annotatedTarget)
_STANDARD = frozenset(
    (
        RDFS.label,
        RDFS.comment,
        RDFS.seeAlso,
        RDFS.isDefinedBy,
        OWL.versionInfo,
        OWL.priorVersion,
        OWL.backwardCompatibleWith,
        OWL.incompatibleWith,
    )
)


def _same(left: Node, right: Node) -> bool:
    """Blank witnesses require object ownership, all other RDF terms exact identity."""
    return (
        left is right
        if isinstance(left, BNode) or isinstance(right, BNode)
        else (term_key(left) == term_key(right))
    )


def _bound(statement: AnnotationStatement, budget: Budget) -> None:
    """Qualify statement metadata before lookup, hashing or retained diagnostics."""
    for term in (statement.subject, statement.predicate, statement.value):
        bound_node(term, budget)


def _physical(
    source: PreparedSource, graph: URIRef | BNode | None | Absent, budget: Budget
) -> tuple[URIRef | BNode | None, Graph]:
    """Keep explicit dataset selection and actual owned blank graph witnesses."""
    if not isinstance(source, PreparedSource):
        raise InputError("Annotation graphs require an owned PreparedSource")
    if source.union:
        raise InputError("Annotation attachments require one physical graph")
    name = source.graph_name if graph is ABSENT else graph
    if name is not None:
        if not isinstance(name, str | BNode):
            raise InputError("Annotation graph selection requires an RDF graph name")
        bound_text(name, budget)
        if isinstance(name, URIRef | BNode):
            bound_node(name, budget)
        if type(name) is str and name == "@default":
            name = None
    if source.is_dataset and graph is ABSENT and source.graph_name is None:
        raise InputError("Dataset annotation inspection requires an explicit graph")
    if not source.is_dataset and name is not None:
        raise InputError("A graph source cannot authorize a named graph")
    if isinstance(name, BNode) and not any(
        actual is name for actual in source._graph_names(budget=budget)
    ):
        raise InputError("A blank graph name requires its actual owned term")
    selected = (
        None if name is None else name if isinstance(name, BNode) else URIRef(name)
    )
    return selected, source._read(
        selected if selected is not None else "@default" if source.is_dataset else None,
        budget=budget,
    )


class _Read:
    """Retain only charged original statements, attachment records and path prefixes."""

    def __init__(
        self, graph: Graph, predicates: tuple[URIRef, ...], budget: Budget
    ) -> None:
        self.graph, self.budget = graph, budget
        budget.consume("states", len(predicates))
        self.predicates = frozenset(predicates)
        self.issues = IssueCollector(budget)
        self.complete = True
        self.attachments: dict[AnnotationResource, AnnotationAttachment] = {}
        self.statements: dict[AnnotationStatement, None] = {}
        self.paths: list[AnnotationPath] = []

    def issue(self, message: str, *, malformed: bool = False) -> None:
        """Do not serialize unqualified node labels or arbitrary term payloads."""
        self.complete = False
        self.issues.append(
            Issue(
                "annotations.attachment_shape"
                if malformed
                else "annotations.graph_unverified",
                message,
                kind="finding" if malformed else "unverified",
            )
        )

    def edges(self, node: AnnotationResource) -> tuple[AnnotationStatement, ...]:
        """Use the existing graph index and charge each actual outgoing assertion."""
        result = []
        for subject, predicate, value in self.graph.triples((node, None, None)):
            self.budget.consume("work")
            self.budget.consume("triples")
            for term in (subject, predicate, value):
                bound_node(term, self.budget)
            self.budget.consume("states")
            self.budget.consume("bytes", 96)
            result.append(
                AnnotationStatement(
                    cast(AnnotationResource, subject),
                    cast(URIRef, predicate),
                    cast(AnnotationValue, value),
                )
            )
        return tuple(result)

    def retain(self, statement: AnnotationStatement) -> None:
        """Reserve every retained assertion before adding its original RDF terms."""
        self.budget.consume("work")
        _bound(statement, self.budget)
        if statement not in self.statements:
            self.budget.consume("states")
            self.budget.consume("bytes", 96)
            self.statements[statement] = None

    def exists(self, statement: AnnotationStatement) -> bool:
        """Require actual subject/object blank witnesses, never equal label guesses."""
        _bound(statement, self.budget)
        # RDFLib echoes bound query terms; leave blank subjects unbound so
        # identity checks inspect the stored witness rather than caller input.
        subject_filter = (
            None if isinstance(statement.subject, BNode) else statement.subject
        )
        for subject, _, value in self.graph.triples(
            (subject_filter, statement.predicate, None)
        ):
            self.budget.consume("work")
            bound_node(subject, self.budget)
            bound_node(value, self.budget)
            if _same(subject, statement.subject) and _same(value, statement.value):
                return True
        return False

    def accepted(self, statement: AnnotationStatement) -> bool:
        """Annotation roles cannot override structural or logical predicates."""
        predicate = statement.predicate
        if predicate not in _STANDARD and any(
            str(predicate).startswith(str(ns)) for ns in (RDF, RDFS, OWL)
        ):
            return False
        roles = classify_statement(
            (statement.subject, predicate, statement.value), budget=self.budget
        )
        if any(
            role.position == "predicate" and role.role == "structural" for role in roles
        ):
            return False
        declared = False
        for _, _, kind in self.graph.triples((predicate, RDF.type, None)):
            self.budget.consume("work")
            bound_node(kind, self.budget)
            if isinstance(kind, URIRef) and (
                declaration_role(kind) in ("object_property", "datatype_property")
                or kind == OWL.FunctionalProperty
            ):
                return False
            declared |= kind == OWL.AnnotationProperty
        return predicate in _STANDARD or predicate in self.predicates or declared

    def attachment(
        self, node: AnnotationResource, statement: AnnotationStatement
    ) -> AnnotationAttachment | None:
        """Qualify every anchor against the same exact selected base assertion."""
        cached = self.attachments.get(node)
        edges = cached.statements if cached is not None else self.edges(node)
        components = []
        for predicate in _ANCHORS:
            self.budget.consume("work", len(edges))
            components.append(
                tuple(edge.value for edge in edges if edge.predicate == predicate)
            )
        sources, properties, targets = components
        if properties and not any(
            _same(item, statement.predicate) for item in properties
        ):
            return None
        if targets and not any(_same(item, statement.value) for item in targets):
            return None
        if cached is not None:
            return cached
        self.budget.consume("states", len(edges) + 1)
        kinds = {
            edge.value
            for edge in edges
            if edge.predicate == RDF.type and edge.value in (OWL.Axiom, OWL.Annotation)
        }
        qualifiers = tuple(
            edge
            for edge in edges
            if edge.predicate not in _ANCHORS
            and not (
                edge.predicate == RDF.type and edge.value in (OWL.Axiom, OWL.Annotation)
            )
        )
        malformed = any(len(items) != 1 for items in components) or len(kinds) != 1
        annotated = None
        status: Choice["attached", "malformed", "unresolved"] = "malformed"
        kind = next(iter(kinds)) if len(kinds) == 1 else None
        if (
            malformed
            or not isinstance(sources[0], URIRef | BNode)
            or not isinstance(properties[0], URIRef)
        ):
            self.issue(
                "Attachment requires one source, property, target and OWL role",
                malformed=True,
            )
        else:
            annotated = AnnotationStatement(sources[0], properties[0], targets[0])
            if not self.exists(annotated):
                self.issue(
                    "Attachment lacks its exact source-scoped base assertion",
                    malformed=True,
                )
            elif not (
                _same(sources[0], statement.subject)
                and _same(properties[0], statement.predicate)
                and _same(targets[0], statement.value)
            ):
                self.issue(
                    "Attachment cannot be associated with this exact owned statement"
                )
            else:
                status = "attached"
        references = []
        for subject, incoming_predicate, value in self.graph.triples(
            (None, None, node)
        ):
            self.budget.consume("work")
            self.budget.consume("triples")
            for term in (subject, incoming_predicate, value):
                bound_node(term, self.budget)
            self.budget.consume("states")
            self.budget.consume("bytes", 96)
            reference = AnnotationStatement(
                cast(AnnotationResource, subject),
                cast(URIRef, incoming_predicate),
                cast(AnnotationValue, value),
            )
            references.append(reference)
            self.retain(reference)
        result = AnnotationAttachment(
            node,
            cast(URIRef | None, kind),
            annotated,
            edges,
            qualifiers,
            status,
            tuple(references),
        )
        self.attachments[node] = result
        for edge in edges:
            self.retain(edge)
        return result

    def walk(self, root: AnnotationStatement) -> None:
        """Traverse original annotation links with bounded paths and cycles."""
        pending: list[tuple[AnnotationStatement, tuple[AnnotationResource, ...]]] = [
            (root, ())
        ]
        while pending:
            statement, chain = pending.pop()
            self.budget.consume("work")
            if len(chain) > self.budget.limits.max_depth:
                raise LimitExceeded("Annotation graph exceeds max_depth")
            self.retain(statement)
            self.budget.consume("states")
            self.budget.consume("bytes", 96 + 8 * len(chain))
            self.paths.append(AnnotationPath(root, chain, statement))
            for node, _, actual in self.graph.triples(
                (None, OWL.annotatedSource, statement.subject)
            ):
                self.budget.consume("work")
                bound_node(node, self.budget)
                bound_node(actual, self.budget)
                if not _same(actual, statement.subject) or not isinstance(
                    node, URIRef | BNode
                ):
                    continue
                attachment = self.attachment(node, statement)
                if attachment is None:
                    continue
                self.budget.consume("work", len(chain))
                if any(_same(node, ancestor) for ancestor in chain):
                    self.issue(
                        "Annotation attachment cycle prevents complete interpretation"
                    )
                    continue
                if attachment.status != "attached":
                    continue
                self.budget.consume("bytes", 8 * (len(chain) + 1))
                next_chain = (*chain, node)
                for qualifier in attachment.qualifiers:
                    self.budget.consume("work")
                    if not self.accepted(qualifier):
                        self.issue(
                            "Qualifier requires an explicit annotation-property profile"
                        )
                    self.budget.consume("states")
                    self.budget.consume("bytes", 64 + 8 * len(next_chain))
                    pending.append((qualifier, next_chain))


def inspect_annotation_graph(
    source: PreparedSource,
    statements: Sequence[AnnotationStatement],
    *,
    graph: URIRef | BNode | None | Absent = ABSENT,
    annotation_predicates: Sequence[URIRef] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30.0,
    cancel: threading.Event | None = None,
) -> AnnotationGraph:
    """Read explicit roots and recursively attached OWL annotation assertions.

    Direct assertions and resource-valued qualifiers keep their original terms;
    referenced resource bodies are not presumed annotations. Dataset input needs
    a physical graph (or its existing pin). Missing/malformed anchors, cycles and
    unqualified predicates retain a bounded reviewable prefix and prevent green.
    This is annotation encoding inspection, not logical axiom classification.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    roots: tuple[AnnotationStatement, ...] = ()
    predicates: tuple[URIRef, ...] = ()
    selected = None
    read = None
    checked = 0
    requested = 1
    qualified_roots: list[AnnotationStatement] = []
    qualified_predicates: list[URIRef] = []
    try:
        supplied = capture_items(statements, AnnotationStatement, operation)
        requested = max(1, len(supplied))
        for root in supplied:
            _bound(root, operation)
            qualified_roots.append(root)
        roots = tuple(qualified_roots)
        supplied_predicates = capture_items(annotation_predicates, URIRef, operation)
        for predicate in supplied_predicates:
            bound_node(predicate, operation)
            qualified_predicates.append(predicate)
        predicates = tuple(qualified_predicates)
        selected, selected_graph = _physical(source, graph, operation)
        read = _Read(selected_graph, predicates, operation)
        issues = read.issues
        if not roots:
            read.issue(
                "An annotation inspection requires explicit nonempty root statements"
            )
        for root in roots:
            _bound(root, operation)
            if not read.exists(root):
                read.issue(
                    "Selected root is absent or lacks its actual owned blank witness"
                )
                continue
            if source.literal_fidelity not in ("source_terms", "supplied_terms"):
                read.issue(
                    "Exact annotation terms require established source literal fidelity"
                )
            read.walk(root)
            checked += 1
    except LimitExceeded as error:
        roots = tuple(qualified_roots)
        predicates = tuple(qualified_predicates)
        issues.exhausted(str(error))
    return AnnotationGraph(
        source,
        selected,
        roots,
        predicates,
        tuple(read.attachments.values()) if read else (),
        tuple(read.paths) if read else (),
        tuple(read.statements) if read else (),
        Report(
            "annotations.graph",
            checked,
            tuple(issues),
            requested=requested,
            allow_empty=True,
            complete=read.complete if read is not None else False,
            regime="asserted-annotation-encoding",
        ),
    )
