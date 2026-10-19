"""Associate selected source usages with owned expanded semantic contributions."""

import json
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import replace
from typing import cast

from bijux_ontology._jsonpaths import SourcePath, at_path, location
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.jsonld.observations import (
    ObservedOccurrence,
    ObservedUsage,
    occurrences,
)
from bijux_ontology.limits import Budget

from .projections import NamedProjections
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
        lists: dict[int, list[tuple[str, int | None]]] = defaultdict(list)
        for item in self._original:
            if item.list_id is not None:
                lists[item.list_id].append((item.value_json, item.list_position))
        self._lists = {key: tuple(members) for key, members in lists.items()}
        self._unscoped_resources = unscoped_resources
        self._projections = NamedProjections(
            self._document,
            self._original,
            self._expand,
            self._budget,
            self._unscoped_resources,
        )
        self._flat_context = (
            context_is_unscoped(document.get("@context", {}))
            if isinstance(document, dict)
            else False
        )

    def index_derivative(
        self, path: SourcePath
    ) -> tuple[
        ObservedUsage,
        tuple[ObservedOccurrence, ...],
        tuple[ObservedOccurrence, ...],
        str | None,
        str | None,
    ]:
        """Rename one source map key using this owned expansion/resource operation.

        Returned original observations retain their original identities. Control
        observations belong to a separate expansion; callers must not join its
        generated blank, graph or list labels to original identities.
        """
        usage = self.select(path)
        if usage.limitation or not usage.occurrences:
            return (
                usage,
                self._original,
                (),
                None,
                usage.limitation or "No attributable indexed contribution",
            )
        if not path or not isinstance(path[-1], str):
            raise InputError("Index origin requires a source map key")
        parent = at_path(self._document, path[:-1])
        if not isinstance(parent, dict) or path[-1] not in parent:
            raise InputError("Index origin requires an existing source map key")
        if "@context" in path:
            raise InputError("Context definitions are not index-map usages")
        self._budget.consume("resources")
        marker = "https://bijux.org/ontology/index-origin"
        for attempt in range(self._budget.limits.max_states):
            self._budget.consume("work")
            sentinel = f"{marker}/{attempt}"
            self._budget.consume(
                "work", sum(len(item.value_json) for item in usage.occurrences)
            )
            if sentinel not in parent and all(
                sentinel not in item.value_json for item in usage.occurrences
            ):
                break
        else:
            raise LimitExceeded("Cannot allocate a fresh bounded index probe key")
        changed = deepcopy(self._document)
        selected = at_path(changed, path[:-1])
        assert isinstance(selected, dict)
        selected[sentinel] = selected.pop(path[-1])
        try:
            control = occurrences(self._expand(changed), budget=self._budget)
        except LimitExceeded:
            raise
        except InputError:
            return (
                usage,
                self._original,
                (),
                sentinel,
                "The key-renaming control cannot be processed safely",
            )
        return usage, self._original, control, sentinel, None

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
        if (
            len(
                {
                    (item.predicate, item.reverse, item.graph, item.anonymous_graph)
                    for item in selected
                }
            )
            != 1
        ):
            return None
        return ObservedUsage(
            path, tuple(replace(item, location=location(path)) for item in selected)
        )

    def prepare_many(self, paths: Sequence[SourcePath]) -> None:
        """Warm proven shared cohorts without changing unsupported fallbacks."""
        if len(paths) > self._budget.limits.max_states:
            return
        pending = tuple(
            dict.fromkeys(path for path in paths if path not in self._cache)
        )
        if len(pending) < 2:
            return
        for path, usage in self._projections.select(pending).items():
            self._budget.consume("states")
            self._cache[path] = usage

    def select_many(self, paths: Sequence[SourcePath]) -> tuple[ObservedUsage, ...]:
        """Observe every requested path, preserving order and duplicate requests."""
        self.prepare_many(paths)
        return tuple(self.select(path) for path in paths)

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
                    (
                        item.value_json,
                        item.list_position,
                        self._lists.get(item.list_id)
                        if item.list_id is not None
                        else None,
                    )
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
                len(
                    {
                        (item.predicate, item.reverse, item.graph, item.anonymous_graph)
                        for item in selected
                    }
                )
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
                    "@language",
                    "@direction",
                    "@index",
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
