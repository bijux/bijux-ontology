"""Bounded operation-local scheme/root normalization over one owned view."""

from dataclasses import dataclass

from rdflib import BNode, URIRef
from rdflib.namespace import SKOS
from rdflib.term import Node

from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.limits import Budget, IssueCollector
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.browsing.results import Edge, Selection

_MEMBERSHIP = tuple(map(str, (SKOS.inScheme, SKOS.topConceptOf, SKOS.hasTopConcept)))


@dataclass(frozen=True)
class _SchemeScope:
    roots: frozenset[Node]
    members: frozenset[Node]
    assertions: tuple[Edge, ...]
    complete: bool


@dataclass(frozen=True)
class _MembershipScope:
    schemes: frozenset[Node]
    complete: bool


class _PublicationRead:
    """Share exact inverse normalization, never a global graph or inference cache."""

    def __init__(self, view: VocabularyView, budget: Budget) -> None:
        self.view = view
        self.budget = budget
        self.issues = IssueCollector(budget)
        self.complete = True
        self._schemes: dict[Node, _SchemeScope] = {}
        self._memberships: dict[Node, _MembershipScope] = {}
        self._broader: dict[Node, Selection[Edge]] = {}

    def _links(
        self, node: URIRef | BNode, predicates: tuple[str, ...]
    ) -> Selection[Edge]:
        selected = self.view.statements(
            node, predicates=predicates, direction="both", budget=self.budget
        )
        list.extend(self.issues, selected.report.issues)
        self.complete &= selected.report.complete
        return selected

    def _resource(self, node: Node, anchor: Node) -> bool:
        if isinstance(node, URIRef | BNode):
            return True
        self.complete = False
        self.issues.append(
            Issue(
                "skos.publication.reference_unverified",
                "Publication links require resource endpoints",
                str(anchor),
                kind="unverified",
                actual=node.n3(),
            )
        )
        return False

    def scheme(self, scheme: URIRef) -> _SchemeScope:
        """Normalize top inverses and membership without imposing publication rules."""
        if scheme not in self._schemes:
            self.budget.consume("states")
            selected = self._links(scheme, _MEMBERSHIP)
            roots: set[Node] = set()
            members: set[Node] = set()
            assertions = []
            for edge in selected.items:
                self.budget.consume("work")
                member = None
                top = False
                if edge.subject == scheme and edge.predicate == SKOS.hasTopConcept:
                    member, top = edge.object, True
                elif edge.object == scheme and edge.predicate in (
                    SKOS.inScheme,
                    SKOS.topConceptOf,
                ):
                    member, top = edge.subject, edge.predicate == SKOS.topConceptOf
                if member is None or not self._resource(member, scheme):
                    continue
                self.budget.consume("states")
                members.add(member)
                if top:
                    roots.add(member)
                    assertions.append(edge)
            self._schemes[scheme] = _SchemeScope(
                frozenset(roots),
                frozenset(members),
                tuple(assertions),
                selected.report.complete,
            )
        return self._schemes[scheme]

    def memberships(self, node: URIRef | BNode) -> _MembershipScope:
        """Retain known scheme memberships and whether their inventory completed."""
        if node not in self._memberships:
            self.budget.consume("states")
            selected = self._links(node, _MEMBERSHIP)
            schemes = set()
            complete = selected.report.complete
            for edge in selected.items:
                self.budget.consume("work")
                scheme = None
                if edge.subject == node and edge.predicate in (
                    SKOS.inScheme,
                    SKOS.topConceptOf,
                ):
                    scheme = edge.object
                elif edge.object == node and edge.predicate == SKOS.hasTopConcept:
                    scheme = edge.subject
                if scheme is not None:
                    if self._resource(scheme, node):
                        self.budget.consume("states")
                        schemes.add(scheme)
                    else:
                        complete = False
            self._memberships[node] = _MembershipScope(frozenset(schemes), complete)
        return self._memberships[node]

    def broader(self, node: URIRef | BNode) -> Selection[Edge]:
        """Select authored broader and inverse narrower assertions only."""
        if node not in self._broader:
            self.budget.consume("states")
            selected = self._links(node, tuple(map(str, (SKOS.broader, SKOS.narrower))))
            self._broader[node] = Selection(
                tuple(
                    edge
                    for edge in selected.items
                    if edge.subject == node
                    and edge.predicate == SKOS.broader
                    or edge.object == node
                    and edge.predicate == SKOS.narrower
                ),
                selected.report,
            )
        return self._broader[node]
