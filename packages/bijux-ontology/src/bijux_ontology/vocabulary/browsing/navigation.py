"""Cycle-safe navigation over chosen asserted relationships, without inference."""

from collections import deque
from collections.abc import Generator, Sequence
from dataclasses import KW_ONLY, dataclass
from typing import Literal

from rdflib import BNode, URIRef
from rdflib.namespace import RDFS, SKOS
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.limits import Budget, IssueCollector
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.structure import iter_rdf_list
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.browsing.results import Edge, Selection


@dataclass(frozen=True)
class Visit:
    """One reachable named resource and its first shortest asserted edge."""

    iri: str
    depth: int
    via: Edge | None


@dataclass(frozen=True)
class Path:
    """Asserted connection evidence with unreachable versus incomplete explicit."""

    status: Literal["found", "unreachable", "incomplete"]
    edges: tuple[Edge, ...]
    report: Report


@dataclass(frozen=True)
class Topology:
    """Roots/leaves, weak components and cyclic strongly connected components."""

    roots: tuple[str, ...]
    leaves: tuple[str, ...]
    components: tuple[tuple[str, ...], ...]
    cycles: tuple[tuple[str, ...], ...]
    report: Report


@dataclass(frozen=True)
class Membership:
    """A concept/collection relation with its original assertion retained."""

    member: Node
    assertion: Edge
    implied: bool = False
    _: KW_ONLY
    position: int | None = None


def _depth(view: VocabularyView, maximum: int | None) -> int:
    selected = view.limits.max_depth if maximum is None else maximum
    if type(selected) is not int or selected < 0:
        raise InputError("Navigation depth must be a nonnegative integer")
    return selected


def _report(name: str, checked: int, issues: IssueCollector, complete: bool) -> Report:
    return Report(
        name,
        checked,
        tuple(issues),
        allow_empty=True,
        requested=checked if complete else checked + 1,
        complete=complete,
        regime="asserted",
        families=(name,),
    )


def neighbours(
    view: VocabularyView,
    iri: str,
    *,
    predicates: Sequence[str] = (str(RDFS.subClassOf),),
    direction: Literal["outgoing", "incoming"] = "outgoing",
) -> Selection[Edge]:
    """Return direct assertions without conflating class/property/SKOS hierarchies."""
    return view.relationships(iri, predicates=predicates, direction=direction)


def _walk(
    view: VocabularyView,
    iri: str,
    *,
    predicates: Sequence[str] = (str(RDFS.subClassOf),),
    direction: Literal["outgoing", "incoming"] = "outgoing",
    include_self: bool = False,
    max_depth: int | None = None,
    stop_at: str | None = None,
    budget: Budget | None = None,
) -> Selection[Visit]:
    """Traverse named asserted edges breadth-first, preserving finite incompleteness."""
    absolute_iri(iri)
    if direction not in ("outgoing", "incoming"):
        raise InputError("Navigation direction must be outgoing or incoming")
    for predicate in predicates:
        absolute_iri(predicate)
    if type(include_self) is not bool:
        raise InputError("include_self must be a boolean")
    maximum = _depth(view, max_depth)
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Navigation budget requires a Budget")
    if budget is not None:
        maximum = min(maximum, budget.limits.max_depth)
    budget = budget if budget is not None else Budget(view.limits)
    issues = IssueCollector(budget)
    items: list[Visit] = [Visit(iri, 0, None)] if include_self else []
    queue = deque([(iri, 0)])
    seen = {iri}
    complete = True
    try:
        while queue:
            if stop_at in seen:
                break
            current, depth = queue.popleft()
            selected = view.relationships(
                current, predicates=predicates, direction=direction, budget=budget
            )
            # Child findings already consumed this shared issue allowance.
            list.extend(issues, selected.report.issues)
            if not selected.report.complete:
                complete = False
                break
            for edge in selected.items:
                budget.consume("work")
                target = edge.object if direction == "outgoing" else edge.subject
                if not isinstance(target, URIRef):
                    issues.append(
                        Issue(
                            "terms.navigation_anonymous",
                            "Named traversal does not interpret this endpoint",
                            current,
                            kind="unverified",
                        )
                    )
                    complete = False
                    continue
                if str(target) in seen:
                    continue
                if depth >= maximum:
                    issues.exhausted(
                        "Navigation exceeded selected max_depth", location=current
                    )
                    complete = False
                    continue
                seen.add(str(target))
                budget.consume("states")
                items.append(Visit(str(target), depth + 1, edge))
                queue.append((str(target), depth + 1))
                if str(target) == stop_at:
                    break
    except LimitExceeded as error:
        issues.exhausted(str(error), location=iri)
        complete = False
    return Selection(
        tuple(items), _report("terms.traversal", len(items), issues, complete)
    )


def traverse(
    view: VocabularyView,
    iri: str,
    *,
    predicates: Sequence[str] = (str(RDFS.subClassOf),),
    direction: Literal["outgoing", "incoming"] = "outgoing",
    include_self: bool = False,
    max_depth: int | None = None,
    budget: Budget | None = None,
) -> Selection[Visit]:
    """Visit named resources with asserted evidence and an optional budget."""
    return _walk(
        view,
        iri,
        predicates=predicates,
        direction=direction,
        include_self=include_self,
        max_depth=max_depth,
        budget=budget,
    )


def shortest_path(
    view: VocabularyView,
    source: str,
    target: str,
    *,
    predicates: Sequence[str] = (str(RDFS.subClassOf),),
    direction: Literal["outgoing", "incoming"] = "outgoing",
    max_depth: int | None = None,
) -> Path:
    """Find an asserted shortest path; it is evidence, never an OWL proof."""
    absolute_iri(source)
    absolute_iri(target)
    selected = _walk(
        view,
        source,
        predicates=predicates,
        direction=direction,
        include_self=True,
        max_depth=max_depth,
        stop_at=target,
    )
    previous = {visit.iri: visit for visit in selected.items}
    if target not in previous:
        return Path(
            "unreachable" if selected.report.complete else "incomplete",
            (),
            selected.report,
        )
    edges: list[Edge] = []
    current = target
    while current != source:
        edge = previous[current].via
        if edge is None:
            raise InputError("Navigation result lacks its asserted path")
        edges.append(edge)
        current = str(edge.subject if direction == "outgoing" else edge.object)
    return Path(
        "found",
        tuple(reversed(edges)),
        Report(
            "terms.path",
            max(1, len(edges)),
            regime="asserted-named-edges",
            families=("terms.path",),
        ),
    )


def topology(
    view: VocabularyView,
    *,
    predicates: Sequence[str] = (str(RDFS.subClassOf),),
    kinds: Sequence[str] = (),
) -> Topology:
    """Inspect a selected directed relation graph without forbidding legal cycles."""
    budget = Budget(view.limits)
    issues = IssueCollector(budget)
    complete = True
    outgoing: dict[str, set[str]] = {}
    incoming: dict[str, set[str]] = {}
    components: list[tuple[str, ...]] = []
    cycles: list[tuple[str, ...]] = []
    try:
        inventory = view.terms(kinds=kinds, budget=budget)
        list.extend(issues, inventory.report.issues)
        complete = inventory.report.complete
        nodes = {term.iri for term in inventory.items}
        outgoing = {node: set() for node in nodes}
        incoming = {node: set() for node in nodes}
        for node in sorted(nodes):
            selected = view.relationships(node, predicates=predicates, budget=budget)
            list.extend(issues, selected.report.issues)
            complete = complete and selected.report.complete
            for edge in selected.items:
                budget.consume("work")
                if isinstance(edge.object, URIRef) and str(edge.object) in nodes:
                    outgoing[node].add(str(edge.object))
                    incoming[str(edge.object)].add(node)
        # Weak components and bounded reachability are inspection, not validity rules.
        remaining = set(nodes)
        while remaining:
            start = min(remaining)
            reached = {start}
            queue = deque([start])
            while queue:
                node = queue.popleft()
                budget.consume("work")
                for linked in outgoing[node] | incoming[node]:
                    budget.consume("work")
                    if linked not in reached:
                        reached.add(linked)
                        queue.append(linked)
            remaining.difference_update(reached)
            components.append(tuple(sorted(reached)))
        # Iterative Kosaraju retains long chains without Python recursion.
        seen: set[str] = set()
        order: list[str] = []
        for start in sorted(nodes):
            if start in seen:
                continue
            stack = [(start, False)]
            while stack:
                node, finishing = stack.pop()
                budget.consume("work")
                if finishing:
                    order.append(node)
                elif node not in seen:
                    seen.add(node)
                    stack.append((node, True))
                    stack.extend(
                        (linked, False)
                        for linked in sorted(outgoing[node], reverse=True)
                        if linked not in seen
                    )
        seen.clear()
        for start in reversed(order):
            if start in seen:
                continue
            group: set[str] = set()
            stack_nodes = [start]
            while stack_nodes:
                node = stack_nodes.pop()
                budget.consume("work")
                if node not in seen:
                    seen.add(node)
                    group.add(node)
                    stack_nodes.extend(incoming[node] - seen)
            if len(group) > 1 or start in outgoing[start]:
                cycles.append(tuple(sorted(group)))
    except LimitExceeded as error:
        issues.exhausted(str(error), location="terms.topology")
        complete = False
    return Topology(
        tuple(sorted(node for node in outgoing if not outgoing[node])),
        tuple(sorted(node for node in incoming if not incoming[node])),
        tuple(sorted(components)),
        tuple(sorted(cycles)),
        _report("terms.topology", len(outgoing), issues, complete),
    )


def scheme_members(
    view: VocabularyView,
    scheme: str,
    *,
    top_only: bool = False,
    budget: Budget | None = None,
) -> Selection[Membership]:
    """Normalise top-concept inverses, preserving original membership assertions."""
    absolute_iri(scheme)
    budget = budget if budget is not None else Budget(view.limits)
    issues = IssueCollector(budget)
    selected = view.relationships(
        scheme,
        predicates=(
            str(SKOS.inScheme),
            str(SKOS.topConceptOf),
            str(SKOS.hasTopConcept),
        ),
        direction="both",
        budget=budget,
    )
    items: list[Membership] = []
    complete = selected.report.complete
    try:
        list.extend(issues, selected.report.issues)
        for edge in selected.items:
            budget.consume("work")
            if edge.subject == URIRef(scheme) and edge.predicate == SKOS.hasTopConcept:
                items.append(Membership(edge.object, edge, not top_only))
            elif edge.object == URIRef(scheme) and edge.predicate in (
                (SKOS.topConceptOf,) if top_only else (SKOS.inScheme, SKOS.topConceptOf)
            ):
                items.append(
                    Membership(
                        edge.subject,
                        edge,
                        edge.predicate == SKOS.topConceptOf and not top_only,
                    )
                )
    except LimitExceeded as error:
        issues.exhausted(str(error), location=scheme)
        complete = False
    return Selection(
        tuple(items), _report("terms.scheme", len(items), issues, complete)
    )


def collection_members(
    view: VocabularyView,
    collection: str,
    *,
    ordered: bool = False,
    budget: Budget | None = None,
) -> Selection[Membership]:
    """Expose direct members or list positions; direct assertions have no position."""
    operation = budget if budget is not None else Budget(view.limits)
    if not ordered:
        selected = view.relationships(
            collection, predicates=(str(SKOS.member),), budget=operation
        )
        return Selection(
            tuple(Membership(edge.object, edge) for edge in selected.items),
            selected.report,
        )
    issues = IssueCollector(operation)
    items = tuple(_ordered_collection_members(view, collection, operation, issues))
    return Selection(items, _report("terms.collection", len(items), issues, not issues))


def _ordered_collection_members(
    view: VocabularyView,
    collection: str | BNode,
    budget: Budget,
    issues: IssueCollector,
) -> Generator[Membership, None, None]:
    """Share proved list positions between navigation and streaming constraints."""
    selected = view.statements(
        collection if isinstance(collection, BNode) else URIRef(collection),
        predicates=(str(SKOS.memberList),),
        budget=budget,
    )
    list.extend(issues, selected.report.issues)
    if not selected.report.complete:
        return
    try:
        if len(selected.items) != 1:
            issues.append(
                Issue(
                    "terms.collection_list_ambiguous",
                    "Ordered view needs exactly one supplied member list",
                    str(collection),
                    kind="unverified",
                )
            )
            return
        edge = selected.items[0]
        for position, member in enumerate(
            iter_rdf_list(
                view._read(),
                edge.object,
                max_items=budget.limits.max_work,
                budget=budget,
            )
        ):
            budget.consume("states")
            yield Membership(member, edge, True, position=position)
    except LimitExceeded as error:
        issues.exhausted(str(error), location=str(collection))
    except InputError as error:
        try:
            issues.append(
                Issue("terms.collection_list_malformed", str(error), str(collection))
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted), location=str(collection))
