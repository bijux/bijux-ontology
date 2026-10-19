"""Bounded root coverage and selected authored hierarchy topology."""

import json
from collections import deque
from collections.abc import Iterator, Sequence
from typing import Literal, cast

from rdflib import BNode, URIRef
from rdflib.namespace import RDF, SKOS
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.terms import term_key
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.browsing.results import Edge

from ..structure import _RELATIONS
from .scope import _PublicationRead
from .types import ConceptReachability, HierarchyStep, SchemeCoverage, _iri


class _Hierarchy:
    """One bounded normalized assertion store for coverage and induced topology."""

    def __init__(self, read: _PublicationRead) -> None:
        self.read = read
        self.budget = read.budget
        self.edges: dict[tuple[Node, Node], Edge] = {}
        self.incident: dict[Node, tuple[tuple[Node, Node], ...]] = {}

    def concept(self, node: Node) -> bool:
        """Use advertised finite SKOS Concept roles; inScheme alone has no domain."""
        assert isinstance(node, URIRef | BNode)
        selected = self.read._links(
            node,
            tuple(
                map(str, (RDF.type, *_RELATIONS, SKOS.topConceptOf, SKOS.hasTopConcept))
            ),
        )
        for edge in selected.items:
            self.budget.consume("work")
            if (
                edge.subject == node
                and edge.predicate == RDF.type
                and edge.object == SKOS.Concept
            ):
                return True
            if edge.predicate in _RELATIONS:
                return True
            if (
                edge.subject == node
                and edge.predicate == SKOS.topConceptOf
                or edge.object == node
                and edge.predicate == SKOS.hasTopConcept
            ):
                return True
        return False

    def load(self, node: Node) -> tuple[tuple[Node, Node], ...]:
        """Read incident authored edges once, retaining child-to-parent identity."""
        if node not in self.incident:
            assert isinstance(node, URIRef | BNode)
            self.budget.consume("states")
            selected = self.read._links(
                node, tuple(map(str, (SKOS.broader, SKOS.narrower)))
            )
            incident = set()
            for edge in selected.items:
                self.budget.consume("work")
                child, parent = (
                    (edge.subject, edge.object)
                    if edge.predicate == SKOS.broader
                    else (edge.object, edge.subject)
                )
                if not self.read._resource(child, node) or not self.read._resource(
                    parent, node
                ):
                    continue
                pair = child, parent
                if pair not in self.edges:
                    self.budget.consume("states")
                    self.edges[pair] = edge
                self.budget.consume("states")
                incident.add(pair)
            self.incident[node] = tuple(
                sorted(incident, key=lambda pair: (pair[0].n3(), pair[1].n3()))
            )
        return self.incident[node]

    def neighbours(self, node: Node, direction: str) -> Iterator[HierarchyStep]:
        """Orient traversal without replacing the original authored assertion."""
        for child, parent in self.load(node):
            self.budget.consume("work")
            target = None
            if node == parent and direction in ("descendants", "both"):
                target = child
            elif node == child and direction in ("ancestors", "both"):
                target = parent
            if target is not None:
                yield HierarchyStep(node, target, self.edges[child, parent])

    def adjacency(self, concepts: set[Node]) -> dict[Node, tuple[HierarchyStep, ...]]:
        """Share the selected concept-induced directed topology across analyses."""
        self.budget.consume("states", len(concepts))
        result: dict[Node, list[HierarchyStep]] = {node: [] for node in concepts}
        for (child, parent), edge in self.edges.items():
            self.budget.consume("work")
            if child in concepts and parent in concepts:
                self.budget.consume("states")
                result[child].append(HierarchyStep(child, parent, edge))
        return {node: tuple(steps) for node, steps in result.items()}


def _roots(values: Sequence[str], budget: Budget) -> tuple[URIRef, ...]:
    if not isinstance(values, Sequence) or isinstance(
        cast(object, values), (str, bytes)
    ):
        raise InputError("Selected roots require a finite named-IRI sequence")
    count = len(values)
    if count > budget.limits.max_states:
        raise LimitExceeded("Selected roots exceed max_states")
    result = []
    for index in range(count):
        budget.consume("work")
        budget.consume("states")
        value = _iri(values[index])
        size = len(value.encode("utf-8"))
        if size > budget.limits.max_bytes:
            raise LimitExceeded("Selected root exceeds max_bytes")
        budget.consume("bytes", size)
        result.append(URIRef(value))
    if len(set(result)) != len(result):
        raise InputError("Selected roots require unique IRIs")
    return tuple(result)


def _walk(
    hierarchy: _Hierarchy,
    roots: set[Node],
    concepts: set[Node],
    direction: str,
    cross_scheme: str,
    witnesses: list[ConceptReachability],
) -> None:
    queue: deque[tuple[Node, Node, tuple[HierarchyStep, ...]]] = deque()
    seen: set[Node] = set()
    for root in sorted(roots, key=lambda node: node.n3()):
        hierarchy.budget.consume("states", 2)
        seen.add(root)
        queue.append((root, root, ()))
        if root in concepts:
            witnesses.append(ConceptReachability(root, root, ()))
    while queue:
        current, root, path = queue.popleft()
        for step in hierarchy.neighbours(current, direction):
            hierarchy.budget.consume("work")
            if (
                step.target in seen
                or cross_scheme == "exclude"
                and step.target not in concepts
            ):
                continue
            if len(path) >= hierarchy.budget.limits.max_depth:
                raise LimitExceeded("Root coverage exceeded max_depth")
            length = len(path) + 1
            hierarchy.budget.consume("work", length)
            hierarchy.budget.consume("states", length + 2)
            hierarchy.budget.consume(
                "bytes",
                sum(
                    len(item.source.n3().encode("utf-8"))
                    + len(item.target.n3().encode("utf-8"))
                    for item in path
                )
                + len(step.source.n3().encode("utf-8"))
                + len(step.target.n3().encode("utf-8")),
            )
            selected = (*path, step)
            seen.add(step.target)
            queue.append((step.target, root, selected))
            if step.target in concepts:
                witnesses.append(ConceptReachability(step.target, root, selected))


def _components(
    adjacency: dict[Node, tuple[HierarchyStep, ...]],
    remaining: set[Node],
    budget: Budget,
) -> tuple[tuple[Node, ...], ...]:
    budget.consume("states", len(remaining))
    neighbours: dict[Node, set[Node]] = {node: set() for node in remaining}
    for node, steps in adjacency.items():
        for step in steps:
            budget.consume("work")
            if node in remaining and step.target in remaining:
                budget.consume("states", 2)
                neighbours[node].add(step.target)
                neighbours[step.target].add(node)
    pending = set(remaining)
    result = []
    for seed in sorted(remaining, key=lambda node: node.n3()):
        budget.consume("work")
        if seed not in pending:
            continue
        pending.remove(seed)
        queue = [seed]
        component = []
        while queue:
            node = queue.pop()
            budget.consume("work")
            budget.consume("states")
            component.append(node)
            for target in neighbours[node]:
                budget.consume("work")
                if target in pending:
                    pending.remove(target)
                    queue.append(target)
        result.append(tuple(sorted(component, key=lambda node: node.n3())))
    return tuple(result)


def _cycles(
    adjacency: dict[Node, tuple[HierarchyStep, ...]],
    read: _PublicationRead,
    output: list[tuple[HierarchyStep, ...]],
) -> None:
    colours: dict[Node, int] = {}
    parents: dict[Node, HierarchyStep] = {}
    for seed in sorted(adjacency, key=lambda node: node.n3()):
        if seed in colours:
            continue
        read.budget.consume("states")
        colours[seed] = 1
        frames = [(seed, iter(adjacency[seed]))]
        while frames:
            node, iterator = frames[-1]
            step = next(iterator, None)
            if step is None:
                colours[node] = 2
                frames.pop()
                continue
            read.budget.consume("work")
            if colours.get(step.target, 0) == 0:
                read.budget.consume("states", 2)
                colours[step.target] = 1
                parents[step.target] = step
                frames.append((step.target, iter(adjacency[step.target])))
            elif colours[step.target] == 1:
                path = []
                cursor = node
                while cursor != step.target:
                    read.budget.consume("work")
                    read.budget.consume("states")
                    previous = parents[cursor]
                    path.append(previous)
                    cursor = previous.source
                read.budget.consume("states")
                witness = (*reversed(path), step)
                evidence = json.dumps(
                    {
                        "nodes": [term_key(item.source) for item in witness]
                        + [term_key(step.target)]
                    }
                )
                read.budget.consume("bytes", len(evidence.encode("utf-8")))
                output.append(witness)
                read.issues.append(
                    Issue(
                        "skos.publication.hierarchy_cycle",
                        "Selected publication topology forbids this "
                        "authored directed cycle",
                        str(step.target),
                        evidence_json=evidence,
                    )
                )


def check_scheme_coverage(
    view: VocabularyView,
    scheme: str,
    *,
    roots: Sequence[str] | None = None,
    direction: Literal["descendants", "ancestors", "both"] = "descendants",
    cross_scheme: Literal["exclude", "include"] = "exclude",
    cycles: Literal["allow", "forbid"] = "allow",
    budget: Budget | None = None,
) -> SchemeCoverage:
    """Cover selected scheme concepts from actual published roots, without inference.

    Scope comprises scheme members with asserted/finite SKOS Concept roles;
    inScheme alone does not classify labels or other resources as concepts.
    Authored broader/narrower inverse links supply all traversal witnesses.
    Cross-scheme inclusion permits supplied outside bridges; only selected-scheme
    concepts are obligations. Components/cycle policy use the selected concept-
    induced topology, with directed child-to-parent cycles independent of walk
    direction. Default cycles are allowed and make no logical-validity claim.
    Incomplete traversal retains reached proofs and publishes no orphan inventory.
    """
    if not isinstance(view, VocabularyView):
        raise InputError("Expected an owned VocabularyView")
    _iri(scheme)
    if (
        direction not in ("descendants", "ancestors", "both")
        or cross_scheme not in ("exclude", "include")
        or cycles not in ("allow", "forbid")
    ):
        raise InputError("Select explicit supported coverage policies")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Expected a shared Budget")
    operation = budget if budget is not None else Budget(view.limits)
    read = _PublicationRead(view, operation)
    hierarchy = _Hierarchy(read)
    selected_scheme = URIRef(scheme)
    concepts: set[Node] = set()
    selected_roots: set[Node] = set()
    witnesses: list[ConceptReachability] = []
    cycle_witnesses: list[tuple[HierarchyStep, ...]] = []
    unreachable = None
    finished = False
    try:
        size = len(scheme.encode("utf-8"))
        if size > operation.limits.max_bytes:
            raise LimitExceeded("Scheme IRI exceeds max_bytes")
        operation.consume("bytes", size)
        captured = _roots(roots, operation) if roots is not None else None
        scope = read.scheme(selected_scheme)
        selected_roots = set(
            scope.roots if captured is None else scope.roots.intersection(captured)
        )
        if captured is not None:
            for root in captured:
                operation.consume("work")
                if root not in scope.roots:
                    read.issues.append(
                        Issue(
                            "skos.publication.root_not_published"
                            if scope.complete
                            else "skos.publication.root_scope_unverified",
                            "Selected root is not published by this scheme"
                            if scope.complete
                            else "Incomplete scheme inventory cannot establish "
                            "this selected root",
                            str(root),
                            kind="finding" if scope.complete else "unverified",
                        )
                    )
        for member in sorted(scope.members, key=lambda node: node.n3()):
            operation.consume("work")
            if hierarchy.concept(member):
                operation.consume("states")
                concepts.add(member)
        _walk(hierarchy, selected_roots, concepts, direction, cross_scheme, witnesses)
        for concept in sorted(concepts, key=lambda node: node.n3()):
            hierarchy.load(concept)
        if read.complete:
            adjacency = hierarchy.adjacency(concepts)
            components = _components(
                adjacency, concepts - {item.concept for item in witnesses}, operation
            )
            for component in components:
                evidence = json.dumps(
                    {
                        "scheme": term_key(selected_scheme),
                        "members": [term_key(node) for node in component],
                    }
                )
                operation.consume("bytes", len(evidence.encode("utf-8")))
                read.issues.append(
                    Issue(
                        "skos.publication.unreachable_component",
                        "Selected scheme concepts are unreachable from "
                        "the chosen published roots",
                        str(component[0]),
                        evidence_json=evidence,
                    )
                )
            if cycles == "forbid":
                _cycles(adjacency, read, cycle_witnesses)
            unreachable = components
            finished = True
        if not concepts and read.complete:
            unreachable = None
            finished = False
            read.issues.append(
                Issue(
                    "skos.publication.scope_unverified",
                    "No selected asserted concept scope completed",
                    scheme,
                    kind="unverified",
                )
            )
    except LimitExceeded as error:
        read.complete = False
        read.issues.exhausted(str(error), location=scheme)
    complete = read.complete and finished and bool(concepts)
    checked = len(concepts) if complete else len(witnesses)
    report = Report(
        "skos.scheme_coverage",
        checked,
        tuple(read.issues),
        requested=max(len(concepts), checked if complete else checked + 1),
        complete=complete,
        families=("skos.publication",),
    )
    return SchemeCoverage(
        selected_scheme,
        tuple(sorted(concepts, key=lambda node: node.n3())),
        tuple(sorted(selected_roots, key=lambda node: node.n3())),
        tuple(witnesses),
        unreachable,
        tuple(cycle_witnesses) if cycles == "forbid" else None,
        report,
    )
