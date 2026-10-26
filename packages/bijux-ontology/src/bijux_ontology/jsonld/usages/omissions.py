"""Exclude explicitly selected whole properties while preserving the remainder.

Processor shape probes establish admissible syntax only. They never identify a
source occurrence from a drop callback or supply retained-content evidence.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import Budget
from bijux_ontology.json.paths import SourcePath, at_path, location
from bijux_ontology.json.values import own_json
from bijux_ontology.jsonld.evidence.occurrences import ObservedUsage
from bijux_ontology.jsonld.usages.selection import Expansion, UsageIndex

_GUARD = "https://bijux.org/ontology/omission-boundary/guard"
_MARKER = "https://bijux.org/ontology/omission-boundary/value"


@dataclass(frozen=True)
class OmissionProjection:
    """An owned retained document, applied exclusions and rejected policy evidence."""

    document: object
    paths: tuple[SourcePath, ...] = ()
    issues: tuple[Issue, ...] = ()


def project_omissions(
    document: object,
    paths: tuple[SourcePath, ...],
    expand: Expansion,
    observe: Callable[[SourcePath], ObservedUsage],
    budget: Budget,
) -> OmissionProjection:
    """Apply valid exclusions atomically; any rejected path retains original input.

    Targets are concrete existing object properties. Array indexes may locate
    their parents, but removing array elements is deliberately unsupported.
    Contexts, keywords/aliases and opaque literal interiors cannot be excluded.
    """
    if not isinstance(paths, tuple) or any(
        not isinstance(path, tuple)
        or any(
            type(key) not in (str, int) or isinstance(key, int) and key < 0
            for key in path
        )
        for path in paths
    ):
        raise InputError("allowed_omissions requires concrete key/index path tuples")
    selected = tuple(dict.fromkeys(paths))
    issues: list[Issue] = []

    def reject(path: SourcePath, message: str, *, ambiguous: bool = False) -> None:
        """Retain the exact rejected policy path and its confidence boundary."""
        budget.consume("issues")
        issues.append(
            Issue(
                "context.omission_unverified"
                if ambiguous
                else "context.omission_invalid",
                message,
                location(path),
                kind="unverified" if ambiguous else "input_error",
            )
        )

    def owned(value: object) -> object:
        """Copy within the same input bounds before a processor probe mutates it."""
        return own_json(
            value,
            max_bytes=budget.limits.max_bytes,
            max_depth=budget.limits.max_depth,
            budget=budget,
        )

    def probe_index(source: object) -> UsageIndex:
        """Observe processor output under the shared operation budget."""
        expanded = expand(source)
        return UsageIndex(source, expand, expanded, budget=budget)

    def owners(usage: ObservedUsage) -> frozenset[tuple[object, ...]]:
        """Retain guard identity, graph ownership and direction across probes."""
        return frozenset(
            (
                item.predicate,
                item.subject,
                item.graph,
                item.anonymous_graph,
                item.reverse,
            )
            for item in usage.occurrences
        )

    def boundary(path: SourcePath) -> None:
        """Require a stable ordinary property before permitting removal."""
        try:
            original = observe(path)
            if original.limitation:
                reject(path, original.limitation, ambiguous=True)
                return
            if any(item.predicate.startswith("@") for item in original.occurrences):
                reject(path, "Keyword usages require separate omission semantics")
                return
            guarded = owned(document)
            parent = cast(dict[str, object], at_path(guarded, path[:-1]))
            if _GUARD in parent:
                reject(
                    path,
                    "The processor boundary guard conflicts with supplied input",
                    ambiguous=True,
                )
                return
            parent[_GUARD] = _GUARD
            guard_path = (*path[:-1], _GUARD)
            guard = probe_index(guarded).select(guard_path)
            if (
                guard.limitation
                or not guard.occurrences
                or any(
                    item.predicate != _GUARD or item.traits.kind != "literal"
                    for item in guard.occurrences
                )
            ):
                reject(
                    path,
                    "The parent is not an established ordinary node boundary",
                    ambiguous=True,
                )
                return
            signatures = []
            for index, value in enumerate((_MARKER, {"@id": _MARKER, _GUARD: _MARKER})):
                budget.consume("work")
                probe = owned(guarded)
                cast(dict[str, object], at_path(probe, path[:-1]))[
                    cast(str, path[-1])
                ] = value
                try:
                    indexer = probe_index(probe)
                except LimitExceeded:
                    raise
                except InputError:
                    if (
                        index != 0
                        or not original.occurrences
                        or not all(item.reverse for item in original.occurrences)
                    ):
                        raise
                    cast(dict[str, object], at_path(probe, path[:-1]))[
                        cast(str, path[-1])
                    ] = {"@id": _MARKER}
                    indexer = probe_index(probe)
                observed = indexer.select(path)
                owner = indexer.select(guard_path)
                if owner.limitation or owners(owner) != owners(guard):
                    reject(
                        path,
                        "The selected syntax changes the retained parent identity",
                        ambiguous=True,
                    )
                    return
                if observed.limitation:
                    reject(path, observed.limitation, ambiguous=True)
                    return
                if any(item.predicate.startswith("@") for item in observed.occurrences):
                    reject(path, "Keyword aliases require separate omission semantics")
                    return
                signatures.append(
                    frozenset(
                        (item.predicate, item.reverse, item.graph, item.anonymous_graph)
                        for item in observed.occurrences
                    )
                )
            ordinary = frozenset(
                (item.predicate, item.reverse, item.graph, item.anonymous_graph)
                for item in original.occurrences
            )
            if signatures[0] != signatures[1] or ordinary and ordinary != signatures[0]:
                reject(
                    path,
                    "The selected syntax has no stable ordinary property boundary",
                    ambiguous=True,
                )
        except LimitExceeded:
            raise
        except (InputError, KeyError, IndexError) as error:
            reject(path, "The selected syntax cannot be excluded safely: " + str(error))

    for index, path in enumerate(selected):
        budget.consume("work")
        if (
            not path
            or not isinstance(path[-1], str)
            or any(key == "@context" for key in path)
            or isinstance(path[-1], str)
            and path[-1].startswith("@")
        ):
            reject(
                path, "Exclusions require whole ordinary properties outside contexts"
            )
            continue
        try:
            parent = at_path(document, path[:-1])
            at_path(document, path)
        except (KeyError, IndexError):
            reject(path, "No supplied property matches the omission path")
            continue
        if not isinstance(parent, dict):
            reject(path, "Removing array elements requires separate omission semantics")
            continue
        overlap = False
        for other in selected[:index]:
            budget.consume("work")
            if path[: len(other)] == other or other[: len(path)] == path:
                reject(
                    path,
                    "Overlapping omission paths do not define independent properties",
                )
                overlap = True
                break
        if not overlap:
            boundary(path)
    if issues:
        return OmissionProjection(owned(document), issues=tuple(issues))
    retained = owned(document)
    for path in selected:
        budget.consume("work")
        del cast(dict[str, object], at_path(retained, path[:-1]))[cast(str, path[-1])]
    return OmissionProjection(retained, selected)
