"""Proven shared projections of primitive fields on uniquely named subjects.

The maintained processor resolves headers and expands every projection. Its
results are reconciled to original occurrences; independent expansion-local
blank/graph/list tokens never become source correspondence evidence.
"""

from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import replace
from typing import cast

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget
from bijux_ontology.json.paths import SourcePath, at_path, location
from bijux_ontology.jsonld.observations import (
    ObservedOccurrence,
    ObservedUsage,
    occurrences,
)

from .resources import context_is_unscoped

Expansion = Callable[[object], list[dict[str, object]]]
_RESOLVE = "https://bijux.org/ontology/identity-resolution"
_HEADERS = frozenset({"@context", "@id", "@type", "@index"})


def _local_context(value: object, budget: Budget) -> bool:
    """Require local unscoped definitions without list or opaque coercions."""
    budget.consume("work")
    if value is None:
        return True
    if isinstance(value, list):
        return all(_local_context(item, budget) for item in value)
    if not isinstance(value, dict) or "@import" in value:
        return False
    if not context_is_unscoped(value):
        return False
    for definition in value.values():
        budget.consume("work")
        if isinstance(definition, dict):
            container = definition.get("@container")
            if (
                definition.get("@type") == "@json"
                or container == "@list"
                or isinstance(container, list)
                and "@list" in container
            ):
                return False
    return _RESOLVE not in value


class NamedProjections:
    """Cache only complete associations proven by the original owned snapshot."""

    def __init__(
        self,
        document: object,
        expanded: tuple[ObservedOccurrence, ...],
        expand: Expansion,
        budget: Budget,
        unscoped_resources: Callable[[], bool],
    ) -> None:
        self._document = document
        self._original = expanded
        self._expand = expand
        self._budget = budget
        self._unscoped_resources = unscoped_resources
        self._subjects: dict[SourcePath, str] | None = None
        self._facts: dict[tuple[object, ...], list[ObservedOccurrence]] = defaultdict(
            list
        )
        self._identities: dict[tuple[object, ...], set[tuple[object, ...]]] = (
            defaultdict(set)
        )
        self._prepared = False

    def _prepare(self) -> bool:
        """Prove global context safety and resolve canonical header IRIs once."""
        if self._prepared:
            return self._subjects is not None
        self._prepared = True
        if (
            not isinstance(self._document, dict)
            or not _local_context(self._document.get("@context", {}), self._budget)
            or not self._unscoped_resources()
        ):
            return False
        described: list[tuple[SourcePath, str]] = []
        stack: list[tuple[object, SourcePath]] = [(self._document, ())]
        while stack:
            value, path = stack.pop()
            self._budget.consume("work")
            if isinstance(value, dict):
                if (
                    path
                    and "@context" in value
                    or any(key in value for key in ("@value", "@list", "@set"))
                ):
                    return False
                identifier = value.get("@id")
                if (
                    isinstance(identifier, str)
                    and not identifier.startswith("_:")
                    and set(value) - {"@id", "@index", "@context"}
                ):
                    described.append((path, identifier))
                stack.extend(
                    (child, (*path, key))
                    for key, child in value.items()
                    if key != "@context"
                )
            elif isinstance(value, list):
                stack.extend(
                    (child, (*path, index)) for index, child in enumerate(value)
                )
        if not described:
            return False
        context = self._document.get("@context", {})
        request = {
            "@context": [
                *(context if isinstance(context, list) else [context]),
                {_RESOLVE: {"@id": _RESOLVE, "@type": "@id", "@container": "@index"}},
            ],
            _RESOLVE: {str(index): raw for index, (_, raw) in enumerate(described)},
        }
        try:
            headers = occurrences(self._expand(request))
        except LimitExceeded:
            raise
        except InputError:
            return False
        resolved: dict[str, str] = {}
        for item in headers:
            self._budget.consume("work")
            if item.predicate != _RESOLVE or item.traits.identifier_kind != "iri":
                continue
            value = item.value
            if not isinstance(value, dict) or not isinstance(value.get("@index"), str):
                return False
            tag = value["@index"]
            if tag in resolved or item.traits.identifier is None:
                return False
            resolved[tag] = item.traits.identifier
        by_identity: dict[str, list[SourcePath]] = defaultdict(list)
        for index, (path, _) in enumerate(described):
            identifier = resolved.get(str(index))
            if identifier is not None:
                by_identity[identifier].append(path)
        self._subjects = {
            paths[0]: identifier
            for identifier, paths in by_identity.items()
            if len(paths) == 1
        }
        for item in self._original:
            self._budget.consume("work")
            self._facts[item.identity].append(item)
            self._identities[item.identity].add(
                (
                    item.value_json,
                    item.subject_identity,
                    item.graph_identity,
                    item.identifier_identity,
                    item.list_id,
                    item.list_position,
                )
            )
        return True

    def _project(
        self,
        value: object,
        branches: dict[object, object],
        *,
        include_values: bool = True,
    ) -> object:
        """Retain exact selected values, ancestor structure and native headers."""
        self._budget.consume("work")
        if None in branches:
            return deepcopy(value)
        if isinstance(value, dict):
            result = {}
            for key, child in value.items():
                self._budget.consume("work")
                if key in branches:
                    branch = cast(dict[object, object], branches[key])
                    if include_values or None not in branch:
                        result[key] = self._project(
                            child, branch, include_values=include_values
                        )
                elif key in _HEADERS:
                    result[key] = deepcopy(child)
            return result
        if isinstance(value, list):
            result_list = []
            for index, child in enumerate(value):
                self._budget.consume("work")
                if index in branches:
                    result_list.append(
                        self._project(
                            child,
                            cast(dict[object, object], branches[index]),
                            include_values=include_values,
                        )
                    )
            return result_list
        raise InputError("A projection path does not identify a source container")

    def select(self, paths: Sequence[SourcePath]) -> dict[SourcePath, ObservedUsage]:
        """Resolve cohorts by raw key; unsupported or ambiguous members fall back."""
        candidates: dict[str, list[SourcePath]] = defaultdict(list)
        for path in paths:
            self._budget.consume("work")
            if (
                not path
                or not isinstance(path[-1], str)
                or path[-1].startswith("@")
                or "@context" in path
            ):
                continue
            try:
                parent = at_path(self._document, path[:-1])
                value = at_path(self._document, path)
            except (KeyError, IndexError):
                continue
            values = value if isinstance(value, list) else [value]
            if (
                not isinstance(parent, dict)
                or not isinstance(parent.get("@id"), str)
                or any(isinstance(member, (dict, list)) for member in values)
            ):
                continue
            candidates[path[-1]].append(path)
        candidates = {
            key: selected for key, selected in candidates.items() if len(selected) > 1
        }
        if not candidates or not self._prepare():
            return {}
        result: dict[SourcePath, ObservedUsage] = {}
        assert self._subjects is not None
        for selected in candidates.values():
            cohort = [path for path in selected if path[:-1] in self._subjects]
            if len(cohort) < 2:
                continue
            branches: dict[object, object] = {}
            for path in cohort:
                current = branches
                for key in path:
                    self._budget.consume("work")
                    current = cast(dict[object, object], current.setdefault(key, {}))
                current[None] = True
            try:
                projected = occurrences(
                    self._expand(self._project(self._document, branches))
                )
                control = occurrences(
                    self._expand(
                        self._project(self._document, branches, include_values=False)
                    )
                )
            except LimitExceeded:
                raise
            except InputError:
                continue
            # Ancestor edges belong to the retained structure, not each leaf.
            # A maintained header-only control removes those shared contributions.
            remaining: Counter[tuple[object, ...]] = Counter()
            for item in control:
                self._budget.consume("work")
                remaining[item.identity] += 1
            projected_counts = Counter(item.identity for item in projected)
            if any(
                count > projected_counts[identity]
                for identity, count in remaining.items()
            ):
                continue
            by_subject: dict[str, list[ObservedOccurrence]] = defaultdict(list)
            for item in projected:
                self._budget.consume("work")
                if remaining[item.identity]:
                    remaining[item.identity] -= 1
                elif item.subject is not None and not item.predicate.startswith("@"):
                    by_subject[item.subject].append(item)
            for path in cohort:
                actual = by_subject[self._subjects[path[:-1]]]
                original: list[ObservedOccurrence] = []
                valid = True
                counts: Counter[tuple[object, ...]] = Counter()
                for item in actual:
                    self._budget.consume("work")
                    matches = self._facts.get(item.identity, [])
                    counts[item.identity] += 1
                    if (
                        counts[item.identity] > len(matches)
                        or len(self._identities[item.identity]) != 1
                        or item.container != "unordered"
                    ):
                        valid = False
                        break
                    original.append(replace(matches[0], location=location(path)))
                if valid:
                    value = at_path(self._document, path)
                    result[path] = ObservedUsage(
                        path,
                        tuple(original),
                        dropped=not original and value not in (None, [], {}),
                        empty=not original and value in (None, [], {}),
                    )
        return result
