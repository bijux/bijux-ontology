"""Selected SKOS resource, collection and native label facts for one operation."""

from dataclasses import dataclass, field

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import RDF, SKOS, XSD
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import Budget, IssueCollector
from bijux_ontology.vocabulary.browsing.results import Edge, Selection

from .collections import collection_memberships
from .selection import CollectionScope, RuleSelection, SkosScope, collection_scope

LABEL_PREDICATES = (SKOS.prefLabel, SKOS.altLabel, SKOS.hiddenLabel)


def literal_key(value: Literal) -> tuple[str, str, str]:
    """Retain lexical identity with RDF-equivalent language and string types."""
    return (
        str(value),
        (value.language or "").lower(),
        str(
            value.datatype
            if value.datatype is not None
            else RDF.langString
            if value.language
            else XSD.string
        ),
    )


def is_lexical(value: Node) -> bool:
    """Recognise plain or language-tagged string literals without coercion."""
    return isinstance(value, Literal) and (
        value.language is not None or value.datatype in (None, XSD.string)
    )


@dataclass(slots=True)
class ResourceFacts:
    """Actual selected evidence; semantic rule results are never memoized."""

    scope: SkosScope
    _budget: Budget = field(repr=False)
    collections: CollectionScope
    members: dict[Node, frozenset[Node]]
    notation_groups: dict[Node, frozenset[Node]]
    notation_nodes: set[Node]
    labels: dict[Node, dict[URIRef, set[Node]]]
    _reader: object = field(init=False, repr=False)
    _index: object = field(init=False, repr=False)
    _descriptions: dict[URIRef | BNode, Selection[Edge]] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self._reader = self.scope.view._reader
        self._index = self.scope.view._index
        self.qualify(self._budget)

    def qualify(self, budget: Budget) -> None:
        """Pin the actual owned read and native inspection facts, not view identity."""
        if budget is not self._budget:
            raise InputError(
                "Selected SKOS resource facts require their originating budget"
            )
        view = self.scope.view
        if (
            view._reader is not self._reader
            or view._index is not self._index
            or view._read() is not self.scope.graph
        ):
            raise InputError("Selected SKOS resource view changed during checking")

    def label_statements(self, node: URIRef | BNode, budget: Budget) -> Selection[Edge]:
        """Read outgoing label metadata once, retaining its actual native receipt."""
        self.qualify(budget)
        budget.consume("work")
        prior = self._descriptions.get(node)
        if prior is not None:
            return prior
        budget.consume("states")
        budget.consume("bytes", 128)
        selected = self.scope.view.statements(node, budget=budget)
        self.qualify(budget)
        if selected.report.complete:
            budget.consume("bytes", 64 + 16 * len(selected.items))
            self._descriptions[node] = selected
        return selected


def prepare_resource_facts(
    scope: SkosScope, policy: RuleSelection, budget: Budget, issues: IssueCollector
) -> ResourceFacts:
    """Prepare original collection/plain-label facts before incremental rule checks."""
    graph, facts, resources = scope.graph, scope.facts, scope.resources
    collections = collection_scope(scope, policy)
    members = collection_memberships(
        graph,
        facts.members,
        scope=collections,
        budget=budget,
        issues=issues,
    )
    notation_groups = {
        collection: scope.select(members.get(collection, ()))
        for collection in collections.notation
    }
    notation_nodes = (
        {node for values in notation_groups.values() for node in values}
        if policy.notation_collections
        else set(resources)
    )
    for collection in sorted(collections.notation, key=lambda node: node.n3()):
        if SKOS.Collection not in facts.roles.get(collection, ()):
            issues.append(
                Issue(
                    "skos.policy.notation_scope_unavailable",
                    "Requested collection lacks supplied membership evidence",
                    str(collection),
                    kind="unverified",
                )
            )
    labels = {
        node: {
            predicate: set(graph.objects(node, predicate))
            for predicate in LABEL_PREDICATES
        }
        for node in resources
    }
    budget.consume("states")
    budget.consume("bytes", 256)
    return ResourceFacts(
        scope, budget, collections, members, notation_groups, notation_nodes, labels
    )
