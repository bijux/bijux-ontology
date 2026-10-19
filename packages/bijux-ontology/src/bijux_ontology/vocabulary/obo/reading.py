"""Selected OBO definition/synonym interpretation over existing owned indexes."""

from collections.abc import Sequence
from dataclasses import replace
from typing import Literal as Role

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import OWL, RDF
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.limits import Budget, IssueCollector
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.terms import term_key
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.browsing.results import Edge, Selection

from .types import OboAttachment, OboRecord, OboRecords

_ROLE: dict[str, Role["definition", "exact", "broad", "narrow", "related"]] = {
    "http://purl.obolibrary.org/obo/IAO_0000115": "definition",
    "http://www.geneontology.org/formats/oboInOwl#hasExactSynonym": "exact",
    "http://www.geneontology.org/formats/oboInOwl#hasBroadSynonym": "broad",
    "http://www.geneontology.org/formats/oboInOwl#hasNarrowSynonym": "narrow",
    "http://www.geneontology.org/formats/oboInOwl#hasRelatedSynonym": "related",
}
_ANCHORS = (OWL.annotatedSource, OWL.annotatedProperty, OWL.annotatedTarget)
_SYNONYM_TYPE = URIRef("http://www.geneontology.org/formats/oboInOwl#hasSynonymType")


class _OboRead:
    """Share one operation budget and preserve completed records before tails."""

    def __init__(self, view: VocabularyView, budget: Budget, iri: str) -> None:
        """Retain only selected readers, immutable edges and bounded local state."""
        self.view = view
        self.budget = budget
        self.iri = iri
        self.issues = IssueCollector(budget)
        self.complete = True

    def edges(
        self, node: Node, *, incoming: bool = False, predicates: Sequence[str] = ()
    ) -> Selection[Edge]:
        """Use the existing targeted index; child issues already share counters."""
        if not isinstance(node, URIRef | BNode | Literal):
            raise InputError("Unsupported OBO annotation node")
        result = self.view.statements(
            node,
            predicates=predicates,
            direction="incoming" if incoming else "outgoing",
            budget=self.budget,
        )
        list.extend(self.issues, result.report.issues)
        self.complete &= result.report.complete
        return result

    def issue(self, code: str, message: str, *, unverified: bool = False) -> None:
        """Locate convention diagnostics at the selected named entity."""
        self.issues.append(
            Issue(
                code, message, self.iri, kind="unverified" if unverified else "finding"
            )
        )

    def groups(self, edges: tuple[Edge, ...]) -> dict[str, list[Edge]]:
        """Group one node's assertions by their original source, never by text."""
        result: dict[str, list[Edge]] = {}
        for edge in edges:
            self.budget.consume("work")
            if edge.scope not in result:
                self.budget.consume("states")
                self.budget.consume("bytes", 128)
                result[edge.scope] = []
            self.budget.consume("states")
            self.budget.consume("bytes", 8)
            result[edge.scope].append(edge)
        return result

    def attachment(
        self,
        node: URIRef | BNode,
        scope: str,
        statements: tuple[Edge, ...],
        bases: tuple[Edge, ...],
        *,
        complete: bool,
        base_complete: bool,
    ) -> OboAttachment | None:
        """Attach only a complete exact anchor whose base exists in that source."""
        self.budget.consume("work", len(statements))
        self.budget.consume("states", len(statements) + 1)
        self.budget.consume("bytes", 256 + 16 * len(statements))
        components = [
            tuple(edge.object for edge in statements if edge.predicate == predicate)
            for predicate in _ANCHORS
        ]
        sources, properties, targets = components
        if properties and not any(str(value) in _ROLE for value in properties):
            return None
        qualifiers = tuple(
            edge
            for edge in statements
            if edge.predicate not in _ANCHORS
            and not (
                edge.predicate == RDF.type
                and edge.object in (OWL.Axiom, OWL.Annotation)
            )
        )
        status: Role["attached", "malformed", "unresolved"] = "unresolved"
        matches: tuple[Edge, ...] = ()
        if complete:
            types = {
                edge.object
                for edge in statements
                if edge.predicate == RDF.type
                and edge.object in (OWL.Axiom, OWL.Annotation)
            }
            if any(len(values) != 1 for values in components) or len(types) > 1:
                self.issue(
                    "obo.attachment_shape",
                    "Annotation attachment has missing or ambiguous anchor components",
                )
                status = "malformed"
            elif not types:
                self.issue(
                    "obo.attachment_unverified",
                    "Annotation anchors lack a supported OWL attachment role",
                    unverified=True,
                )
            elif sources[0] != URIRef(self.iri) or not isinstance(
                properties[0], URIRef
            ):
                self.issue(
                    "obo.attachment_shape",
                    "Annotation attachment has invalid source or property roles",
                )
                status = "malformed"
            else:
                target = term_key(targets[0])
                selected = []
                for base in bases:
                    self.budget.consume("work")
                    if (
                        base.scope == scope
                        and base.predicate == properties[0]
                        and term_key(base.object) == target
                    ):
                        selected.append(base)
                matches = tuple(selected)
                if matches:
                    status = "attached"
                elif base_complete:
                    self.issue(
                        "obo.base_missing",
                        "Exact annotated OBO assertion is absent from its source scope",
                    )
                else:
                    self.issue(
                        "obo.attachment_unverified",
                        "Incomplete base inventory cannot establish this attachment",
                        unverified=True,
                    )
        for edge in qualifiers:
            self.budget.consume("work")
            if edge.predicate == _SYNONYM_TYPE and not isinstance(edge.object, URIRef):
                self.issue(
                    "obo.synonym_type_shape",
                    "Synonym type requires a named IRI; original term retained",
                )
        return OboAttachment(
            node, scope, status, matches, statements, qualifiers, complete
        )


def read_obo_records(
    view: VocabularyView, iri: str, *, budget: Budget | None = None
) -> OboRecords:
    """Read the explicitly selected native OBO-in-OWL annotation convention.

    Definition and four synonym roles follow OBO Format 1.4's OWL mapping.
    Attachment lookup begins with explicit annotatedSource assertions for this
    entity. Full source/property/target RDF identity and actual source scope are
    required before qualifiers attach; blank labels never join separate inputs.
    Equal text and separate reification nodes do not merge annotation sets.
    Unselected axiom properties are outside this reader. Opaque qualifier edges
    remain uninterpreted; this does not parse OBO text, resolve alternate IDs,
    check release policy or recursively validate nested annotations.
    """
    if (
        not isinstance(view, VocabularyView)
        or budget is not None
        and not isinstance(budget, Budget)
    ):
        raise InputError("Expected an owned VocabularyView and optional Budget")
    if not isinstance(iri, str) or isinstance(iri, BNode | Literal):
        raise InputError("OBO selection requires a named IRI string")
    operation = budget if budget is not None else Budget(view.limits)
    if len(iri) > operation.limits.max_bytes:
        raise LimitExceeded("OBO identifier exceeds max_bytes")
    absolute_iri(iri)
    read = _OboRead(view, operation, iri)
    bases: tuple[Edge, ...] = ()
    records: list[OboRecord] = []
    attachments: list[OboAttachment] = []
    base_complete = False
    try:
        operation.consume("work", len(iri))
        try:
            size = len(iri.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise InputError("OBO identifiers require valid Unicode") from error
        if size > operation.limits.max_bytes:
            raise LimitExceeded("OBO identifier exceeds max_bytes")
        operation.consume("bytes", size)
        selected = read.edges(URIRef(iri), predicates=tuple(_ROLE))
        bases, base_complete = selected.items, selected.report.complete
        for edge in bases:
            operation.consume("work")
            operation.consume("states")
            operation.consume("bytes", 128)
            records.append(OboRecord(_ROLE[str(edge.predicate)], edge))
            if not isinstance(edge.object, Literal):
                read.issue(
                    "obo.value_kind",
                    "OBO text requires a literal; original assertion retained",
                )
        incoming = read.edges(
            URIRef(iri), incoming=True, predicates=(str(OWL.annotatedSource),)
        )
        operation.consume("work", len(incoming.items))
        operation.consume("states", len(incoming.items))
        operation.consume("bytes", 128 * len(incoming.items))
        selected_pairs = frozenset(
            (edge.subject, edge.scope) for edge in incoming.items
        )
        seen: set[Node] = set()
        for source in incoming.items:
            operation.consume("work")
            node = source.subject
            if node in seen:
                continue
            operation.consume("states")
            seen.add(node)
            if not isinstance(node, URIRef | BNode):
                continue
            outgoing = read.edges(node)
            for scope, edges in read.groups(outgoing.items).items():
                if (node, scope) not in selected_pairs:
                    continue
                attachment = read.attachment(
                    node,
                    scope,
                    tuple(edges),
                    bases,
                    complete=outgoing.report.complete,
                    base_complete=base_complete,
                )
                if attachment is None:
                    continue
                attachments.append(attachment)
                if attachment.status == "attached":
                    for index, record in enumerate(records):
                        operation.consume("work")
                        if record.assertion in attachment.assertions:
                            operation.consume("states")
                            operation.consume(
                                "bytes", 128 + 8 * (len(record.attachments) + 1)
                            )
                            records[index] = replace(
                                record, attachments=record.attachments + (attachment,)
                            )
        if read.complete:
            for index, record in enumerate(records):
                operation.consume("work")
                records[index] = replace(record, complete=True)
    except LimitExceeded as error:
        read.complete = False
        read.issues.exhausted(str(error), location=iri)
    return OboRecords(
        tuple(records),
        tuple(attachments),
        Report(
            "obo.records",
            len(records),
            tuple(read.issues),
            allow_empty=True,
            requested=len(records) if read.complete else len(records) + 1,
            complete=read.complete,
            regime="asserted",
            families=("obo-records",),
        ),
        bases,
    )
