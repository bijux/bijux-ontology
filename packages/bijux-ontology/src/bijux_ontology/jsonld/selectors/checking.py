"""Resolve declared source traversals and exact asserted entity selections."""

from collections.abc import Callable, Iterator
from itertools import chain

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, IssueCollector
from bijux_ontology.json.paths import (
    SourcePath,
    at_path,
    bound_index,
    bound_path,
    bound_pointer_text,
    bounded_location,
)
from bijux_ontology.jsonld.evidence.associations import source_associations
from bijux_ontology.jsonld.evidence.entities import (
    EntityInventory,
    EntityKey,
    SourceEntity,
    object_key,
    subject_key,
)
from bijux_ontology.jsonld.evidence.occurrences import ObservedUsage
from bijux_ontology.jsonld.selectors.types import (
    ArrayItems,
    CoverageUnit,
    EntitySelector,
    KeyedItems,
    Property,
    Selection,
    SourceSelector,
    Step,
)


def _selector_metadata(
    selector: SourceSelector | EntitySelector, budget: Budget
) -> None:
    """Bound immutable selector declarations without traversing source data."""
    if isinstance(selector, SourceSelector):
        bound_path(selector.start, budget)
    elif isinstance(selector, EntitySelector):
        budget.consume("states", len(selector.identifiers) + len(selector.types))
        for value in chain(selector.identifiers, selector.types):
            bound_pointer_text(value, budget)
        if selector.graph is not None:
            bound_pointer_text(selector.graph, budget)
    else:
        raise InputError("Expected a typed JSON-LD selector")
    bound_index(selector.minimum, budget)
    if selector.maximum is not None:
        bound_index(selector.maximum, budget)
    budget.consume("states", len(selector.steps))
    budget.consume("work", len(selector.steps))
    for step in selector.steps:
        bound_index(step.minimum, budget)
        if step.maximum is not None:
            bound_index(step.maximum, budget)
        if isinstance(step, Property):
            bound_pointer_text(step.key, budget)
        elif isinstance(step, KeyedItems) and step.keys is not None:
            budget.consume("states", len(step.keys))
            for key in step.keys:
                bound_pointer_text(key, budget)


def _count_matches(
    count: int,
    minimum: int,
    maximum: int | None,
    where: str,
    budget: Budget,
    issues: IssueCollector,
    *,
    parent: str | None = None,
) -> None:
    """Check aggregate or per-parent bounds without borrowing sibling counts."""
    budget.consume("work")
    if count < minimum or maximum is not None and count > maximum:
        upper = str(maximum) if maximum is not None else "unbounded"
        issues.append(
            Issue(
                "selector.parent_count_mismatch"
                if parent
                else "selector.count_mismatch",
                f"Selected {parent} count violates per-parent bounds"
                if parent is not None
                else "Selected matches violate aggregate bounds",
                where,
                expected=f"{minimum}..{upper}",
                actual=str(count),
            )
        )


class _SourceMatches:
    """One selector's completed associations and its own coverage policy."""

    __slots__ = (
        "keys",
        "include_references",
        "budget",
        "issues",
        "paths",
        "established",
    )

    def __init__(
        self,
        keys: set[EntityKey],
        include_references: bool,
        budget: Budget,
        issues: IssueCollector,
    ) -> None:
        budget.consume("states")
        budget.reserve_storage(384)
        self.keys, self.include_references = keys, include_references
        self.budget, self.issues = budget, issues
        self.paths: list[SourcePath] = []
        self.established: set[EntityKey] = set()

    def adopt(self, evidence: SourceEntity | ObservedUsage) -> None:
        """Retain only paid relevant describing objects or explicit uncertainties."""
        if not self.keys:
            return
        if isinstance(evidence, SourceEntity):
            if evidence.key in self.keys and (
                evidence.described or self.include_references
            ):
                self.budget.consume("states", 2)
                self.budget.reserve_storage(128)
                self.paths.append(evidence.path)
                self.established.add(evidence.key)
        elif evidence.limitation:
            relevant = any(
                subject_key(item) in self.keys or object_key(item) in self.keys
                for item in evidence.occurrences
            )
            if relevant or not evidence.occurrences:
                self.issues.append(
                    Issue(
                        "selector.binding_unverified",
                        evidence.limitation,
                        bounded_location(evidence.path, self.budget),
                        kind="unverified",
                    )
                )

    def finish(self) -> None:
        """Missing descriptions require a completed source traversal."""
        if not self.keys:
            return
        self.budget.consume("states", len(self.keys) * 2)
        self.budget.reserve_storage(256 + 128 * len(self.keys))
        for key in sorted(
            self.keys - self.established,
            key=lambda item: (
                item.identifier or "",
                item.anonymous if item.anonymous is not None else -1,
            ),
        ):
            self.budget.consume("work")
            self.issues.append(
                Issue(
                    "selector.binding_unverified",
                    "No describing source object could be associated with this entity",
                    f"entity[{key.identifier}]",
                    kind="unverified",
                )
            )


def _matching_entities(
    selector: EntitySelector,
    inventory: EntityInventory,
    budget: Budget,
) -> Iterator[EntityKey]:
    """Yield qualified identity/type matches without precollecting the inventory."""
    budget.reserve_storage(256 + 64 * len(selector.types))
    required_types = set(selector.types)
    for entity in inventory.iter_entities(budget=budget):
        budget.consume("work")
        key = entity.key
        if key.anonymous_graph or key.graph != selector.graph:
            continue
        if selector.identifiers and key.identifier not in selector.identifiers:
            continue
        budget.consume("states", len(entity.types))
        budget.reserve_storage(256 + 64 * len(entity.types))
        asserted = set(entity.types)
        if (
            required_types != asserted
            if selector.exact_types
            else not required_types <= asserted
        ):
            continue
        if entity.described or selector.include_references:
            budget.consume("states")
            budget.reserve_storage(128)
            yield key


def _selection(
    unit: CoverageUnit,
    paths: tuple[SourcePath, ...],
    entities: tuple[EntityKey, ...],
    minimum: int,
    issues: IssueCollector,
) -> Selection:
    """Keep the declared coverage unit and its raw completed evidence explicit."""
    count = len(paths) if unit == "source_occurrence" else len(entities)
    report = Report(
        f"jsonld-selection:{unit}",
        count or int(bool(issues)),
        tuple(issues),
        allow_empty=minimum == 0,
        regime="jsonld:asserted",
        families=("selection", unit),
    )
    return Selection(unit, paths, entities, report)


def resolve_selection(
    selector: SourceSelector | EntitySelector,
    document: object,
    inventory: EntityInventory,
    observe: Callable[[SourcePath], ObservedUsage],
    budget: Budget,
    *,
    require_usage: bool = True,
    association_only: bool = False,
) -> Selection:
    """Resolve bounded source paths or semantically associated entity selections.

    Explicit source form checking may omit usage attribution. An association-only
    entity selection proves describing-node ownership, without promising isolated
    attribution of the whole object's contribution. Semantic association, graph
    scope and required counts still apply; ordinary selection remains unchanged.
    """
    if type(require_usage) is not bool:
        raise InputError("require_usage must be a boolean")
    if type(association_only) is not bool:
        raise InputError("association_only must be a boolean")
    if association_only and (
        require_usage
        or not isinstance(selector, EntitySelector)
        or selector.coverage_unit != "source_occurrence"
        or selector.steps
    ):
        raise InputError("Association-only selection requires whole entity paths")
    issues = IssueCollector(budget)
    paths: tuple[SourcePath, ...] = ()
    entities: tuple[EntityKey, ...] = ()
    unit: CoverageUnit = (
        selector.coverage_unit
        if isinstance(selector, EntitySelector)
        else "source_occurrence"
    )
    matching: list[EntityKey] = []

    def traverse(
        starts: tuple[SourcePath, ...], steps: tuple[Step, ...]
    ) -> tuple[SourcePath, ...]:
        """Prepay each child path and retain only completed final-step matches."""
        nonlocal paths

        def append(
            parent: SourcePath,
            key: str | int,
            children: list[SourcePath],
            completed: list[SourcePath] | None,
        ) -> None:
            """Own a path tuple and its traversal/output reference slots."""
            budget.consume("states")
            budget.reserve_storage(128 + 8 * (len(parent) + 1))
            child = (*parent, key)
            children.append(child)
            if completed is not None:
                completed.append(child)

        selected = starts
        for number, step in enumerate(steps):
            budget.reserve_storage(192)
            following: list[SourcePath] = []
            final = number == len(steps) - 1
            completed: list[SourcePath] = []
            try:
                for parent in selected:
                    budget.consume("work")
                    value = at_path(document, parent)
                    budget.reserve_storage(64)
                    children: list[SourcePath] = []

                    if isinstance(step, Property) and isinstance(value, dict):
                        if step.key in value:
                            append(
                                parent, step.key, children, completed if final else None
                            )
                    elif isinstance(step, ArrayItems) and isinstance(value, list):
                        for index in range(len(value)):
                            budget.consume("work")
                            append(
                                parent, index, children, completed if final else None
                            )
                    elif isinstance(step, KeyedItems) and isinstance(value, dict):
                        budget.reserve_storage(256)
                        seen: set[str] = set()
                        for key in value if step.keys is None else step.keys:
                            budget.consume("work")
                            if step.keys is not None:
                                if key in seen:
                                    continue
                                budget.consume("states")
                                budget.reserve_storage(64)
                                seen.add(key)
                            if key in value:
                                if step.keys is None:
                                    bound_pointer_text(key, budget)
                                append(
                                    parent, key, children, completed if final else None
                                )
                    else:
                        issues.append(
                            Issue(
                                "selector.structure_mismatch",
                                "Selected parent has the wrong source container kind",
                                bounded_location(parent, budget),
                                actual=type(value).__name__,
                            )
                        )
                    if isinstance(step, Property):
                        budget.consume("work", len(step.key))
                        budget.consume("bytes", 64 + 12 * len(step.key))
                    _count_matches(
                        len(children),
                        step.minimum,
                        step.maximum,
                        bounded_location(parent, budget),
                        budget,
                        issues,
                        parent=f"property {step.key!r}"
                        if isinstance(step, Property)
                        else "array members"
                        if isinstance(step, ArrayItems)
                        else "keyed members",
                    )
                    following.extend(children)
            except LimitExceeded:
                if final:
                    paths = tuple(completed)
                raise
            selected = tuple(following)
        return selected

    def source_paths(
        keys: set[EntityKey], include_references: bool
    ) -> tuple[SourcePath, ...]:
        """Associate selected entities through the shared original evidence walk."""
        nonlocal paths
        matches = _SourceMatches(keys, include_references, budget, issues)
        if not keys:
            return ()
        try:
            for evidence in source_associations(document, observe, budget):
                matches.adopt(evidence)
        except LimitExceeded:
            if not selector.steps:
                paths = tuple(matches.paths)
            raise
        if not selector.steps:
            paths = tuple(matches.paths)
        matches.finish()
        return tuple(matches.paths)

    try:
        _selector_metadata(selector, budget)
        if isinstance(selector, SourceSelector):
            try:
                at_path(document, selector.start)
            except (KeyError, IndexError):
                paths = ()
            else:
                paths = traverse((selector.start,), selector.steps)
            where = bounded_location(selector.start, budget)
        elif isinstance(selector, EntitySelector):
            unit = selector.coverage_unit
            for key in _matching_entities(selector, inventory, budget):
                matching.append(key)
            if unit == "rdf_entity":
                entities = tuple(
                    sorted(
                        matching,
                        key=lambda item: (
                            item.identifier or "",
                            item.anonymous if item.anonymous is not None else -1,
                        ),
                    )
                )
            else:
                budget.consume("states", len(matching))
                budget.reserve_storage(256 + 64 * len(matching))
                paths = traverse(
                    source_paths(set(matching), selector.include_references),
                    selector.steps,
                )
            where = f"graph[{selector.graph}]"
        else:
            raise InputError("Expected a typed JSON-LD selector")
        count = len(paths) if unit == "source_occurrence" else len(entities)
        _count_matches(count, selector.minimum, selector.maximum, where, budget, issues)
        if unit == "source_occurrence" and (
            require_usage
            or isinstance(selector, EntitySelector)
            and not association_only
        ):
            for path in paths:
                if not path:
                    continue
                budget.consume("work")
                try:
                    usage = observe(path)
                except LimitExceeded:
                    raise
                except InputError as error:
                    issues.append(
                        Issue(
                            "selector.binding_unverified",
                            str(error),
                            bounded_location(path, budget),
                            kind="unverified",
                        )
                    )
                    continue
                if usage.limitation:
                    issues.append(
                        Issue(
                            "selector.binding_unverified",
                            usage.limitation,
                            bounded_location(path, budget),
                            kind="unverified",
                        )
                    )
    except LimitExceeded as error:
        if unit == "rdf_entity":
            entities = tuple(matching)
        issues.exhausted(str(error))
    return _selection(unit, paths, entities, selector.minimum, issues)


def resolve_entity_associations(
    selectors: tuple[EntitySelector, ...],
    document: object,
    inventory: EntityInventory,
    observe: Callable[[SourcePath], ObservedUsage],
    budget: Budget,
) -> tuple[Selection, ...]:
    """Resolve whole-entity source associations jointly, with per-request truth.

    This is the association-only selector contract: no whole-contribution or
    source-step guarantee is added. Completed paths survive an interrupted walk;
    absent descriptions and aggregate counts require complete traversal.
    """
    if type(selectors) is not tuple:
        raise InputError("Joint associations require a finite selector tuple")
    budget.consume("states", len(selectors))
    budget.reserve_storage(256 + 128 * len(selectors))
    requests: list[_SourceMatches] = []
    owners: dict[EntityKey, list[_SourceMatches]] = {}
    for selector in selectors:
        if (
            not isinstance(selector, EntitySelector)
            or selector.coverage_unit != "source_occurrence"
            or selector.steps
        ):
            raise InputError("Joint associations require whole entity paths")
        _selector_metadata(selector, budget)
        matching = []
        for key in _matching_entities(selector, inventory, budget):
            matching.append(key)
        budget.consume("states", len(matching))
        budget.reserve_storage(256 + 64 * len(matching))
        request = _SourceMatches(
            set(matching), selector.include_references, budget, IssueCollector(budget)
        )
        requests.append(request)
        for key in request.keys:
            budget.consume("states")
            budget.reserve_storage(128)
            owners.setdefault(key, []).append(request)
    stopped = None
    try:
        if owners:
            for evidence in source_associations(document, observe, budget):
                if isinstance(evidence, SourceEntity):
                    for request in owners.get(evidence.key, ()):
                        request.adopt(evidence)
                else:
                    for request in requests:
                        request.adopt(evidence)
    except LimitExceeded as error:
        stopped = str(error)
    for selector, request in zip(selectors, requests, strict=True):
        if stopped is None:
            try:
                request.finish()
                _count_matches(
                    len(request.paths),
                    selector.minimum,
                    selector.maximum,
                    f"graph[{selector.graph}]",
                    budget,
                    request.issues,
                )
            except LimitExceeded as error:
                stopped = str(error)
        if stopped is not None:
            request.issues.exhausted(stopped)
    return tuple(
        _selection(
            "source_occurrence",
            tuple(request.paths),
            (),
            selector.minimum,
            request.issues,
        )
        for selector, request in zip(selectors, requests, strict=True)
    )
