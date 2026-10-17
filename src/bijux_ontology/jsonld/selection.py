"""Associate selected source usages with owned expanded semantic contributions."""

import json
from collections import Counter
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from typing import cast

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget

from .observations import (
    ObservedOccurrence,
    ObservedUsage,
    SourcePath,
    at_path,
    location,
    occurrences,
)
from .resources import context_is_unscoped

Expansion = Callable[[object], list[dict[str, object]]]


class UsageIndex:
    """Reuse original expansion and finite semantic differences for selections."""

    def __init__(
        self,
        document: object,
        expand: Expansion,
        expanded: object,
        *,
        unscoped_resources: Callable[[], bool] = lambda: False,
        budget: Budget | None = None,
    ) -> None:
        self._budget = budget or Budget()
        self._document = deepcopy(document)
        self._expand = expand
        self._original = occurrences(expanded)
        self._cache: dict[SourcePath, ObservedUsage] = {}
        self._counts = Counter(item.identity for item in self._original)
        self._unscoped_resources = unscoped_resources
        self._flat_context = (
            context_is_unscoped(document.get("@context", {}))
            if isinstance(document, dict)
            else False
        )

    def _flat_projection(
        self, path: SourcePath, original: object
    ) -> ObservedUsage | None:
        """Isolate flat primitive fields only when sibling scopes cannot interfere.

        Public expansion still determines every semantic trait. Restricting the
        syntax to local, unscoped definitions proves that removing sibling fields
        cannot change the selected field's context or subject. Complex usages
        retain the full differential algorithm.
        """
        if len(path) != 1 or not isinstance(path[0], str) or path[0].startswith("@"):
            return None
        if not isinstance(self._document, dict) or "@type" in self._document:
            return None
        if not self._flat_context or not self._unscoped_resources():
            return None
        context = self._document.get("@context", {})
        values = original if isinstance(original, list) else [original]
        if any(isinstance(value, (dict, list)) for value in values):
            return None
        projection = {"@context": context, path[0]: original}
        if "@id" in self._document:
            projection["@id"] = self._document["@id"]
        projected = occurrences(self._expand(projection))
        selected = tuple(
            item for item in projected if not item.predicate.startswith("@")
        )
        if not selected:
            return None
        selected_counts: Counter[tuple[object, ...]] = Counter()
        for item in selected:
            selected_counts[item.identity] += 1
            if selected_counts[item.identity] > self._counts[item.identity]:
                return None
        if len({(item.predicate, item.reverse, item.graph) for item in selected}) != 1:
            return None
        return ObservedUsage(
            path, tuple(replace(item, location=location(path)) for item in selected)
        )

    def select(self, path: SourcePath) -> ObservedUsage:
        """Select a contribution without assuming stable expansion indexes."""
        if path in self._cache:
            return self._cache[path]
        self._budget.consume("states")
        original = at_path(self._document, path)
        if "@context" in path:
            raise InputError("Selections inside context definitions are not usages")
        for size in range(1, len(path)):
            prefix = path[:size]
            ancestor = at_path(self._document, prefix)
            if isinstance(ancestor, dict) and "@value" in ancestor:
                raise InputError("Selections inside literal values are not usages")
            prefix_usage = (
                self.select(prefix)
                if isinstance(ancestor, (dict, list))
                else self._cache.get(prefix)
            )
            if prefix_usage is not None and any(
                isinstance(item.value, dict)
                and item.value.get("@type") == "@json"
                and item.value.get("@value") == ancestor
                for item in prefix_usage.occurrences
            ):
                raise InputError(
                    "Selections inside opaque JSON literals are not usages"
                )
        projected = self._flat_projection(path, original)
        if projected is not None:
            self._cache[path] = projected
            return projected
        reduced = deepcopy(self._document)
        parent = at_path(reduced, path[:-1])
        key = path[-1]
        if isinstance(parent, dict) and isinstance(key, str):
            del cast(dict[str, object], parent)[key]
        elif isinstance(parent, list) and type(key) is int:
            del cast(list[object], parent)[key]
        else:
            raise KeyError(key)
        try:
            remaining = occurrences(self._expand(reduced))
        except LimitExceeded:
            raise
        except InputError:
            usage = ObservedUsage(
                path,
                limitation="The valid usage cannot be isolated safely "
                "through public expansion",
            )
            self._cache[path] = usage
            return usage
        counts = Counter(item.identity for item in remaining)
        for identity, count in counts.items():
            if 0 < count < self._counts[identity]:
                contributions = {
                    (item.value_json, item.list_position)
                    for item in self._original
                    if item.identity == identity
                }
                if len(contributions) > 1:
                    usage = ObservedUsage(
                        path,
                        limitation=(
                            "Anonymous contributions cannot be distinguished safely"
                        ),
                    )
                    self._cache[path] = usage
                    return usage
        lost: list[ObservedOccurrence] = []
        for item in self._original:
            if counts[item.identity]:
                counts[item.identity] -= 1
            else:
                lost.append(item)
        if not lost:
            usage = ObservedUsage(
                path,
                dropped=original not in (None, [], {}),
                empty=original in (None, [], {}),
            )
        else:
            if not isinstance(original, dict):
                original_counts = Counter(item.identity for item in self._original)
                added: list[ObservedOccurrence] = []
                for item in remaining:
                    if original_counts[item.identity]:
                        original_counts[item.identity] -= 1
                    else:
                        added.append(item)

                def rebind(item: ObservedOccurrence) -> tuple[object, ...]:
                    """Match unchanged facts when removing a node identifier."""
                    value = item.value
                    if isinstance(value, dict) and "@value" not in value:
                        value = {"node": True}
                    return (
                        item.predicate,
                        json.dumps(
                            value,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        item.reverse,
                        item.container,
                        item.depth,
                    )

                retained = []
                for item in lost:
                    match = next(
                        (
                            index
                            for index, new in enumerate(added)
                            if rebind(item) == rebind(new)
                        ),
                        None,
                    )
                    if match is None:
                        retained.append(item)
                    else:
                        added.pop(match)
                lost = retained
            if not lost:
                usage = ObservedUsage(
                    path,
                    limitation="The selection changes identity without "
                    "an attributable contribution",
                )
                self._cache[path] = usage
                return usage
            depth = min(item.depth for item in lost)
            selected = [item for item in lost if item.depth == depth]
            nonkeywords = [
                item for item in selected if not item.predicate.startswith("@")
            ]
            types = [item for item in selected if item.predicate == "@type"]
            if types and not isinstance(original, dict):
                selected = types
            elif nonkeywords:
                selected = nonkeywords
            if (
                len({(item.predicate, item.reverse, item.graph) for item in selected})
                != 1
            ):
                usage = ObservedUsage(
                    path,
                    limitation="Selected usage has ambiguous semantic contributions",
                )
            else:
                usage = ObservedUsage(
                    path,
                    tuple(replace(item, location=location(path)) for item in selected),
                )
        self._cache[path] = usage
        opaque = any(
            isinstance(item.value, dict)
            and item.value.get("@type") == "@json"
            and item.value.get("@value") == original
            for item in usage.occurrences
        )
        if isinstance(original, dict) and usage.occurrences and not opaque:
            attributed = list(usage.occurrences)
            for key in original:
                if key in {
                    "@context",
                    "@list",
                    "@value",
                    "@type",
                    "@id",
                    "@set",
                    "@reverse",
                    "@graph",
                    "@included",
                }:
                    continue
                member = self.select((*path, key))
                for occurrence in member.occurrences:
                    if occurrence.predicate != usage.occurrences[0].predicate:
                        continue
                    match = next(
                        (
                            index
                            for index, existing in enumerate(attributed)
                            if existing.identity == occurrence.identity
                            and existing.location == location(path)
                        ),
                        None,
                    )
                    if match is not None:
                        attributed[match] = occurrence
            usage = replace(usage, occurrences=tuple(attributed))
            self._cache[path] = usage
        return usage
