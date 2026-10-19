"""Validated declarations of expected meanings for selected JSON-LD usages."""

from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from typing import Literal, cast

from bijux_ontology._json import own_json
from bijux_ontology._jsonpaths import SourcePath, bound_pointer_text, location
from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import Budget
from bijux_ontology.jsonld.observations import ObservedUsage
from bijux_ontology.rdf.identifiers import absolute_iri, language_tag

from .lists import ExpectedList, compare_list_values, compare_lists
from .values import ValueKind

ContainerKind = Literal["list", "unordered"]


@dataclass(frozen=True)
class ContextExpectation:
    """Expected expansion of one supplied field usage, located in compact JSON.

    Paths name a field value, including array indexes where needed. Containers
    describe expanded list versus unordered values, not raw context spelling.
    ordered_values describes one complete list, never concatenated collections.
    lists describes an unordered collection of complete ordered RDF member lists;
    an outer ordering contract requires separate explicit per-list source paths.
    """

    path: SourcePath
    predicate: str
    value_kind: ValueKind | None = None
    datatype: str | Absent | None = None
    language: str | Absent | None = None
    container: ContainerKind | None = None
    reverse: bool = False
    identifier: str | Absent | None = None
    types: tuple[str, ...] | None = None
    direction: Literal["ltr", "rtl"] | Absent | None = None
    identifier_kind: Literal["iri", "blank", "relative"] | None = None
    ordered_values: tuple[object, ...] | None = None
    lists: tuple[ExpectedList, ...] | None = None
    index: str | Absent | None = field(default=None, kw_only=True)
    list_index: str | Absent | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.path, tuple)
            or not self.path
            or any(
                type(key) not in (str, int) or isinstance(key, int) and key < 0
                for key in self.path
            )
        ):
            raise InputError("Expected a nonempty key/index usage selector")
        if not isinstance(self.predicate, str):
            raise InputError("Expected a predicate string")
        if self.predicate not in {
            "@id",
            "@type",
            "@graph",
            "@included",
            "@list",
            "@reverse",
        }:
            absolute_iri(self.predicate, location=location(self.path))
        if self.value_kind not in (
            None,
            "literal",
            "iri",
            "node",
            "blank",
            "relative",
            "graph",
        ):
            raise InputError("Unknown context value kind")
        if self.container not in (None, "list", "unordered"):
            raise InputError("Unknown context container")
        if type(self.reverse) is not bool:
            raise InputError("reverse must be a boolean")
        for value in (self.datatype, self.identifier):
            if isinstance(value, str):
                absolute_iri(value)
        for name in ("datatype", "language", "identifier", "index", "list_index"):
            value = getattr(self, name)
            if value is not None and value is not ABSENT and not isinstance(value, str):
                raise InputError(f"{name} must be a string, ABSENT or None")
        if isinstance(self.language, str):
            language_tag(self.language)
        if self.direction not in (None, ABSENT, "ltr", "rtl"):
            raise InputError("Unknown base direction")
        if self.identifier_kind not in (None, "iri", "blank", "relative"):
            raise InputError("Unknown identifier kind")
        if self.types is not None:
            if not isinstance(self.types, tuple):
                raise InputError("types must be a tuple of absolute IRIs")
            for iri in self.types:
                absolute_iri(iri)
        if self.lists is not None:
            if not isinstance(self.lists, tuple) or any(
                not isinstance(item, ExpectedList) for item in self.lists
            ):
                raise InputError("lists must be a tuple of ExpectedList values")
            if self.ordered_values is not None:
                raise InputError("Choose lists or ordered_values, not both")
        if self.ordered_values is not None:
            if not isinstance(self.ordered_values, tuple):
                raise InputError(
                    "ordered_values must be a tuple of expanded JSON values"
                )
            object.__setattr__(
                self,
                "ordered_values",
                tuple(cast(list[object], own_json(list(self.ordered_values)))),
            )


def compare_usage(
    expectation: ContextExpectation, usage: ObservedUsage, *, budget: Budget
) -> Iterator[Issue]:
    """Compare known occurrences without expanding documents or resolving resources.

    Charge the supplied operation for each occurrence and comparison. Streaming
    diagnostics lets callers retain earlier findings if a later charge exhausts
    the budget; orchestration and report completeness remain the caller's concern.
    """
    for value in (expectation.index, expectation.list_index):
        if isinstance(value, str):
            bound_pointer_text(value, budget)
    where = location(expectation.path)
    if expectation.lists is not None:
        yield from compare_lists(expectation.lists, usage, budget=budget)
    groups = {item.list_id for item in usage.occurrences}
    selected_length = sum(not item.empty for item in usage.occurrences)
    complete_list = (
        len(groups) == 1
        and None not in groups
        and all(
            item.container == "list" and item.list_length == selected_length
            for item in usage.occurrences
        )
    )
    if expectation.ordered_values is not None and not complete_list:
        yield Issue(
            "context.order_unverified",
            "Ordering requires one complete selected list",
            where,
            kind="unverified",
        )
    elif expectation.ordered_values is not None:
        for issue in compare_list_values(
            (expectation.ordered_values,), usage, budget=budget
        ):
            yield replace(
                issue,
                code="context.order_mismatch"
                if issue.kind == "finding"
                else "context.order_unverified",
            )
    for item in usage.occurrences:
        budget.consume("work")
        comparisons: list[tuple[str, object, object]] = [
            ("mapping_mismatch", expectation.predicate, item.predicate),
            ("reverse_mismatch", expectation.reverse, item.reverse),
        ]
        if expectation.container is not None:
            comparisons.append(
                (
                    "container_mismatch",
                    expectation.container,
                    item.container,
                )
            )
        traits = item.traits
        if expectation.index is not None:
            comparisons.append(("index_mismatch", expectation.index, traits.index))
        if expectation.list_index is not None:
            comparisons.append(
                ("list_index_mismatch", expectation.list_index, item.list_index)
            )
        comparisons.extend(
            [
                (
                    "identifier_mismatch",
                    expectation.identifier,
                    traits.identifier,
                ),
                (
                    "identifier_kind_mismatch",
                    expectation.identifier_kind,
                    traits.identifier_kind,
                ),
                (
                    "types_mismatch",
                    set(expectation.types) if expectation.types is not None else None,
                    set(traits.types),
                ),
                (
                    "direction_mismatch",
                    expectation.direction,
                    traits.direction,
                ),
            ]
        )
        if item.empty and any(
            value is not None
            for value in (
                expectation.value_kind,
                expectation.datatype,
                expectation.language,
                expectation.identifier,
                expectation.identifier_kind,
                expectation.types,
                expectation.direction,
            )
        ):
            yield Issue(
                "context.value_unverified",
                "An empty list supplies no value traits",
                where,
                kind="unverified",
            )
        else:
            comparisons.extend(
                [
                    (
                        "value_kind_mismatch",
                        expectation.value_kind,
                        traits.kind,
                    ),
                    (
                        "datatype_mismatch",
                        expectation.datatype,
                        traits.datatype,
                    ),
                    (
                        "language_mismatch",
                        expectation.language,
                        traits.language,
                    ),
                ]
            )
        for code, wanted, actual in comparisons:
            budget.consume("work")
            equal = actual is None if wanted is ABSENT else wanted == actual
            if (
                code == "language_mismatch"
                and isinstance(wanted, str)
                and isinstance(actual, str)
            ):
                equal = wanted.lower() == actual.lower()
            if wanted is not None and not equal:
                yield Issue(
                    f"context.{code}",
                    "Expanded usage differs from its declaration",
                    item.location,
                    expected=str(wanted),
                    actual=str(actual),
                )
