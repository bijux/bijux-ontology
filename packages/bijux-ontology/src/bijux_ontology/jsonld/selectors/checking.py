"""Resolve declared source traversals and exact asserted entity selections."""

from collections.abc import Callable
from typing import cast

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.jsonld.entities import (
    EntityInventory,
    EntityKey,
    object_key,
    source_entity,
    subject_key,
)
from bijux_ontology.jsonld.observations import ObservedUsage
from bijux_ontology.jsonld.paths import (
    SourcePath,
    at_path,
    bound_index,
    bound_path,
    bound_pointer_text,
    bounded_location,
)
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
from bijux_ontology.limits import Budget, IssueCollector
from bijux_ontology.reports import Issue, Report


def _selector_metadata(
    selector: SourceSelector | EntitySelector, budget: Budget
) -> None:
    """Bound immutable selector declarations without traversing source data."""
    if isinstance(selector, SourceSelector):
        bound_path(selector.start, budget)
    elif isinstance(selector, EntitySelector):
        budget.consume("states", len(selector.identifiers) + len(selector.types))
        for value in (*selector.identifiers, *selector.types):
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
    unit: CoverageUnit = "source_occurrence"

    def count_matches(
        count: int,
        minimum: int,
        maximum: int | None,
        where: str,
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

    def traverse(
        starts: tuple[SourcePath, ...], steps: tuple[Step, ...]
    ) -> tuple[SourcePath, ...]:
        """Traverse each parent independently using actual source containers."""
        selected = starts
        for step in steps:
            following = []
            for parent in selected:
                budget.consume("work")
                value = at_path(document, parent)
                children: list[SourcePath] = []
                if isinstance(step, Property) and isinstance(value, dict):
                    if step.key in value:
                        children.append((*parent, step.key))
                elif isinstance(step, ArrayItems) and isinstance(value, list):
                    budget.consume("work", len(value))
                    children.extend((*parent, index) for index in range(len(value)))
                elif isinstance(step, KeyedItems) and isinstance(value, dict):
                    keys = (
                        tuple(value)
                        if step.keys is None
                        else tuple(dict.fromkeys(step.keys))
                    )
                    for key in keys:
                        budget.consume("work")
                        if key in value:
                            children.append((*parent, key))
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
                count_matches(
                    len(children),
                    step.minimum,
                    step.maximum,
                    bounded_location(parent, budget),
                    parent=f"property {step.key!r}"
                    if isinstance(step, Property)
                    else "array members"
                    if isinstance(step, ArrayItems)
                    else "keyed members",
                )
                following.extend(children)
            selected = tuple(following)
        return selected

    def source_paths(
        keys: set[EntityKey], include_references: bool
    ) -> tuple[SourcePath, ...]:
        """Associate selected entities with actual describing source objects."""
        result: list[SourcePath] = []
        established: set[EntityKey] = set()
        if not keys:
            return ()

        def walk(value: object, path: SourcePath) -> None:
            """Skip contexts and opaque payloads while checking node identity."""
            budget.consume("work")
            if not isinstance(value, (list, dict)):
                return
            try:
                usage = observe(path) if path else None
            except LimitExceeded:
                raise
            except InputError:
                return
            if usage is not None and any(
                item.traits.kind == "literal"
                and isinstance(item.value, dict)
                and "@value" in item.value
                and item.value["@value"] == value
                for item in usage.occurrences
            ):
                return
            if isinstance(value, list):
                for index, item in enumerate(value):
                    walk(item, (*path, index))
                return
            node = cast(dict[str, object], value)
            if "@value" in node:
                return
            wrapper = bool(node) and not set(node) - {
                "@context",
                "@graph",
                "@included",
                "@list",
                "@set",
                "@index",
            }
            if not wrapper:
                try:
                    binding = source_entity(node, path, observe, budget)
                except LimitExceeded:
                    raise
                except InputError:
                    binding = None
                if binding is not None and binding.key in keys:
                    if binding.described or include_references:
                        result.append(path)
                        established.add(binding.key)
                elif binding is None and usage is not None:
                    # A finite excluded identity cannot make selected coverage
                    # ambiguous. Only selected original contributions matter.
                    relevant = any(
                        subject_key(item) in keys or object_key(item) in keys
                        for item in usage.occurrences
                    )
                    if usage.limitation and (relevant or not usage.occurrences):
                        issues.append(
                            Issue(
                                "selector.binding_unverified",
                                usage.limitation,
                                bounded_location(path, budget),
                                kind="unverified",
                            )
                        )
            for key, item in node.items():
                if key != "@context":
                    walk(item, (*path, key))

        walk(document, ())
        for key in sorted(
            keys - established,
            key=lambda item: (
                item.identifier or "",
                item.anonymous if item.anonymous is not None else -1,
            ),
        ):
            budget.consume("work")
            issues.append(
                Issue(
                    "selector.binding_unverified",
                    "No describing source object could be associated with this entity",
                    f"entity[{key.identifier}]",
                    kind="unverified",
                )
            )
        return tuple(result)

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
            matching = []
            required_types = set(selector.types)
            for entity in inventory.entities:
                budget.consume("work")
                key = entity.key
                if key.anonymous_graph or key.graph != selector.graph:
                    continue
                if selector.identifiers and key.identifier not in selector.identifiers:
                    continue
                asserted = set(entity.types)
                if (
                    required_types != asserted
                    if selector.exact_types
                    else not required_types <= asserted
                ):
                    continue
                if entity.described or selector.include_references:
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
                paths = traverse(
                    source_paths(set(matching), selector.include_references),
                    selector.steps,
                )
            where = f"graph[{selector.graph}]"
        else:
            raise InputError("Expected a typed JSON-LD selector")
        count = len(paths) if unit == "source_occurrence" else len(entities)
        count_matches(count, selector.minimum, selector.maximum, where)
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
        issues.exhausted(str(error))
    count = len(paths) if unit == "source_occurrence" else len(entities)
    report = Report(
        f"jsonld-selection:{unit}",
        count or int(bool(issues)),
        tuple(issues),
        allow_empty=selector.minimum == 0,
        regime="jsonld:asserted",
        families=("selection", unit),
    )
    return Selection(unit, paths, entities, report)
