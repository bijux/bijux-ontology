"""Explicit finite classification policies, separate from real-world coverage."""

from collections.abc import Callable, Mapping
from itertools import chain
from threading import Event

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import SKOS, XSD
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import (
    Budget,
    BudgetKind,
    IssueCollector,
    Limits,
    retain_report,
)
from bijux_ontology.interfaces.adapters.vocabulary_bindings.controlled.algebra import (
    compile_allowed,
)
from bijux_ontology.rdf.literals import _lexical, datatype_relation
from bijux_ontology.rdf.patterns import matches_pattern
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.terms import bound_node, term_key
from bijux_ontology.skos.structure import reachability

from .reading import reserve, values
from .types import (
    ClassificationCheck,
    ClassificationPolicy,
    ClassificationView,
    PartitionContract,
    RelationKind,
    Resource,
)

_Issue = Callable[[str, str, Node, bool], None]


class _PathBudget(Budget):
    """Prepay bounded path storage while reusing the existing graph primitive."""

    def __init__(self, shared: Budget, nodes: int) -> None:
        """At most one simple path per target has at most nodes+1 references."""
        super().__init__(shared.limits)
        self.shared = shared
        self.width = 128 + 16 * (nodes + 1)
        self._retained_issues = shared._retained_issues

    def consume(self, kind: BudgetKind, amount: int = 1) -> None:
        """Charge storage before the generic traversal retains each new path."""
        if kind == "states":
            self.shared.consume("bytes", self.width * amount)
        self.shared.consume(kind, amount)

    def remaining(self, kind: BudgetKind) -> int:
        """Keep every enclosing quota cumulative rather than exposing a fresh one."""
        return self.shared.remaining(kind)


def _paths(
    adjacency: Mapping[Node, frozenset[Node]], operation: Budget
) -> dict[Node, dict[Node, tuple[Node, ...]]]:
    """Retain bounded generic reachability with exact original RDF endpoints."""
    reserve(operation, 4 * len(adjacency))
    for node, targets in adjacency.items():
        for term in chain((node,), targets):
            bound_node(term, operation)
            operation.consume("bytes", len(adjacency) * (4 * len(str(term)) + 128))
            operation.consume("work", len(adjacency) * (len(str(term)) + 1))
    return reachability(adjacency, _PathBudget(operation, len(adjacency)))


def _depth(
    raw: tuple[Node, ...],
    expected: int,
    subject: Node,
    operation: Budget,
    issue: _Issue,
) -> None:
    """Use existing finite datatype semantics without normalizing original terms."""
    observed = set()
    if not raw:
        issue("depth_missing", "Required level depth was not supplied", subject, True)
    for term in raw:
        bound_node(term, operation)
        if not isinstance(term, Literal):
            issue("depth_invalid", "Depth must be an integer literal", subject, False)
            continue
        datatype = str(term.datatype or XSD.string)
        relation = datatype_relation(datatype, str(XSD.integer), budget=operation)
        if relation not in ("equal", "subset") or len(term) > 32:
            issue(
                "depth_unverified",
                "Depth is outside the finite integer profile",
                subject,
                True,
            )
            continue
        operation.consume("work", len(term) + 1)
        if not _lexical(str(term), datatype):
            issue(
                "depth_invalid",
                "Depth literal has invalid datatype lexical form",
                subject,
                False,
            )
            continue
        value = int(str(term))
        reserve(operation)
        observed.add(value)
        if value != expected:
            issue(
                "depth_mismatch",
                "Declared depth differs from the 1-based ordered position",
                subject,
                False,
            )
    if len(observed) > 1:
        issue(
            "depth_conflict",
            "Selected level has conflicting integer depths",
            subject,
            False,
        )


def _adjacency(
    view: ClassificationView,
    kinds: tuple[RelationKind, ...],
    categories: set[Node],
    operation: Budget,
    pending: list[tuple[str, str, Node, bool]],
) -> dict[Node, frozenset[Node]]:
    """Select relationship meaning explicitly, retaining unknown support boundaries."""
    reserve(operation, 2 * len(categories))
    adjacency: dict[Node, set[Node]] = {node: set() for node in categories}
    for link in view.hierarchy:
        operation.consume("work")
        if link.kind not in kinds:
            continue
        bound_node(link.child, operation)
        bound_node(link.parent, operation)
        if not isinstance(link.child, URIRef | BNode) or not isinstance(
            link.parent, URIRef | BNode
        ):
            reserve(operation)
            pending.append(
                (
                    "hierarchy_invalid",
                    "Selected hierarchy has a non-resource endpoint",
                    link.child,
                    False,
                )
            )
            continue
        if link.child not in categories or link.parent not in categories:
            reserve(operation)
            pending.append(
                (
                    "hierarchy_scope_unverified",
                    "Hierarchy requires a category outside the finite projection",
                    link.child,
                    True,
                )
            )
            continue
        reserve(operation)
        adjacency[link.child].add(link.parent)
    return {key: frozenset(items) for key, items in adjacency.items()}


def _partition(
    view: ClassificationView,
    contract: PartitionContract,
    graph: Graph,
    positions: Mapping[Node, set[Resource]],
    operation: Operation,
) -> Report:
    """Check only a supplied finite unit population and closed assignment evidence."""
    issues = IssueCollector(operation)
    checked = 0
    finished = False

    def issue(code: str, message: str, subject: Node, unknown: bool = False) -> None:
        """Retain an exact subject location before continuing selected work."""
        bound_node(subject, operation)
        issues.append(
            Issue(
                "xkos." + code,
                message,
                str(subject),
                kind="unverified" if unknown else "finding",
            )
        )

    try:
        reserve(
            operation,
            len(contract.levels)
            + len(contract.population or ())
            + len(contract.assignment_predicates),
        )
        for level in contract.levels:
            bound_node(level, operation)
        requested_levels = set(contract.levels)
        available = {level.identifier for level in view.levels}
        if (
            not requested_levels
            or len(requested_levels) != len(contract.levels)
            or not requested_levels <= available
        ):
            raise ResourceError(
                "Partition levels are absent, repeated or outside the selected view"
            )
        if not contract.population:
            raise ResourceError(
                "Finite partition requires a nonempty explicit population"
            )
        if not contract.assignments_complete:
            issue(
                "assignments_open",
                "Assignment inventory is not complete",
                view.scheme,
                True,
            )
        units = set()
        owned = set()
        for triple in graph:
            operation.consume("work")
            for term in triple:
                bound_node(term, operation)
                if isinstance(term, BNode):
                    reserve(operation)
                    owned.add(id(term))
        for unit in contract.population:
            bound_node(unit, operation)
            if isinstance(unit, BNode) and id(unit) not in owned:
                raise ResourceError(
                    "Anonymous population unit lacks selected source ownership"
                )
            if unit in units:
                raise InputError("Finite population units must be distinct")
            units.add(unit)
        for predicate in contract.assignment_predicates:
            bound_node(predicate, operation)
        if len(set(contract.assignment_predicates)) != len(
            contract.assignment_predicates
        ):
            raise InputError("Select each assignment predicate once")
        categories = set(positions)
        pending: list[tuple[str, str, Node, bool]] = []
        adjacency = _adjacency(
            view, contract.propagation, categories, operation, pending
        )
        paths = _paths(adjacency, operation) if contract.propagation else {}
        for unit in contract.population:
            assigned: set[Node] = set()
            unresolved = False
            for predicate in contract.assignment_predicates:
                for category in values(graph, unit, predicate, operation):
                    if (
                        not isinstance(category, URIRef | BNode)
                        or category not in categories
                    ):
                        issue(
                            "assignment_unverified",
                            "Assignment target is outside the selected category scope",
                            unit,
                            True,
                        )
                        unresolved = True
                        continue
                    reserve(operation, len(paths.get(category, {})) + 1)
                    assigned.add(category)
                    assigned.update(paths.get(category, {}))
            for pending_code, message, child, unknown in pending:
                operation.consume("work")
                if child in assigned:
                    issue(pending_code, message, child, unknown)
                    unresolved = True
            for level in contract.levels:
                operation.consume("work", len(assigned) + 1)
                reserve(operation, len(assigned))
                selected = {
                    category
                    for category in assigned
                    if level in positions.get(category, set())
                }
                if contract.exhaustive and not selected:
                    if contract.assignments_complete and not unresolved:
                        issue(
                            "population_unassigned",
                            "Closed inventory has no required assignment",
                            unit,
                        )
                    else:
                        issue(
                            "population_unverified",
                            "Assignment absence is not established",
                            unit,
                            True,
                        )
                if contract.mutually_exclusive and len(selected) > 1:
                    issue(
                        "population_overlap",
                        "Unit has multiple asserted assignments at the required level",
                        unit,
                    )
                checked += 1
        finished = True
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(
                Issue(
                    "xkos.partition_unverified",
                    str(error),
                    kind="unverified"
                    if isinstance(error, ResourceError)
                    else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return Report(
        "adapter.xkos.finite_partition",
        checked,
        tuple(issues),
        requested=max(1, len(contract.levels) * len(contract.population or ())),
        complete=finished
        and not any(
            item.kind in ("unverified", "exhausted", "input_error") for item in issues
        ),
        regime="explicit-finite-population-only",
        exclusions=("unspecified-real-world-universe",),
    )


def check_classification(
    view: ClassificationView,
    policy: ClassificationPolicy | None = None,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> ClassificationCheck:
    """Check selected structural/code/finite assignment obligations only.

    coversExhaustively/coversMutuallyExclusively remain descriptive assertions.
    They never substitute for a supplied population, complete assignments, or
    an explicitly chosen propagation relationship. Ordinary SKOS is unaffected.
    """
    policy = ClassificationPolicy() if policy is None else policy
    if type(view) is not ClassificationView or type(policy) is not ClassificationPolicy:
        raise InputError("Classification checking requires typed owned inputs")
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    reports: list[Report] = [view.report]
    partition = None
    finished = False
    checked = 0
    selected_rows = []

    def issue(code: str, message: str, subject: Node, unknown: bool = False) -> None:
        """Retain an exact subject location before continuing selected work."""
        bound_node(subject, operation)
        issues.append(
            Issue(
                "xkos." + code,
                message,
                str(subject),
                kind="unverified" if unknown else "finding",
            )
        )

    try:
        retain_report(view.report, operation, issues)
        if not view.report.verified or view.source is None or view.metadata is None:
            raise ResourceError("Source classification extraction did not complete")
        if not view.metadata.same_scope(
            SnapshotMetadata.capture(view.source, operation)
        ):
            raise ResourceError("Original classification source metadata changed")
        graph = view.source._read(
            view.graph if view.graph is not None else "@default", budget=operation
        )
        reserve(
            operation,
            len(view.levels)
            + len(view.rows)
            + len(policy.required_levels)
            + len(policy.codes),
        )
        owned_levels = {
            id(level.identifier)
            for level in view.levels
            if isinstance(level.identifier, BNode)
        }
        selectors = chain(
            policy.required_levels,
            (rule.level for rule in policy.codes),
            policy.partition.levels if policy.partition is not None else (),
        )
        for selector in selectors:
            operation.consume("work")
            bound_node(selector, operation)
            if isinstance(selector, BNode) and id(selector) not in owned_levels:
                raise ResourceError(
                    "Blank policy level requires the selected classification witness"
                )
        for level in view.levels:
            bound_node(level.identifier, operation)
            for term in level.members:
                bound_node(term, operation)
        for category in view.categories:
            bound_node(category, operation)
        for requested_level in policy.required_levels:
            bound_node(requested_level, operation)
        if len(set(policy.required_levels)) != len(policy.required_levels):
            raise InputError("Select each required level once")
        if view.categories_explicit and not view.categories:
            raise ResourceError("Required category selection is empty")
        required = (
            set(policy.required_levels)
            if policy.required_levels
            else {level.identifier for level in view.levels}
        )
        available = {level.identifier for level in view.levels}
        if not required or not required <= available:
            raise ResourceError(
                "Required classification levels are missing from this selected view"
            )
        if len(set(view.ordered_levels)) != len(view.ordered_levels):
            issue(
                "level_repeated",
                "Ordered classification repeats a level resource",
                view.scheme,
            )
        positions: dict[Node, set[Resource]] = {}
        support_memberships: dict[Resource, set[Resource]] = {}
        support_positions: dict[Node, set[int]] = {}
        selected_categories = set(view.categories)
        for level in view.levels:
            active = level.identifier in required
            if active and policy.require_depth:
                _depth(level.depths, level.position, level.identifier, operation, issue)
            for member in level.members:
                operation.consume("work")
                if not isinstance(member, URIRef | BNode):
                    if active and not view.categories_explicit:
                        issue(
                            "category_invalid",
                            "Required member is not a resource",
                            level.identifier,
                        )
                    continue
                reserve(operation, 3)
                support_memberships.setdefault(member, set()).add(level.identifier)
                support_positions.setdefault(member, set()).add(level.position)
                if active and member in selected_categories:
                    positions.setdefault(member, set()).add(level.identifier)
            if active:
                checked += 1
        if view.number_of_levels:
            _depth(
                view.number_of_levels,
                len(view.ordered_levels),
                view.scheme,
                operation,
                issue,
            )
        for category in view.categories:
            if category not in support_memberships:
                issue(
                    "category_level_unverified",
                    "Requested category has no selected membership",
                    category,
                    True,
                )
            elif policy.unique_level and len(positions.get(category, ())) > 1:
                issue(
                    "category_level_conflict",
                    "Category occurs in conflicting required levels",
                    category,
                )
        for row in view.rows:
            if row.level in required:
                for term in chain(row.codes, row.labels):
                    bound_node(term, operation)
                reserve(operation, len(row.codes) + len(row.labels) + 1)
                selected_rows.append(row)
        if policy.tree_relations:
            pending: list[tuple[str, str, Node, bool]] = []
            adjacency = _adjacency(
                view,
                policy.tree_relations,
                set(support_memberships),
                operation,
                pending,
            )
            paths = _paths(adjacency, operation)
            reserve(operation, len(support_memberships))
            relevant: set[Node] = set(positions)
            for active_category in positions:
                reserve(operation, len(paths.get(active_category, {})))
                relevant.update(paths.get(active_category, {}))
            for pending_code, message, pending_child, unknown in pending:
                operation.consume("work")
                if pending_child in relevant:
                    issue(pending_code, message, pending_child, unknown)
            for child, parents in adjacency.items():
                operation.consume("work")
                if child not in relevant:
                    continue
                if len(parents) > 1:
                    issue(
                        "tree_multiple_parents",
                        "Selected tree permits at most one parent",
                        child,
                    )
                if child in paths.get(child, {}):
                    issue(
                        "tree_cycle",
                        "Selected tree has an actual directed cycle",
                        child,
                    )
                if policy.adjacent_levels:
                    for parent in parents:
                        for position in support_positions.get(child, ()):
                            operation.consume("work")
                            if position - 1 not in support_positions.get(parent, ()):
                                issue(
                                    "hierarchy_level_mismatch",
                                    "Parent is not at the immediately preceding level",
                                    child,
                                )
        handled_codes = set()
        for rule in policy.codes:
            bound_node(rule.level, operation)
            if rule.level in handled_codes:
                raise InputError("Select each level code policy once")
            handled_codes.add(rule.level)
            if rule.level not in required:
                raise ResourceError("Code policy requests an unselected required level")
            allowed = (
                compile_allowed(rule.allowed, budget=operation)
                if rule.allowed is not None
                else None
            )
            if allowed is not None:
                allowed_report = allowed.qualification_report()
                reports.append(allowed_report)
                retain_report(allowed_report, operation, issues)
                reserve(operation, len(allowed.values) + len(allowed.excluded))
            permitted = (
                {term_key(term) for term in allowed.values} if allowed else set()
            )
            excluded = (
                {term_key(term) for term in allowed.excluded} if allowed else set()
            )
            seen_codes: dict[tuple[str | None, ...], Resource] = {}
            level = next(
                record for record in view.levels if record.identifier == rule.level
            )
            pattern = None
            if rule.declared_pattern_dialect:
                if len(level.patterns) != 1 or not isinstance(
                    level.patterns[0], Literal
                ):
                    issue(
                        "pattern_unverified",
                        "Selected declared pattern is missing or ambiguous",
                        rule.level,
                        True,
                    )
                else:
                    bound_node(level.patterns[0], operation)
                    pattern = str(level.patterns[0])
            for row in selected_rows:
                operation.consume("work")
                if row.level != rule.level:
                    continue
                raw = values(graph, row.category, SKOS.notation, operation)
                if rule.required and not raw:
                    issue(
                        "code_missing",
                        "Selected level requires category notation",
                        row.category,
                    )
                for code in raw:
                    bound_node(code, operation)
                    if not isinstance(code, Literal):
                        issue(
                            "code_invalid",
                            "Selected notation must be an RDF literal",
                            row.category,
                        )
                        continue
                    reserve(operation)
                    key = term_key(code)
                    if (
                        rule.unique
                        and key in seen_codes
                        and seen_codes[key] != row.category
                    ):
                        issue(
                            "code_collision",
                            "Distinct selected categories share the same exact code",
                            row.category,
                        )
                    seen_codes[key] = row.category
                    if allowed is not None and key not in permitted:
                        issue(
                            "code_disallowed"
                            if key in excluded or allowed.qualified
                            else "code_unverified",
                            "Code lacks selected allowed-set membership",
                            row.category,
                            not (key in excluded or allowed.qualified),
                        )
                    if pattern is not None:
                        matched = matches_pattern(
                            pattern, str(code), "fullmatch", operation
                        )
                        if matched is not True:
                            issue(
                                "code_pattern"
                                if matched is False
                                else "code_pattern_unverified",
                                "Code does not have a proved selected RE2 full match",
                                row.category,
                                matched is None,
                            )
        if policy.partition is not None:
            reserve(operation, len(support_memberships))
            partition_positions: dict[Node, set[Resource]] = {
                node: memberships
                for node, memberships in support_memberships.items()
                if node in selected_categories
            }
            partition = _partition(
                view, policy.partition, graph, partition_positions, operation
            )
            reports.append(partition)
            retain_report(partition, operation, issues)
        finished = True
    except LimitExceeded as error:
        for child_report in reports:
            issues.retain_completed(child_report)
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(
                Issue(
                    "xkos.classification_unverified",
                    str(error),
                    kind="unverified"
                    if isinstance(error, ResourceError)
                    else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            for child_report in reports:
                issues.retain_completed(child_report)
            issues.exhausted(str(exhausted))
    return ClassificationCheck(
        view,
        tuple(selected_rows),
        partition,
        Report(
            "adapter.xkos.classification.check",
            checked,
            tuple(issues),
            requested=max(1, len(policy.required_levels) or len(view.levels)),
            complete=finished
            and not any(
                item.kind in ("unverified", "exhausted", "input_error")
                for item in issues
            ),
            children=tuple(reports),
            regime="selected-finite-xkos-1.2",
            exclusions=(
                "unspecified-real-world-universe",
                "unselected-hierarchy-implications",
                "implicit-regex-dialect",
            ),
        ),
    )
