"""Finite typed source traversal and graph-qualified asserted entity selection."""

from collections.abc import Callable
from dataclasses import KW_ONLY, dataclass
from typing import Literal, cast

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.reports import Issue, Report

from .entities import EntityInventory, EntityKey, object_key, source_entity, subject_key
from .observations import ObservedUsage, SourcePath, at_path, location

CoverageUnit = Literal["source_occurrence", "rdf_entity"]


def _bounds(minimum: int, maximum: int | None) -> None:
    if (
        type(minimum) is not int
        or minimum < 0
        or maximum is not None
        and (type(maximum) is not int or maximum < minimum)
    ):
        raise InputError("Selector bounds require 0 <= minimum <= maximum")


def _path(path: SourcePath) -> None:
    if not isinstance(path, tuple) or any(
        type(key) not in (str, int) or isinstance(key, int) and key < 0 for key in path
    ):
        raise InputError("Expected a concrete key/index path")


@dataclass(frozen=True)
class Property:
    """Select a concrete property, bounding matches separately for each parent."""

    key: str
    minimum: int = 0
    maximum: int | None = 1

    def __post_init__(self) -> None:
        if not isinstance(self.key, str):
            raise InputError("A property selector requires a string key")
        _bounds(self.minimum, self.maximum)


@dataclass(frozen=True)
class ArrayItems:
    """Select each actual array member, without treating objects as arrays."""

    minimum: int = 0
    maximum: int | None = None

    def __post_init__(self) -> None:
        _bounds(self.minimum, self.maximum)


@dataclass(frozen=True)
class KeyedItems:
    """Select members of an explicitly declared keyed source container.

    None selects all supplied keys. A finite tuple selects only those exact keys;
    these keys are source syntax, not inferred language/index/identity semantics.
    """

    keys: tuple[str, ...] | None = None
    minimum: int = 0
    maximum: int | None = None

    def __post_init__(self) -> None:
        if self.keys is not None and (
            not isinstance(self.keys, tuple)
            or any(not isinstance(key, str) for key in self.keys)
        ):
            raise InputError("Keyed containers require a tuple of string keys")
        _bounds(self.minimum, self.maximum)


Step = Property | ArrayItems | KeyedItems


def _steps(steps: tuple[Step, ...]) -> None:
    if not isinstance(steps, tuple) or any(
        not isinstance(step, (Property, ArrayItems, KeyedItems)) for step in steps
    ):
        raise InputError("Expected typed finite selector steps")


@dataclass(frozen=True)
class SourceSelector:
    """Traverse source occurrences with aggregate and per-parent bounds.

    Each step's bounds apply universally to its selected parents. Selector bounds
    apply to the final aggregate. minimum=0 explicitly permits an empty selection.
    """

    steps: tuple[Step, ...]
    start: SourcePath = ()
    _: KW_ONLY
    minimum: int = 1
    maximum: int | None = None

    def __post_init__(self) -> None:
        _steps(self.steps)
        _path(self.start)
        _bounds(self.minimum, self.maximum)


@dataclass(frozen=True)
class EntitySelector:
    """Select finite expanded identities/asserted types in exactly one graph.

    Types match asserted membership, never entailed classification. exact_types
    additionally rejects other asserted types. Source coverage selects each actual
    describing object separately before following steps; pure references count
    only when include_references=True. Entity coverage joins all its descriptions
    and returns EntityKeys, with no fabricated source pointer.
    """

    identifiers: tuple[str, ...] = ()
    types: tuple[str, ...] = ()
    _: KW_ONLY
    graph: str | None
    coverage_unit: CoverageUnit = "rdf_entity"
    exact_types: bool = False
    include_references: bool = False
    steps: tuple[Step, ...] = ()
    minimum: int = 1
    maximum: int | None = None

    def __post_init__(self) -> None:
        for values in (self.identifiers, self.types):
            if not isinstance(values, tuple):
                raise InputError("Identity and asserted-type filters require tuples")
            for value in values:
                absolute_iri(value)
        if self.graph is not None:
            absolute_iri(self.graph)
        if self.coverage_unit not in ("source_occurrence", "rdf_entity"):
            raise InputError("Unknown selector coverage unit")
        if (
            type(self.exact_types) is not bool
            or type(self.include_references) is not bool
        ):
            raise InputError("Selector policies must be booleans")
        _steps(self.steps)
        if self.coverage_unit == "rdf_entity" and self.steps:
            raise InputError(
                "Source traversal steps require source occurrence coverage"
            )
        _bounds(self.minimum, self.maximum)


@dataclass(frozen=True)
class Selection:
    """Concrete matches and truthful coverage, separate from semantic checking."""

    coverage_unit: CoverageUnit
    paths: tuple[SourcePath, ...]
    entities: tuple[EntityKey, ...]
    report: Report

    @property
    def count(self) -> int:
        """Count the explicitly chosen coverage unit, not RDF/source interchangeably."""
        return (
            len(self.paths)
            if self.coverage_unit == "source_occurrence"
            else len(self.entities)
        )


def resolve_selection(
    selector: SourceSelector | EntitySelector,
    document: object,
    inventory: EntityInventory,
    observe: Callable[[SourcePath], ObservedUsage],
    budget: Budget,
) -> Selection:
    """Resolve a bounded operation using owned input and original observations."""
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
                            location(parent),
                            actual=type(value).__name__,
                        )
                    )
                count_matches(
                    len(children),
                    step.minimum,
                    step.maximum,
                    location(parent),
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
                                location(path),
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
        if isinstance(selector, SourceSelector):
            try:
                at_path(document, selector.start)
            except (KeyError, IndexError):
                paths = ()
            else:
                paths = traverse((selector.start,), selector.steps)
            where = location(selector.start)
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
        if unit == "source_occurrence":
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
                            location(path),
                            kind="unverified",
                        )
                    )
                    continue
                if usage.limitation:
                    issues.append(
                        Issue(
                            "selector.binding_unverified",
                            usage.limitation,
                            location(path),
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
