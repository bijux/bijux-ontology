"""Scoped syntactic OWL restriction projection without modifying RDF assertions."""

from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal as Status

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, IssueCollector
from bijux_ontology.owl.constructs import _UNSUPPORTED as _EXPRESSION_PREDICATES
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.structure import iter_rdf_list
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.browsing.results import Edge, Selection

from .types import RestrictionProjection

_QUANTIFIERS = frozenset((OWL.someValuesFrom, OWL.allValuesFrom, OWL.hasValue))
_SUPPORTED = frozenset(map(str, (*_QUANTIFIERS, OWL.onProperty, OWL.intersectionOf)))
_CONSTRAINTS = _QUANTIFIERS | frozenset(
    (
        OWL.hasSelf,
        OWL.cardinality,
        OWL.minCardinality,
        OWL.maxCardinality,
        OWL.qualifiedCardinality,
        OWL.minQualifiedCardinality,
        OWL.maxQualifiedCardinality,
    )
)


@dataclass(frozen=True)
class _NamedConjunct:
    """A named superclass reached through actual intersection/list assertions."""

    owner: URIRef
    evidence: tuple[Edge, ...]
    depth: int


def _operation(view: VocabularyView, budget: Budget | None) -> Budget:
    """Require an owned view and one cumulative optional allowance."""
    if not isinstance(view, VocabularyView) or (
        budget is not None and not isinstance(budget, Budget)
    ):
        raise InputError("Expected a VocabularyView and optional Budget")
    return budget if budget is not None else Budget(view.limits)


def _named(value: str, budget: Budget) -> URIRef:
    """Bound a public named identifier before validation and capture."""
    if not isinstance(value, str) or isinstance(value, BNode | Literal):
        raise InputError("Restriction operations require named IRI strings")
    if len(value) > budget.limits.max_bytes:
        raise LimitExceeded("Restriction identifier exceeds max_bytes")
    budget.consume("work", len(value))
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise InputError("Restriction identifiers require valid Unicode") from error
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Restriction identifier exceeds max_bytes")
    budget.consume("bytes", size)
    return URIRef(absolute_iri(value))


def _report(name: str, count: int, issues: IssueCollector, complete: bool) -> Report:
    """Separate complete finite inspection from unsupported logical alternatives."""
    return Report(
        name,
        count,
        tuple(issues),
        allow_empty=True,
        complete=complete,
        requested=count if complete else count + 1,
        regime="asserted",
        families=(name,),
    )


class _Projection:
    """Reuse scoped reads within one bounded operation, retaining actual edges."""

    def __init__(self, view: VocabularyView, budget: Budget) -> None:
        self.view = view
        self.budget = budget
        self.issues = IssueCollector(budget)
        self.complete = True
        self.facts: dict[Node, tuple[Edge, ...]] = {}

    def edges(self, node: Node) -> tuple[Edge, ...]:
        """Read each actual node once; a truncated read cannot justify a shape."""
        if not isinstance(node, URIRef | BNode | Literal):
            raise InputError("Restriction lookup requires an RDF resource or literal")
        if node not in self.facts:
            self.budget.consume("states")
            selected = self.view.statements(node, budget=self.budget)
            list.extend(self.issues, selected.report.issues)
            if not selected.report.complete:
                self.complete = False
                raise LimitExceeded("Restriction source read is incomplete")
            self.budget.consume("bytes", 128 + 64 * len(selected.items))
            self.facts[node] = selected.items
        return self.facts[node]

    def scoped(self, node: Node, scope: str) -> tuple[Edge, ...]:
        """Keep components of named or blank expressions in their authored source."""
        selected: list[Edge] = []
        for edge in self.edges(node):
            self.budget.consume("work")
            if edge.scope == scope:
                self.budget.consume("states")
                self.budget.consume("bytes", 64)
                selected.append(edge)
        return tuple(selected)

    def graph(self, scope: str) -> Graph:
        """Borrow the existing source read boundary solely for shared list decoding."""
        for name, graph in self.view._reader.scopes:
            self.budget.consume("work")
            if name == scope:
                return graph
        raise InputError("Restriction axiom has no selected source scope")

    def record(
        self,
        owner: URIRef,
        authored: URIRef,
        axiom: Edge,
        node: Node,
        chain: tuple[Edge, ...],
        witness: tuple[Edge, ...],
        status: Status["supported", "malformed", "unsupported"],
        prop: URIRef | None = None,
        quantifier: URIRef | None = None,
        filler: Node | None = None,
    ) -> RestrictionProjection:
        """Charge retained witness references before publishing a projection."""
        size = len(chain) + len(witness) + 1
        self.budget.consume("work", size)
        self.budget.consume("states", size)
        self.budget.consume("bytes", 256 + 64 * size)
        if status != "supported":
            self.complete = False
            self.issues.append(
                Issue(
                    "terms.restriction_malformed"
                    if status == "malformed"
                    else "terms.restriction_unverified",
                    "Restriction expression is malformed"
                    if status == "malformed"
                    else "Expression is outside the supported restriction projection",
                    str(authored),
                    kind="finding" if status == "malformed" else "unverified",
                    actual=str(node),
                )
            )
        return RestrictionProjection(
            owner,
            authored,
            axiom,
            node,
            prop,
            quantifier,
            filler,
            status,
            chain,
            witness,
        )

    def expressions(
        self,
        owner: URIRef,
        authored: URIRef,
        axiom: Edge,
        chain: tuple[Edge, ...],
        inherited: bool,
    ) -> Iterator[RestrictionProjection | _NamedConjunct]:
        """Walk intersections structurally; union alternatives never become edges."""
        frames: list[tuple[Node, tuple[Edge, ...], tuple[Node, ...]]] = [
            (axiom.object, (), ())
        ]
        while frames:
            self.budget.consume("work")
            node, witness, ancestors = frames.pop()
            if len(ancestors) >= self.budget.limits.max_depth:
                raise LimitExceeded("Restriction expression exceeds max_depth")
            if not isinstance(node, URIRef | BNode) or node in ancestors:
                yield self.record(
                    owner, authored, axiom, node, chain, witness, "malformed"
                )
                continue
            if inherited and ancestors and isinstance(node, URIRef):
                size = len(witness) + 1
                self.budget.consume("work", size)
                self.budget.consume("states", size + 1)
                self.budget.consume("bytes", 128 + 64 * size)
                yield _NamedConjunct(node, (axiom, *witness), len(ancestors))
            facts = self.scoped(node, axiom.scope)
            self.budget.consume("states", len(witness) + len(facts))
            self.budget.consume("bytes", 128 + 64 * (len(witness) + len(facts)))
            structure = (*witness, *facts)
            predicates = {edge.predicate for edge in facts}
            constructors = {
                str(edge.predicate)
                for edge in facts
                if edge.predicate in _EXPRESSION_PREDICATES
            }
            restriction = bool(
                predicates.intersection(_CONSTRAINTS | {OWL.onProperty})
            ) or any(
                edge.predicate == RDF.type and edge.object == OWL.Restriction
                for edge in facts
            )
            intersections = [
                edge for edge in facts if edge.predicate == OWL.intersectionOf
            ]
            if (
                intersections
                and not restriction
                and not set(constructors).difference((str(OWL.intersectionOf),))
            ):
                try:
                    if len(intersections) != 1:
                        raise InputError("Intersection needs one list")
                    members: list[Node] = []
                    list_edges: list[Edge] = []
                    cell = intersections[0].object
                    graph = self.graph(axiom.scope)
                    for member in iter_rdf_list(
                        graph,
                        cell,
                        max_items=self.budget.limits.max_states,
                        budget=self.budget,
                    ):
                        edges = self.scoped(cell, axiom.scope)
                        links = [
                            e for e in edges if e.predicate in (RDF.first, RDF.rest)
                        ]
                        self.budget.consume("states", len(links) + 1)
                        self.budget.consume("bytes", 128 + 64 * (len(links) + 1))
                        list_edges.extend(links)
                        members.append(member)
                        cell = next(e.object for e in links if e.predicate == RDF.rest)
                    if len(members) < 2 or any(
                        not isinstance(member, URIRef | BNode) for member in members
                    ):
                        raise InputError(
                            "Intersection needs at least two resource operands"
                        )
                except LimitExceeded:
                    raise
                except InputError:
                    yield self.record(
                        owner, authored, axiom, node, chain, structure, "malformed"
                    )
                    continue
                self.budget.consume("states", len(structure) + len(list_edges))
                self.budget.consume(
                    "bytes", 128 + 64 * (len(structure) + len(list_edges))
                )
                extended = (*structure, *list_edges)
                for member in reversed(members):
                    self.budget.consume("states", len(ancestors) + 2)
                    self.budget.consume("bytes", 128 + 64 * (len(ancestors) + 1))
                    frames.append((member, extended, (*ancestors, node)))
                continue
            if not restriction and not constructors and isinstance(node, URIRef):
                # A named conjunct contributes no restriction; it is not an error.
                continue
            properties = [
                edge.object for edge in facts if edge.predicate == OWL.onProperty
            ]
            constraints = [edge for edge in facts if edge.predicate in _CONSTRAINTS]
            prop = (
                properties[0]
                if len(properties) == 1 and isinstance(properties[0], URIRef)
                else None
            )
            constraint = constraints[0] if len(constraints) == 1 else None
            quantifier = constraint.predicate if constraint is not None else None
            filler = constraint.object if constraint is not None else None
            malformed = restriction and (
                len(properties) != 1
                or len(constraints) != 1
                or not isinstance(properties[0], URIRef | BNode)
                or quantifier in (OWL.someValuesFrom, OWL.allValuesFrom)
                and not isinstance(filler, URIRef | BNode)
            )
            supported = (
                restriction
                and not malformed
                and prop is not None
                and quantifier in _QUANTIFIERS
                and not set(constructors).difference(_SUPPORTED)
                and (quantifier == OWL.hasValue or isinstance(filler, URIRef))
                and not intersections
            )
            yield self.record(
                owner,
                authored,
                axiom,
                node,
                chain,
                structure,
                "malformed"
                if malformed
                else "supported"
                if supported
                else "unsupported",
                prop,
                quantifier,
                filler,
            )

    def collect(
        self, owner: URIRef, inherited: bool
    ) -> Iterator[RestrictionProjection]:
        """Retain named inheritance witnesses and the actual authored owners."""
        queue: deque[tuple[URIRef, tuple[Edge, ...], int]] = deque([(owner, (), 0)])
        seen = {owner}
        chain: tuple[Edge, ...] = ()

        def enqueue(parent: URIRef, evidence: tuple[Edge, ...], depth: int) -> None:
            """Retain superclass witnesses from actual assertions and lists."""
            if parent in seen:
                return
            if depth > self.budget.limits.max_depth:
                raise LimitExceeded("Named restriction inheritance exceeds max_depth")
            size = len(chain) + len(evidence)
            self.budget.consume("work", size)
            self.budget.consume("states", size + 1)
            self.budget.consume("bytes", 128 + 64 * size)
            seen.add(parent)
            queue.append((parent, (*chain, *evidence), depth))

        while queue:
            current, chain, depth = queue.popleft()
            for axiom in self.edges(current):
                self.budget.consume("work")
                if axiom.predicate not in (RDFS.subClassOf, OWL.equivalentClass):
                    continue
                expressed = True
                if isinstance(axiom.object, URIRef):
                    facts = self.scoped(axiom.object, axiom.scope)
                    expressed = any(
                        edge.predicate in _EXPRESSION_PREDICATES
                        or edge.predicate == RDF.type
                        and edge.object == OWL.Restriction
                        for edge in facts
                    )
                if expressed:
                    for item in self.expressions(
                        owner, current, axiom, chain, inherited
                    ):
                        if isinstance(item, _NamedConjunct):
                            enqueue(item.owner, item.evidence, depth + item.depth)
                        else:
                            yield item
                if (
                    inherited
                    and isinstance(axiom.object, URIRef)
                    and axiom.predicate == RDFS.subClassOf
                ):
                    enqueue(axiom.object, (axiom,), depth + 1)


def project_restrictions(
    view: VocabularyView,
    owner: str,
    *,
    inherited: bool = False,
    budget: Budget | None = None,
) -> Selection[RestrictionProjection]:
    """Inspect some/all/value restrictions and nested intersection attachments.

    Named inheritance follows authored ``rdfs:subClassOf`` links and named
    intersection conjuncts, retaining their actual structural witnesses. Each
    expression and RDF list is interpreted in its attachment's source scope;
    independent sources cannot supply missing components for a named expression.
    Blank fillers and other unsupported constructors remain visible, unverified.
    """
    operation = _operation(view, budget)
    named = _named(owner, operation)
    if type(inherited) is not bool:
        raise InputError("inherited must be a boolean")
    state = _Projection(view, operation)
    output: list[RestrictionProjection] = []
    try:
        for item in state.collect(named, inherited):
            output.append(item)
    except LimitExceeded as error:
        state.issues.exhausted(str(error), location=str(named))
        state.complete = False
    return Selection(
        tuple(output),
        _report("terms.restrictions", len(output), state.issues, state.complete),
    )
