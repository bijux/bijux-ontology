"""Owned named-class closure and compact asserted disjointness contracts."""

from collections.abc import Iterator, Sequence
from heapq import merge
from itertools import combinations

from rdflib import BNode, Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget
from bijux_ontology.owl.encoding.constructs import unsupported_constructs
from bijux_ontology.rdf.structure import iter_rdf_list


class ClassContracts:
    """Cache finite named closure within one immutable graph checking operation."""

    def __init__(self, graph: Graph, budget: Budget) -> None:
        self.graph = graph
        self.budget = budget
        self._closures: dict[str, tuple[frozenset[str], bool]] = {}
        self._pairs: set[tuple[str, ...]] = set()
        self._groups: list[frozenset[str]] = []
        self._memberships: dict[str, list[int]] = {}

    def _closure(self, root: str) -> tuple[frozenset[str], bool]:
        self.budget.consume("work")
        cached = self._closures.get(root)
        if cached is not None:
            return cached
        pending = [root]
        seen: set[str] = set()
        anonymous = False
        while pending:
            self.budget.consume("work")
            current = pending.pop()
            if current in seen:
                continue
            cached = self._closures.get(current)
            if cached is not None:
                self.budget.consume("work", len(cached[0]))
                seen.update(cached[0])
                anonymous |= cached[1]
                continue
            seen.add(current)
            node = URIRef(current)
            anonymous |= bool(unsupported_constructs(self.graph, node))
            neighbours = (
                *self.graph.objects(node, RDFS.subClassOf),
                *self.graph.objects(node, OWL.equivalentClass),
                *self.graph.subjects(OWL.equivalentClass, node),
            )
            for neighbour in neighbours:
                self.budget.consume("work")
                if isinstance(neighbour, URIRef):
                    pending.append(str(neighbour))
                elif isinstance(neighbour, BNode):
                    anonymous = True
        # Cache only complete closures; retained memberships have a shared bound.
        self.budget.consume("states", len(seen) + 1)
        result = frozenset(seen), anonymous
        self._closures[root] = result
        return result

    def ancestors(self, types: Sequence[str]) -> tuple[frozenset[str], bool]:
        """Follow explicit subclass and bidirectional equivalence, never OWL rules."""
        self.budget.consume("work", len(types))
        roots = tuple(dict.fromkeys(types))
        if len(roots) == 1:
            return self._closure(roots[0])
        seen: set[str] = set()
        anonymous = False
        for root in roots:
            closure, unsupported = self._closure(root)
            self.budget.consume("work", len(closure))
            seen.update(closure)
            anonymous |= unsupported
        return frozenset(seen), anonymous

    def _named_members(
        self, head: Node, wanted: dict[URIRef, str]
    ) -> tuple[str, ...] | None:
        """Validate the entire list while retaining only relevant member evidence."""
        selected: list[str] = []
        occurrences: dict[URIRef, int] = {}
        try:
            for member in iter_rdf_list(
                self.graph,
                head,
                max_items=self.budget.limits.max_work,
                budget=self.budget,
                constant_space=True,
            ):
                self.budget.consume("work")
                if not isinstance(member, URIRef):
                    return None
                # Two occurrences establish self-disjointness; retaining further
                # repeats cannot add a distinct selected class obligation.
                if member in wanted and occurrences.get(member, 0) < 2:
                    self.budget.consume("states", 2)
                    self.budget.consume("bytes", 64)
                    selected.append(wanted[member])
                    occurrences[member] = occurrences.get(member, 0) + 1
        except LimitExceeded:
            raise
        except InputError:
            return None
        return tuple(selected)

    def _mentioned(self, heads: Sequence[Node], wanted: dict[URIRef, str]) -> set[str]:
        """Bound malformed branches while retaining only relevant mentions."""
        self.budget.consume("states", len(heads))
        self.budget.consume("bytes", 64 + 16 * len(heads))
        pending = list(heads)
        visited: set[Node] = set()
        mentioned: set[str] = set()
        while pending:
            self.budget.consume("work")
            cell = pending.pop()
            if cell in visited:
                continue
            self.budget.consume("states")
            self.budget.consume("bytes", 64)
            visited.add(cell)
            for item in self.graph.objects(cell, RDF.first):
                self.budget.consume("work")
                if (
                    isinstance(item, URIRef)
                    and item in wanted
                    and wanted[item] not in mentioned
                ):
                    self.budget.consume("states")
                    mentioned.add(wanted[item])
            for tail in self.graph.objects(cell, RDF.rest):
                self.budget.consume("work")
                self.budget.consume("states")
                self.budget.consume("bytes", 16)
                pending.append(tail)
        return mentioned

    def prepare_disjoint(self, relevant: set[str]) -> tuple[Node, ...]:
        """Prepare reachable groups and return relevant unsupported group nodes."""
        wanted: dict[URIRef, str] | None = None
        unsupported: list[Node] = []
        for left, right in self.graph.subject_objects(OWL.disjointWith):
            self.budget.consume("work")
            if (
                isinstance(left, URIRef)
                and isinstance(right, URIRef)
                and str(left) in relevant
                and str(right) in relevant
            ):
                pair = tuple(sorted({str(left), str(right)}))
                if pair not in self._pairs:
                    self.budget.consume("states")
                    self._pairs.add(pair)
        for group in self.graph.subjects(RDF.type, OWL.AllDisjointClasses):
            self.budget.consume("work")
            if wanted is None:
                self.budget.consume("states", len(relevant))
                size = sum(map(len, relevant))
                self.budget.consume("work", len(relevant) + size)
                self.budget.consume("bytes", 64 + 64 * len(relevant) + 4 * size)
                wanted = {URIRef(name): name for name in relevant}
            heads = []
            for head in self.graph.objects(group, OWL.members):
                self.budget.consume("work")
                self.budget.consume("states")
                self.budget.consume("bytes", 16)
                heads.append(head)
            members = self._named_members(heads[0], wanted) if len(heads) == 1 else None
            if members is None:
                if self._mentioned(heads, wanted):
                    unsupported.append(group)
                continue
            selected: set[str] = set()
            for member in members:
                self.budget.consume("work")
                if member in relevant:
                    if member in selected and (member,) not in self._pairs:
                        self.budget.consume("states")
                        self._pairs.add((member,))
                    selected.add(member)
            if len(selected) < 2:
                continue
            self.budget.consume("states", 2 * len(selected) + 1)
            index = len(self._groups)
            self._groups.append(frozenset(selected))
            for member in selected:
                self._memberships.setdefault(member, []).append(index)
        return tuple(unsupported)

    def conflicts(self, types: Sequence[str]) -> Iterator[str]:
        """Generate only actual conflicts, without an all-pairs ontology index."""
        ancestors, _ = self.ancestors(types)
        selected_pairs = []
        for pair in self._pairs:
            self.budget.consume("work")
            if all(member in ancestors for member in pair):
                selected_pairs.append(pair)
        streams: list[Iterator[tuple[str, ...]]] = [iter(sorted(selected_pairs))]
        groups: set[int] = set()
        for ancestor in ancestors if self._memberships else ():
            self.budget.consume("work")
            memberships = self._memberships.get(ancestor, ())
            self.budget.consume("work", len(memberships))
            groups.update(memberships)
        for index in groups:
            members = self._groups[index]
            self.budget.consume("work", min(len(members), len(ancestors)))
            simultaneous = sorted(members.intersection(ancestors))
            if len(simultaneous) > 1:
                streams.append(combinations(simultaneous, 2))
        previous = None
        count = 0
        for pair in merge(*streams):
            self.budget.consume("work")
            if pair == previous:
                continue
            if count >= self.budget.limits.max_issues:
                raise LimitExceeded("Disjoint conflicts exceed max_issues")
            count += 1
            previous = pair
            yield ", ".join(pair)
        if str(OWL.Nothing) in ancestors:
            yield str(OWL.Nothing)
