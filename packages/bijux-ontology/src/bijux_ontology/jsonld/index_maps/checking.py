"""Processor-qualified index origin, separate from RDF equality."""

import json
from collections import Counter
from collections.abc import Sequence
from typing import cast

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, IssueCollector
from bijux_ontology.json.paths import bounded_location, location
from bijux_ontology.jsonld.evidence.occurrences import ObservedOccurrence, ObservedUsage
from bijux_ontology.jsonld.expectations import compare_usage
from bijux_ontology.jsonld.selectors.checking import resolve_selection
from bijux_ontology.jsonld.selectors.types import KeyedItems, SourceSelector
from bijux_ontology.jsonld.usages.checking import PreparedUsages
from bijux_ontology.rdf.identifiers import absolute_iri

from .types import (
    IndexMapCheck,
    IndexMapExpectation,
    IndexMapObservation,
    _capture,
    _text,
)


def _value(item: ObservedOccurrence, budget: Budget) -> dict[str, object] | None:
    """Decode owned expanded value evidence only after charging its allocation."""
    _text(item.value_json, budget)
    value = item.value
    return cast(dict[str, object], value) if isinstance(value, dict) else None


def _anchor(item: ObservedOccurrence, budget: Budget) -> tuple[object, ...] | None:
    """Use explicit named roles and exact literals, never expansion-local labels."""
    budget.consume("work")
    if (
        item.predicate.startswith("@")
        or item.subject is None
        or item.subject.startswith("_:")
        or item.anonymous_graph
        or item.graph is not None
        and item.graph.startswith("_:")
    ):
        return None
    try:
        absolute_iri(item.subject)
        if item.graph is not None:
            absolute_iri(item.graph)
    except InputError:
        return None
    value = _value(item, budget)
    if value is None:
        return None
    if "@value" in value:
        value.pop("@index", None)
        identity: object = json.dumps(value, sort_keys=True, ensure_ascii=False)
    else:
        identity = value.get("@id")
        if not isinstance(identity, str):
            return None
        try:
            absolute_iri(identity)
        except InputError:
            return None
    return (
        item.predicate,
        item.subject,
        item.graph,
        item.reverse,
        item.container,
        identity,
    )


def _list_groups(
    items: tuple[ObservedOccurrence, ...], budget: Budget
) -> tuple[tuple[ObservedOccurrence, ...], ...]:
    """Read existing list groups in emitted order, retaining original boundaries."""
    groups: dict[int, list[ObservedOccurrence]] = {}
    for item in items:
        budget.consume("work")
        if item.container != "list" or item.list_id is None:
            continue
        budget.consume("states")
        groups.setdefault(item.list_id, []).append(item)
    return tuple(tuple(group) for group in groups.values())


def _list_signature(
    items: tuple[ObservedOccurrence, ...], budget: Budget
) -> tuple[object, ...] | None:
    """Identify complete lists by named scope and processed member sequence."""
    budget.consume("work", len(items))
    if not items:
        return None
    first = items[0]
    if (
        first.subject is None
        or first.subject.startswith("_:")
        or first.anonymous_graph
        or first.graph is not None
        and first.graph.startswith("_:")
    ):
        return None
    try:
        absolute_iri(first.subject)
        if first.graph is not None:
            absolute_iri(first.graph)
    except InputError:
        return None
    if any(item.empty for item in items):
        if len(items) != 1 or first.list_length != 0:
            return None
        return (first.subject, first.graph, first.predicate, first.reverse, ())
    members = []
    for position, item in enumerate(items):
        budget.consume("work")
        if (
            item.list_position != position
            or item.list_length != len(items)
            or (item.subject, item.graph, item.predicate, item.reverse)
            != (first.subject, first.graph, first.predicate, first.reverse)
        ):
            return None
        if _anchor(item, budget) is None:
            return None
        members.append(item.value_json)
    return (first.subject, first.graph, first.predicate, first.reverse, tuple(members))


def _origin(
    usage: ObservedUsage,
    original: tuple[ObservedOccurrence, ...],
    control: tuple[ObservedOccurrence, ...],
    sentinel: str,
    index_property: str | None,
    budget: Budget,
) -> tuple[bool | None, str]:
    """Require unique original/control witnesses before comparing index effects."""
    selected = usage.occurrences
    if not selected or usage.limitation:
        return None, usage.limitation or "No attributable index-map value"
    budget.consume("work", len(selected))
    if any(item.container == "list" for item in selected):
        if index_property is not None:
            return (
                None,
                "Property-based indexes require node objects, not list wrappers",
            )
        signature = _list_signature(selected, budget)
        if signature is None:
            return None, "List index origin requires a complete named-scope list"
        before_groups = [
            group
            for group in _list_groups(original, budget)
            if _list_signature(group, budget) == signature
        ]
        after_groups = [
            group
            for group in _list_groups(control, budget)
            if _list_signature(group, budget) == signature
        ]
        if len(before_groups) != 1 or len(after_groups) != 1:
            return (
                None,
                "Parallel or indistinguishable list groups lack unique correspondence",
            )
        if all(item.list_index == sentinel for item in after_groups[0]):
            return True, ""
        return False, "The source key does not supply the list wrapper index"
    original_counts: Counter[tuple[object, ...]] = Counter()
    control_by_anchor: dict[tuple[object, ...], list[ObservedOccurrence]] = {}
    for item in original:
        anchor = _anchor(item, budget)
        if anchor is not None:
            budget.consume("states")
            original_counts[anchor] += 1
    for item in control:
        anchor = _anchor(item, budget)
        if anchor is not None:
            budget.consume("states")
            control_by_anchor.setdefault(anchor, []).append(item)
    for item in selected:
        anchor = _anchor(item, budget)
        if (
            anchor is None
            or original_counts[anchor] != 1
            or len(control_by_anchor.get(anchor, ())) != 1
        ):
            return (
                None,
                "Merged or anonymous records lack unique source correspondence",
            )
        before = _value(item, budget)
        after = _value(control_by_anchor[anchor][0], budget)
        assert before is not None and after is not None
        if index_property is None:
            after_index = after.pop("@index", None)
            before.pop("@index", None)
            if before != after:
                return (
                    False,
                    "Key renaming changes a contribution beyond plain index metadata",
                )
            if after_index != sentinel:
                return False, "The source key does not supply plain index metadata"
        else:
            if any(key in before for key in ("@value", "@graph", "@list")):
                return None, "Property-based indexes require supported node objects"
            old_values, new_values = (
                before.pop(index_property, []),
                after.pop(index_property, []),
            )
            if (
                before != after
                or not isinstance(old_values, list)
                or not isinstance(new_values, list)
            ):
                return (
                    None,
                    "Cannot isolate the declared property-index contribution",
                )
            budget.consume("work", len(old_values) + len(new_values))
            budget.consume("states", len(old_values) + len(new_values))
            old_counts = Counter(
                json.dumps(value, sort_keys=True, ensure_ascii=False)
                for value in old_values
            )
            new_counts = Counter(
                json.dumps(value, sort_keys=True, ensure_ascii=False)
                for value in new_values
            )
            added, removed = (
                list((new_counts - old_counts).elements()),
                list((old_counts - new_counts).elements()),
            )
            if len(added) != 1 or len(removed) > 1:
                return (
                    False,
                    "The key does not supply one declared property-index contribution",
                )
            value = json.loads(added[0])
            if (
                not isinstance(value, dict)
                or value.get("@id", value.get("@value")) != sentinel
            ):
                return (
                    False,
                    "The processed key contribution differs from the control key",
                )
    return True, ""


def _contained(usage: ObservedUsage, parent: ObservedUsage, budget: Budget) -> bool:
    """Compare original occurrences within the same prepared identity boundary."""
    budget.consume("work", len(parent.occurrences) + len(usage.occurrences))
    budget.consume("states", len(parent.occurrences))
    for item in (*parent.occurrences, *usage.occurrences):
        _text(item.value_json, budget)
    scope = {(item.identity, item.value_json) for item in parent.occurrences}
    return all((item.identity, item.value_json) in scope for item in usage.occurrences)


def check_index_maps(
    prepared: PreparedUsages, expectations: Sequence[IndexMapExpectation]
) -> IndexMapCheck:
    """Check raw key facts and required processor-established index associations.

    Comparisons reuse the prepared operation's budget and controlled resources.
    Returned observations belong to its original expansion. Every key is a
    source occurrence; repeated effective IDs never merge its application keys.
    Unknown correspondence remains required, even after exact keys were checked.
    """
    if not isinstance(prepared, PreparedUsages):
        raise InputError("Index contracts require PreparedUsages")
    budget = prepared._budget
    issues = IssueCollector(budget)
    children: list[Report] = []
    observations: list[IndexMapObservation] = []
    checked = 0
    complete = True
    try:
        rules = _capture(expectations, budget)
        if not rules or any(
            not isinstance(rule, IndexMapExpectation) for rule in rules
        ):
            raise InputError("Select nonempty typed IndexMapExpectation values")
        for rule in rules:
            declared_keys = rule.keys or ()
            budget.consume("states", len(declared_keys) + len(rule.bindings))
            budget.consume("work", len(declared_keys) + len(rule.bindings))
            for text in declared_keys:
                _text(text, budget)
            if rule.index_property is not None:
                _text(rule.index_property, budget)
            for binding in rule.bindings:
                for binding_text in (binding.key, binding.identifier, binding.graph):
                    if binding_text is not None:
                        _text(binding_text, budget)
            where = bounded_location(rule.mapping.path, budget)
            parent = prepared.observe(rule.mapping.path)
            if not parent.limitation:
                issues.extend(compare_usage(rule.mapping, parent, budget=budget))
            selection = resolve_selection(
                SourceSelector((KeyedItems(),), start=rule.mapping.path, minimum=0),
                prepared._document,
                prepared.entities,
                prepared.observe,
                budget,
                require_usage=False,
            )
            children.append(selection.report)
            complete &= selection.report.complete
            keys_complete = selection.report.complete
            keys = tuple(cast(str, path[-1]) for path in selection.paths)
            for key in keys:
                _text(key, budget)
            if keys_complete and rule.keys is not None:
                budget.consume("work", len(keys) + len(rule.keys))
                budget.consume("states", len(keys) + len(rule.keys))
                key_issues = IssueCollector(budget)
                if set(keys) != set(rule.keys):
                    key_issues.append(
                        Issue(
                            "context.index_keys_mismatch",
                            "Source index-map keys differ from the exact declared keys",
                            where,
                            expected=str(rule.keys),
                            actual=str(keys),
                        )
                    )
                children.append(
                    Report(
                        f"index-keys:{where}",
                        1,
                        tuple(key_issues),
                        regime="jsonld-document",
                    )
                )
            for path in selection.paths:
                key = cast(str, path[-1])
                usage, original, control, sentinel, limitation = (
                    prepared._index_derivative(path)
                )
                if limitation is not None or parent.limitation:
                    status, reason = (
                        None,
                        limitation or parent.limitation or "Unresolved map scope",
                    )
                elif not _contained(usage, parent, budget):
                    status, reason = (
                        None,
                        "Entry contributions lack attribution to the containing map",
                    )
                else:
                    assert sentinel is not None
                    status, reason = _origin(
                        usage, original, control, sentinel, rule.index_property, budget
                    )
                budget.consume("states")
                observations.append(
                    IndexMapObservation(key, path, usage, status is True)
                )
                if status is not True:
                    issues.append(
                        Issue(
                            "context.index_origin_mismatch"
                            if status is False
                            else "context.index_unverified",
                            reason,
                            location(path),
                            kind="finding" if status is False else "unverified",
                        )
                    )
                    complete &= status is not None
                if status is True:
                    budget.consume("work", len(rule.bindings) + len(usage.occurrences))
                    budget.consume("states", len(rule.bindings))
                    expected = {
                        (binding.identifier, binding.graph)
                        for binding in rule.bindings
                        if binding.key == key
                    }
                    if expected:
                        budget.consume("states", len(usage.occurrences))
                        actual = {
                            (item.traits.identifier, item.graph)
                            for item in usage.occurrences
                            if not item.anonymous_graph
                        }
                        if actual != expected:
                            issues.append(
                                Issue(
                                    "context.index_binding_mismatch",
                                    "Map key identifies different records or graphs",
                                    location(path),
                                    expected=str(expected),
                                    actual=str(actual),
                                )
                            )
                checked += 1
            budget.consume("work", len(keys) + len(rule.bindings))
            budget.consume("states", len(keys))
            key_set = set(keys)
            for binding in rule.bindings:
                if binding.key not in key_set and keys_complete:
                    issues.append(
                        Issue(
                            "context.index_binding_missing",
                            "Required indexed record key is absent",
                            where,
                            expected=binding.key,
                        )
                    )
            if not selection.paths:
                complete = False
                issues.append(
                    Issue(
                        "context.index_unverified",
                        "No processed entry establishes index-map origin",
                        where,
                        kind="unverified",
                    )
                )
    except LimitExceeded as error:
        complete = False
        issues.exhausted(str(error))
    return IndexMapCheck(
        tuple(observations),
        Report(
            "jsonld.index_maps",
            checked,
            tuple(issues),
            requested=checked if complete else checked + 1,
            complete=complete,
            children=tuple(children),
            regime="jsonld-document",
        ),
    )
