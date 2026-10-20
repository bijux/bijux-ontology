"""Finite typed relation paths over selected asserted vocabulary evidence."""

from collections import deque
from collections.abc import Iterable, Sequence

from rdflib import URIRef

from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.inputs import capture
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.browsing.results import Selection
from bijux_ontology.vocabulary.restrictions.types import RestrictionProjection

from .automaton import Automaton, named
from .edges import _Inventory
from .types import (
    RelationEndpoint,
    RelationPathResult,
    RelationSpec,
    RelationWitness,
)


def _endpoint(
    endpoint: RelationEndpoint, budget: Budget, inventory: _Inventory
) -> tuple[frozenset[URIRef], frozenset[str], frozenset[URIRef] | None]:
    """Own finite endpoint conditions and require supplied membership coverage."""
    if not isinstance(endpoint, RelationEndpoint):
        raise InputError("Expected a typed relation endpoint")
    groups = []
    for values in (endpoint.identifiers, endpoint.declared_kinds):
        if type(values) is not tuple:
            raise InputError("Endpoint conditions require tuples")
        budget.consume("states", len(values))
        budget.consume("bytes", 64 * len(values))
        groups.append(frozenset(named(value, budget) for value in values))
    allowed = None
    if endpoint.enumeration is not None:
        if not isinstance(endpoint.enumeration, ValueEnumeration):
            raise InputError("Endpoint membership requires ValueEnumeration")
        inventory.reserve()
        inventory.adopt(endpoint.enumeration.qualification_report(), owned=False)
        if not endpoint.enumeration.qualified:
            inventory.complete = False
        budget.consume("states", len(endpoint.enumeration.values))
        budget.consume("bytes", 64 * len(endpoint.enumeration.values))
        members = []
        for term in endpoint.enumeration.values:
            bound_node(term, budget)
            if isinstance(term, URIRef):
                members.append(term)
        allowed = frozenset(members)
    if not any(groups) and allowed is None:
        raise InputError("Select endpoint identities, kinds or enumeration")
    return groups[0], frozenset(map(str, groups[1])), allowed


def _matches(
    view: VocabularyView,
    node: URIRef,
    conditions: tuple[frozenset[URIRef], frozenset[str], frozenset[URIRef] | None],
    budget: Budget,
) -> bool:
    """Check only selected exact names and actual asserted declaration kinds."""
    identifiers, kinds, allowed = conditions
    budget.consume("work")
    if (
        identifiers
        and node not in identifiers
        or allowed is not None
        and node not in allowed
    ):
        return False
    if kinds:
        actual = view.lookup(str(node)).kinds
        for kind in actual:
            bound_text(kind, budget)
        budget.consume("work", len(actual))
        return bool(kinds.intersection(actual))
    return True


def _run(
    view: VocabularyView,
    source: str,
    path: RelationSpec,
    endpoint: RelationEndpoint,
    projections: Selection[RestrictionProjection] | None,
    maximum: int | None,
    budget: Budget,
    require_match: bool,
) -> RelationPathResult:
    """Search the finite node/automaton product, preserving shortest edge depth."""
    issues = IssueCollector(budget)
    children: list[Report] = []
    inventory = _Inventory(view, budget, issues, children)
    start = goal = None
    witness: tuple[RelationWitness, ...] = ()
    complete = False
    found = False
    checked = 0
    try:
        start = named(source, budget)
        depth_limit = budget.limits.max_depth if maximum is None else maximum
        if type(depth_limit) is not int or depth_limit < 0:
            raise InputError("Path depth requires a nonnegative integer")
        depth_limit = min(depth_limit, budget.limits.max_depth)
        automaton = Automaton(path, budget)
        conditions = _endpoint(endpoint, budget, inventory)
        if projections is not None:
            inventory.supplied_view(projections)
        if view.lookup(str(start)).status == "absent":
            issues.append(
                Issue(
                    "terms.relation_source_unverified",
                    "Path source is absent from the selected vocabulary",
                    kind="unverified",
                )
            )
            inventory.complete = False
        else:
            budget.consume("states", 2)
            budget.consume("bytes", 192)
            queue: deque[tuple[URIRef, int, tuple[RelationWitness, ...]]] = deque(
                [(start, automaton.start, ())]
            )
            best = {(start, automaton.start): 0}
            truncated = False
            while queue and not found:
                node, state, current = queue.popleft()
                budget.consume("work")
                if best[node, state] != len(current):
                    continue
                if state == automaton.end and _matches(view, node, conditions, budget):
                    goal, witness, found = node, current, True
                    break
                for arc in automaton.arcs[state]:
                    budget.consume("work")
                    if arc.step is None:
                        following: Iterable[tuple[URIRef, RelationWitness | None]] = (
                            (node, None),
                        )
                    else:
                        following = (
                            (edge.target, edge)
                            for edge in inventory.neighbours(
                                node, arc.step, arc.inverse
                            )
                        )
                    for target, edge in following:
                        budget.consume("work")
                        key = target, arc.target
                        distance = len(current) + (edge is not None)
                        if key in best and best[key] <= distance:
                            continue
                        if distance > depth_limit:
                            truncated = True
                            continue
                        budget.consume("states", 2 + distance)
                        budget.consume("bytes", 192 + 64 * distance)
                        budget.consume("work", distance)
                        steps = current if edge is None else (*current, edge)
                        best[key] = distance
                        if arc.step is None:
                            queue.appendleft((target, arc.target, steps))
                        else:
                            queue.append((target, arc.target, steps))
            if truncated and not found:
                issues.exhausted("Typed relation path exceeded selected max_depth")
                inventory.complete = False
            complete = inventory.complete
            checked = 1
            if complete and not found and require_match:
                issues.append(
                    Issue(
                        "terms.relation_path_missing",
                        "No selected typed relation path satisfies the endpoint",
                    )
                )
    except (LimitExceeded, MemoryError, RecursionError):
        complete = False
        issues.exhausted("Typed relation path exceeded its operation allowance")
    except InputError:
        complete = False
        try:
            issues.append(
                Issue(
                    "terms.relation_path_invalid",
                    "Relation paths require typed terms, roles and finite conditions",
                    kind="input_error",
                )
            )
        except LimitExceeded:
            issues.exhausted("Relation path diagnostic allowance exhausted")
    report = Report(
        "terms.relation_path",
        checked,
        tuple(issues),
        requested=1,
        complete=complete,
        children=tuple(children),
        regime="asserted-typed-relation-path",
    )
    return RelationPathResult(
        "found" if complete and found else "nonmatch" if complete else "incomplete",
        start,
        goal,
        witness,
        report,
    )


def check_relation_path(
    view: VocabularyView,
    source: str,
    path: RelationSpec,
    *,
    endpoint: RelationEndpoint,
    projections: Selection[RestrictionProjection] | None = None,
    max_depth: int | None = None,
    budget: Budget | None = None,
) -> RelationPathResult:
    """Require a path in selected asserted roles, not a logical entailment.

    Class and SKOS steps read only their explicitly named asserted hierarchy
    predicates. Existential steps reuse original someValuesFrom projections;
    universal/hasValue restrictions and raw instance triples never substitute.
    Inverse reverses navigation only. Missing source IRIs cannot satisfy zero
    repetition. Caller projection selections retain their required coverage and
    must requalify against actual original records in this same selected view.
    Unknown projection scope or depth/work truncation never proves nonmatch.
    """
    if (
        not isinstance(view, VocabularyView)
        or budget is not None
        and not isinstance(budget, Budget)
    ):
        raise InputError("Expected VocabularyView and optional shared Budget")
    operation = budget if budget is not None else Budget(view.limits)
    return _run(view, source, path, endpoint, projections, max_depth, operation, True)


def find_relation_path(
    view: VocabularyView,
    source: str,
    path: RelationSpec,
    *,
    endpoint: RelationEndpoint,
    projections: Selection[RestrictionProjection] | None = None,
    max_depth: int | None = None,
    budget: Budget | None = None,
) -> RelationPathResult:
    """Observe a selected path without requiring a matching endpoint.

    A complete nonmatch verifies search coverage and retains status ``nonmatch``;
    it does not establish a path. Required contract callers use
    ``check_relation_path``. Unknown scope and truncation remain incomplete.
    """
    if (
        not isinstance(view, VocabularyView)
        or budget is not None
        and not isinstance(budget, Budget)
    ):
        raise InputError("Expected VocabularyView and optional shared Budget")
    operation = budget if budget is not None else Budget(view.limits)
    return _run(view, source, path, endpoint, projections, max_depth, operation, False)


def relation_enumeration(
    view: VocabularyView,
    candidates: Sequence[str],
    path: RelationSpec,
    *,
    endpoint: RelationEndpoint,
    projections: Selection[RestrictionProjection] | None = None,
    max_depth: int | None = None,
    budget: Budget | None = None,
) -> ValueEnumeration:
    """Classify explicit finite candidates for ordinary application enumerations.

    A complete nonmatch is an excluded candidate, not a coverage failure. Raw
    classification reports remain children. Missing/unknown candidates prevent
    a closed allowed set
    no unrequested vocabulary inventory is inferred.
    """
    if (
        not isinstance(view, VocabularyView)
        or budget is not None
        and not isinstance(budget, Budget)
    ):
        raise InputError("Expected VocabularyView and optional shared Budget")
    operation = budget if budget is not None else Budget(view.limits)
    issues = IssueCollector(operation)
    children = []
    allowed = []
    excluded = []
    complete = False
    checked = count = 0
    try:
        if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
            raise InputError("Candidates require a finite sequence")
        captured = capture(candidates, operation)
        count = len(captured)
        if not count:
            raise InputError("Select nonempty candidate inventory")
        operation.consume("states", 3 * count)
        operation.consume("bytes", 192 * count)
        owned = tuple(named(value, operation) for value in captured)
        for candidate in owned:
            result = _run(
                view,
                str(candidate),
                path,
                endpoint,
                projections,
                max_depth,
                operation,
                False,
            )
            children.append(result.report)
            checked += 1
            if result.status == "found":
                allowed.append(candidate)
            elif result.status == "nonmatch":
                excluded.append(candidate)
            retain_report(result.report, operation, issues)
        complete = all(child.verified for child in children)
    except (LimitExceeded, MemoryError, RecursionError):
        issues.exhausted("Relation membership exceeded its operation allowance")
    except InputError:
        try:
            issues.append(
                Issue(
                    "terms.relation_candidates_invalid",
                    "Select finite named candidate IRIs",
                    kind="input_error",
                )
            )
        except LimitExceeded:
            issues.exhausted("Relation candidate diagnostic allowance exhausted")
    report = Report(
        "terms.relation_membership",
        checked,
        tuple(issues),
        requested=count,
        complete=complete,
        children=tuple(children),
        regime="asserted-typed-relation-path",
    )
    return ValueEnumeration(
        tuple(allowed), excluded=tuple(excluded), closed=report.verified, report=report
    )
