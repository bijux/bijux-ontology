"""Compare representative usages through independent controlled JSON-LD operations."""

import json
import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from typing import cast

from rdflib import BNode, Graph, Literal, Namespace
from rdflib.term import Node

from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_text

from ..errors import InputError, LimitExceeded
from ..reports import Issue, Report, combine_reports
from .checking import PreparedUsages, prepare_usages
from .conversion import DirectionPolicy, convert_to_rdf
from .observations import ObservedOccurrence, ObservedUsage, SourcePath, location
from .resources import DocumentLoader


def _resource(value: str | None, implicit: int | None = None) -> str | None:
    return (
        "_:"
        if implicit is not None or isinstance(value, str) and value.startswith("_:")
        else value
    )


def _signature(item: ObservedOccurrence) -> tuple[object, ...]:
    traits = item.traits
    return (
        item.predicate,
        item.reverse,
        item.container,
        _resource(item.subject, item.subject_identity),
        "_:" if item.anonymous_graph else _resource(item.graph),
        traits.kind,
        traits.datatype,
        traits.language.lower() if traits.language else None,
        traits.direction,
        _resource(traits.identifier, item.identifier_identity)
        if traits.identifier_kind == "blank" or item.identifier_identity is not None
        else traits.identifier,
        tuple(
            sorted("_:" if value.startswith("_:") else value for value in traits.types)
        ),
        item.empty,
    )


def _group(items: tuple[ObservedOccurrence, ...]) -> dict[str, tuple[object, ...]]:
    unordered: dict[str, set[tuple[object, ...]]] = defaultdict(set)
    lists: dict[str, dict[int | None, list[tuple[object, ...]]]] = defaultdict(dict)
    for item in items:
        signature = _signature(item)
        if item.empty:
            # Empty lists share rdf:nil; duplicate edges do not create new heads.
            unordered[item.location].add(
                ("list", ((item.list_position, item.list_length, signature),))
            )
        elif item.container == "list":
            lists[item.location].setdefault(item.list_id, []).append(
                (item.list_position, item.list_length, signature)
            )
        else:
            unordered[item.location].add(("unordered", signature))
    return {
        where: tuple(
            sorted(
                [
                    *unordered.get(where, ()),
                    *(
                        ("list", tuple(members))
                        for members in lists.get(where, {}).values()
                    ),
                ],
                key=repr,
            )
        )
        for where in unordered.keys() | lists.keys()
    }


def _display(values: tuple[object, ...]) -> str:
    return json.dumps(values, ensure_ascii=False)


def _structure(items: Sequence[ObservedOccurrence], budget: Budget) -> Graph:
    """Encode selected traits with one identity space for every resource role.

    Record nodes keep source associations and list membership separate from RDF
    resource identity. Literal contents remain outside this mapping/coercion
    comparison, as they are in the ordinary usage signatures.
    """
    graph = Graph()
    vocabulary = Namespace("urn:bijux:jsonld-usage:")
    nodes: dict[tuple[str, str | int], BNode] = {}
    lists: dict[tuple[str, int | None], BNode] = {}
    retained: set[tuple[object, ...]] = set()

    def resource(value: str | None, implicit: int | None = None) -> Node:
        """Preserve explicit and implicit node identities without merging them."""
        if implicit is not None:
            key: tuple[str, str | int] = ("implicit", implicit)
        elif value is not None and value.startswith("_:"):
            key = ("blank", value)
        else:
            return (
                Literal(value)
                if value is not None
                else Literal("absent", datatype=vocabulary["absent"])
            )
        if key not in nodes:
            budget.consume("states")
            nodes[key] = BNode()
        return nodes[key]

    for item in items:
        budget.consume("work")
        traits = item.traits
        signature = _signature(item)
        key = (
            item.location,
            signature,
            item.subject,
            item.graph,
            traits.identifier,
            tuple(sorted(traits.types)),
            item.subject_identity,
            item.graph_identity,
            item.identifier_identity,
            item.list_id if not item.empty else None,
            item.list_position,
        )
        if key in retained:
            continue
        retained.add(key)
        budget.consume("states")
        record = BNode()
        for role, value in (
            ("location", item.location),
            (
                "traits",
                _display(
                    (
                        item.predicate,
                        item.reverse,
                        item.container,
                        traits.kind,
                        traits.datatype,
                        traits.language.lower() if traits.language else None,
                        traits.direction,
                        item.empty,
                    )
                ),
            ),
            ("subject", resource(item.subject, item.subject_identity)),
            ("graph", resource(item.graph, item.graph_identity)),
            ("identifier", resource(traits.identifier, item.identifier_identity)),
        ):
            graph.add(
                (
                    record,
                    vocabulary[role],
                    value if isinstance(value, Node) else Literal(value),
                )
            )
        for node_type in traits.types:
            graph.add((record, vocabulary["type"], resource(node_type)))
        if item.container == "list":
            if item.empty:
                head: Node = vocabulary["empty-list"]
            else:
                head = lists.setdefault((item.location, item.list_id), BNode())
            graph.add((record, vocabulary["list"], head))
            graph.add((record, vocabulary["position"], Literal(item.list_position)))
            graph.add((record, vocabulary["length"], Literal(item.list_length)))
    return graph


def _associated_occurrences(
    token: str, usage: ObservedUsage, budget: Budget
) -> tuple[ObservedOccurrence, ...]:
    """Associate only a proven selected pointer and its concrete descendants.

    Labels establish caller-supplied correspondence; the original observations
    remain unchanged. Relative descendants preserve collection associations.
    """
    if not isinstance(usage.path, tuple) or not usage.path:
        raise InputError("Observed usage requires a nonempty source path")
    for part in usage.path:
        if type(part) not in (str, int) or isinstance(part, int) and part < 0:
            raise InputError("Observed paths require keys or nonnegative indexes")
        if isinstance(part, int) and part.bit_length() > min(
            10000, budget.limits.max_bytes
        ):
            raise LimitExceeded("Observed path index exceeds metadata bound")
        bound_text(str(part), budget)
    prefix = location(usage.path)
    bound_text(prefix, budget)
    result: list[ObservedOccurrence] = []
    for item in usage.occurrences:
        budget.consume("work")
        budget.consume("states")
        for text in (
            item.location,
            item.value_json,
            item.predicate,
            item.subject,
            item.graph,
            item.list_index,
        ):
            if text is not None:
                bound_text(text, budget)
        if item.location != prefix and not item.location.startswith(prefix + "/"):
            raise InputError("Observed occurrence is outside its selected pointer")
        suffix = item.location[len(prefix) :]
        # Reserve the combined UTF-8 representation before creating it.
        size = len(token.encode("utf-8")) + len(suffix.encode("utf-8"))
        if size > budget.limits.max_bytes:
            raise LimitExceeded("Associated usage location exceeds max_bytes")
        budget.consume("bytes", size)
        result.append(replace(item, location=token + suffix))
    return tuple(result)


def compare_observed_usages(
    pairs: Sequence[tuple[str, ObservedUsage, ObservedUsage]], *, budget: Budget
) -> Report:
    """Compare explicitly associated prepared evidence without expanding again.

    Each label identifies one caller-established pair, whose source paths may
    differ. Mapping and joint anonymous topology are checked; literal contents
    require a separate RDF or literal comparison. Original observations retain
    their real source locations and side-local identity tokens.
    """
    if not isinstance(pairs, Sequence) or isinstance(cast(object, pairs), (str, bytes)):
        raise InputError("Observed comparison requires a finite sequence")
    count = len(pairs)
    issues = IssueCollector(budget)
    checked = 0
    selected: tuple[list[ObservedOccurrence], list[ObservedOccurrence]] = ([], [])
    try:
        if count > budget.limits.max_states:
            raise LimitExceeded("Observed comparison exceeds max_states")
        for index in range(count):
            try:
                pair = pairs[index]
            except IndexError as error:
                raise InputError(
                    "Observed correspondence changed during capture"
                ) from error
            if not isinstance(pair, tuple) or len(pair) != 3:
                raise InputError("Observed correspondence requires labelled pairs")
            label, previous, current = pair
            budget.consume("work")
            budget.consume("states")
            if not isinstance(label, str) or not label:
                raise InputError("Observed correspondence requires nonempty labels")
            bound_text(label, budget)
            if not isinstance(previous, ObservedUsage) or not isinstance(
                current, ObservedUsage
            ):
                raise InputError("Observed comparison requires prepared usage evidence")
            checked += 1
            if (
                previous.limitation
                or current.limitation
                or previous.empty
                or current.empty
                or not previous.occurrences
                and not previous.dropped
                or not current.occurrences
                and not current.dropped
            ):
                message = (
                    previous.limitation
                    or current.limitation
                    or "No representative expanded value"
                )
                bound_text(message, budget)
                issues.append(
                    Issue(
                        "context.change_unverified", message, label, kind="unverified"
                    )
                )
                continue
            if previous.dropped or current.dropped:
                issues.append(
                    Issue(
                        "context.usage_dropped",
                        "A selected usage does not survive one or both expansions",
                        label,
                        expected=str(not previous.dropped),
                        actual=str(not current.dropped),
                    )
                )
                continue
            token = f"/{index}"
            old_items = _associated_occurrences(token, previous, budget)
            new_items = _associated_occurrences(token, current, budget)
            selected[0].extend(old_items)
            selected[1].extend(new_items)
            previous_groups, current_groups = _group(old_items), _group(new_items)
            for where in sorted(previous_groups.keys() | current_groups.keys()):
                budget.consume("work")
                old, new = previous_groups.get(where, ()), current_groups.get(where, ())
                if old != new:
                    expected, actual = _display(old), _display(new)
                    bound_text(expected, budget)
                    bound_text(actual, budget)
                    suffix = where[len(token) :]
                    size = len(label.encode("utf-8")) + len(suffix.encode("utf-8"))
                    if size > budget.limits.max_bytes:
                        raise LimitExceeded("Usage display location exceeds max_bytes")
                    budget.consume("bytes", size)
                    issues.append(
                        Issue(
                            "context.usage_meaning_changed",
                            "Observed mapping or effective value traits changed",
                            label + suffix,
                            expected=expected,
                            actual=actual,
                        )
                    )
        if selected[0] and not any(
            issue.code == "context.usage_meaning_changed" for issue in issues
        ):
            topology = compare_rdf(
                _structure(selected[0], budget),
                _structure(selected[1], budget),
                budget=budget,
            )
            if topology.outcome == "disagreement":
                issues.append(
                    Issue(
                        "context.usage_meaning_changed",
                        "Selected usages changed shared node or graph identity",
                        "",
                    )
                )
            elif not topology.verified:
                issues.extend(topology.issues)
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(
                Issue("context.change_unverified", str(error), kind="unverified")
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return Report(
        "context_changes",
        checked,
        tuple(issues),
        requested=count,
        complete=not any(issue.kind == "exhausted" for issue in issues),
        regime="jsonld",
        families=("representative_usages",),
    )


def compare_context_usages(
    before_document: object,
    after_document: object,
    selectors: Sequence[SourcePath],
    *,
    before_base: str | None = None,
    after_base: str | None = None,
    before_loader: DocumentLoader | None = None,
    after_loader: DocumentLoader | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> Report:
    """Compare actual selected mappings, coercions, directions and node scopes.

    Documents include their original contexts. Unused definition spelling is
    outside this representative scope. This operation does not establish complete
    serialization equivalence or compatibility for unobserved application data.
    All selected resource roles share one consistent blank-node correspondence.
    Prepared usages reuse their established expansion and owned resources;
    loader/base overrides for an already prepared side are rejected.
    """
    operation = budget or Budget(limits)
    if limits is not None and limits != operation.limits:
        raise InputError("Explicit limits differ from the operation budget")
    issues = IssueCollector(operation)
    checked = 0
    views: list[PreparedUsages | None] = []
    try:
        for name, document, base, loader in (
            ("before", before_document, before_base, before_loader),
            ("after", after_document, after_base, after_loader),
        ):
            try:
                if isinstance(document, PreparedUsages):
                    if base is not None or loader is not None:
                        raise InputError(
                            "Prepared usages cannot override their base or resources"
                        )
                    views.append(document)
                    continue
                views.append(
                    prepare_usages(
                        document,
                        base=base,
                        document_loader=loader,
                        budget=operation,
                    )
                )
            except LimitExceeded:
                raise
            except InputError as error:
                views.append(None)
                issues.append(
                    Issue(
                        f"context.{name}_input_error",
                        str(error),
                        kind="input_error",
                    )
                )
        if all(view is not None for view in views):
            before, after = views
            assert before is not None and after is not None
            pairs: list[tuple[str, ObservedUsage, ObservedUsage]] = []
            for path in selectors:
                operation.consume("work")
                try:
                    selected_budget = (
                        operation if budget is not None or limits is not None else None
                    )
                    previous = before.observe(path, budget=selected_budget)
                    current = after.observe(path, budget=selected_budget)
                except LimitExceeded:
                    raise
                except InputError as error:
                    issues.append(
                        Issue(
                            "context.invalid_path",
                            str(error),
                            location(path),
                            kind="input_error",
                        )
                    )
                    continue
                operation.consume("states")
                pairs.append((location(path), previous, current))
            compared = compare_observed_usages(pairs, budget=operation)
            checked = compared.checked
            issues.extend(compared.issues)
    except LimitExceeded as error:
        issues.exhausted(str(error))
    return Report(
        "context_changes",
        checked,
        tuple(issues),
        requested=len(selectors),
        complete=not any(issue.kind == "exhausted" for issue in issues),
        regime="jsonld",
        families=("representative_usages",),
    )


def compare_serializations(
    before_document: object,
    after_document: object,
    *,
    before_base: str | None = None,
    after_base: str | None = None,
    before_loader: DocumentLoader | None = None,
    after_loader: DocumentLoader | None = None,
    direction: DirectionPolicy | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout_seconds: float = 10.0,
) -> Report:
    """Compare representative RDF meaning after independently verified conversion.

    RDF graph identity, list order and consistent blank-node relationships use
    the maintained bounded dataset comparator. Every conversion scope remains
    inspectable. A lossy or incomplete side cannot establish preservation.
    """
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise InputError("Comparison timeout must be a finite positive number")
    operation = budget or Budget(limits)
    if limits is not None and limits != operation.limits:
        raise InputError("Explicit limits differ from the operation budget")
    reports: list[Report] = []
    prepared: list[PreparedSource] = []
    for name, document, base, loader in (
        ("before", before_document, before_base, before_loader),
        ("after", after_document, after_base, after_loader),
    ):
        converted_ready = False
        try:
            converted = convert_to_rdf(
                document,
                base=base,
                document_loader=loader,
                direction=direction,
                budget=operation,
            )
            reports.append(replace(converted.report, scope=f"serialization.{name}"))
            if not converted.report.verified:
                continue
            converted_ready = True
            prepared.append(
                prepare_rdf(
                    converted.nquads,
                    format="nquads",
                    limits=operation.limits,
                    budget=operation,
                )
            )
        except LimitExceeded as error:
            reports.append(
                Report(
                    f"serialization.{name}",
                    issues=(
                        Issue(
                            "execution.limit_exceeded",
                            str(error),
                            kind="exhausted",
                        ),
                    ),
                    requested=1,
                    complete=False,
                    regime="jsonld_to_rdf",
                )
            )
            break
        except InputError as error:
            reports.append(
                Report(
                    f"serialization.{name}",
                    issues=(
                        Issue(
                            "context.rdf_interoperability_unverified"
                            if converted_ready
                            else "context.input_error",
                            str(error),
                            kind="unverified" if converted_ready else "input_error",
                        ),
                    ),
                    requested=1,
                    complete=False,
                    regime="jsonld_to_rdf",
                )
            )
    if len(prepared) == 2 and all(report.verified for report in reports):
        reports.append(
            compare_rdf(
                prepared[0],
                prepared[1],
                limits=operation.limits,
                budget=operation,
                timeout_seconds=timeout_seconds,
            )
        )
    else:
        reports.append(
            Report(
                "serialization.comparison",
                issues=(
                    Issue(
                        "context.serialization_unverified",
                        "Both representative conversions must preserve their meaning",
                        kind="unverified",
                    ),
                ),
                requested=1,
                complete=False,
                regime="rdf-structure",
                families=("representative_serialization",),
            )
        )
    return combine_reports("serialization_changes", *reports)
