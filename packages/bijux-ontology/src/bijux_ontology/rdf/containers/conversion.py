"""Checked copy-on-write conversion using the existing owned RDF list machinery."""

import threading
import time
from collections.abc import Sequence
from typing import Literal as Choice
from typing import cast

from rdflib import BNode, URIRef
from rdflib.namespace import RDF
from rdflib.term import Node

from bijux_ontology.absence import ABSENT
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.migrations.candidates import MigrationCheck, finish_migration
from bijux_ontology.rdf.migrations.lists.checking import _portable, _scope
from bijux_ontology.rdf.migrations.lists.planning import copy_chain
from bijux_ontology.rdf.migrations.lists.types import ListTarget, ListValue, _terms
from bijux_ontology.rdf.patches.applying import apply_rdf_patch
from bijux_ontology.rdf.patches.correspondence import compare_witnesses
from bijux_ontology.rdf.patches.planning import capture_source
from bijux_ontology.rdf.patches.types import (
    DatasetPatch,
    PatchBlank,
    PatchBlankBinding,
    PatchQuad,
    PatchUnavailable,
    PatchValue,
)
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.structure import iter_rdf_list
from bijux_ontology.rdf.transforms import Transformation

from .checking import check_containers
from .types import ContainerContract, ContainerConversion


def _postcondition(
    source: PreparedSource,
    patch: DatasetPatch,
    incoming: PatchQuad,
    expected: tuple[PatchValue, ...],
    operation: Operation,
) -> None:
    """Read the actual result list and compare exact terms via maintained bindings."""
    captured = capture_source(source, "converted:", operation)
    head = incoming.value
    for quad in patch.additions:
        operation.consume("work")
        if (quad.subject, quad.predicate, quad.graph) == (
            incoming.subject,
            incoming.predicate,
            incoming.graph,
        ):
            head = quad.value
            break
    operation.consume("states", len(expected) + 3)
    required = {
        t.token
        for t in (head, incoming.subject, incoming.graph, *expected)
        if isinstance(t, PatchBlank)
    }
    equal, pairs = compare_witnesses(
        patch.target, captured.witness, operation, required=tuple(required)
    )
    if not equal:
        raise PatchUnavailable(
            "The list output lacks its exact structural postcondition"
        )
    reverse: dict[str, BNode] = {}
    for node, ref in captured.owned.values():
        operation.consume("work")
        operation.consume("states")
        operation.consume("bytes", 32)
        reverse.setdefault(ref.token, node)

    def actual(value: PatchValue) -> Node:
        """Resolve only established required resource bindings inside this result."""
        if isinstance(value, PatchBlank):
            return reverse[pairs[value.token]]
        if isinstance(value, URIRef):
            return value
        raise InputError("List roots require RDF resources")

    graph_name: URIRef | BNode | None = None
    if incoming.graph is not None:
        if isinstance(incoming.graph, PatchBlank):
            for name in source._graph_names(budget=operation):
                operation.consume("work")
                if (
                    isinstance(name, BNode)
                    and captured.reference(name).token == pairs[incoming.graph.token]
                ):
                    graph_name = name
                    break
            if graph_name is None:
                raise PatchUnavailable(
                    "The output graph lacks its owned correspondence"
                )
        else:
            graph_name = incoming.graph
    graph = source._read(
        graph_name
        if graph_name is not None
        else "@default"
        if source.is_dataset
        else None,
        budget=operation,
    )
    actual_head = actual(head)
    if (actual(incoming.subject), incoming.predicate, actual_head) not in graph:
        raise PatchUnavailable("The chosen list incoming edge was not preserved")
    observed: list[PatchValue] = []
    for value in iter_rdf_list(
        graph, actual_head, max_items=operation.limits.max_states, budget=operation
    ):
        operation.consume("states")
        operation.consume("bytes", 16)
        observed.append(_portable(cast(ListValue, value), captured, operation))
    operation.consume("work", len(expected) + len(observed))
    operation.consume("states", len(expected))
    mapped = tuple(
        PatchBlank(pairs[t.token]) if isinstance(t, PatchBlank) else t for t in expected
    )
    if tuple(observed) != mapped:
        raise PatchUnavailable("The actual list differs from the chosen exact sequence")


def container_to_list(
    source: PreparedSource,
    target: ListTarget,
    contract: ContainerContract,
    *,
    expected_values: tuple[ListValue, ...],
    order: Choice["sequence", "numbered"] = "sequence",
    before_checks: Sequence[MigrationCheck] = (),
    after_checks: Sequence[MigrationCheck] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 10.0,
    cancel: threading.Event | None = None,
) -> ContainerConversion:
    """Replace one explicit container edge by a checked fresh RDF list.

    The original container, annotations and every unrelated edge remain intact.
    Sequence ordering is by positive ordinal. A bag requires explicit numbered
    output ordering; that is a consumer choice, never inferred RDF meaning.
    Expected members compare exact terms and preserve duplicates. Required blank
    output correspondence must be unique; ambiguous cases remain unknown.
    """
    if (
        not isinstance(source, PreparedSource)
        or not isinstance(target, ListTarget)
        or not isinstance(contract, ContainerContract)
    ):
        raise InputError(
            "Container conversion requires owned source, ListTarget and contract"
        )
    if order not in ("sequence", "numbered"):
        raise InputError("Select sequence or explicit numbered output order")
    if source.is_dataset and target.graph is ABSENT:
        raise InputError(
            "Dataset container conversion requires an explicit physical graph"
        )
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    graph_name = target.graph if target.graph is not ABSENT else None
    check = check_containers(
        source,
        contract,
        nodes=(target.head,),
        graph_name=graph_name
        if graph_name is not None
        else "@default"
        if source.is_dataset
        else None,
        budget=operation,
    )
    children: list[Report] = [check.report]
    patch = None
    transformed = None
    candidate = None
    checked = check.report.checked
    complete = False
    try:
        retain_report(check.report, operation, issues)
        if check.report.verified:
            if contract.kind == "bag" and order != "numbered":
                raise PatchUnavailable(
                    "Bag conversion requires explicit numbered output order"
                )
            captured = capture_source(source, "base:", operation)
            _, incoming = _scope(source, target, captured, operation)
            if len(check.inspection.records) != 1:
                raise PatchUnavailable(
                    "Conversion requires one exact container occurrence"
                )
            record = check.inspection.records[0]
            operation.consume("states", len(record.slots))
            values = tuple(
                _portable(cast(ListValue, slot.values[0]), captured, operation)
                for slot in record.slots
            )
            expected = tuple(
                _portable(value, captured, operation)
                for value in _terms(expected_values, operation)
            )
            operation.consume("work", len(values) + len(expected))
            if values != expected:
                issues.append(
                    Issue(
                        "rdf.container_conversion_rejected",
                        "Observed members differ from the explicit expected sequence",
                    )
                )
            else:
                patch = copy_chain(
                    captured.witness,
                    incoming,
                    values,
                    values,
                    operation,
                    force=bool(values or incoming.value != RDF.nil),
                )
                bindings: list[PatchBlankBinding] = []
                seen: set[str] = set()
                for node, ref in captured.owned.values():
                    operation.consume("work")
                    if ref.token not in seen:
                        operation.consume("states", 2)
                        operation.consume("bytes", 32)
                        seen.add(ref.token)
                        bindings.append(PatchBlankBinding(ref.token, node))
                remaining = operation.deadline - time.monotonic()
                if remaining <= 0:
                    raise LimitExceeded("Container conversion exceeded its deadline")
                transformed = apply_rdf_patch(
                    source,
                    patch,
                    blank_bindings=tuple(bindings),
                    budget=operation,
                    timeout=remaining,
                    cancel=cancel,
                )
                retain_report(transformed.report, operation, issues)
                children.append(transformed.report)
                if transformed.source is not None and transformed.report.verified:
                    _postcondition(
                        transformed.source, patch, incoming, values, operation
                    )
                    checked += 1
                    complete = True
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except (PatchUnavailable, ResourceError) as error:
        try:
            issues.append(
                Issue("rdf.container_unverified", str(error), kind="unverified")
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    raw = Report(
        "rdf.container_conversion",
        checked,
        tuple(issues),
        requested=checked if complete else checked + 1,
        complete=complete,
        children=tuple(children),
        regime="explicit-numbered-container-to-rdf-list",
    )
    if (
        complete
        and transformed is not None
        and transformed.source is not None
        and raw.verified
    ):
        candidate = finish_migration(
            source,
            Transformation(transformed.source, raw),
            patch,
            before_checks=before_checks,
            after_checks=after_checks,
            budget=operation,
        )
    return ContainerConversion(
        check, candidate, candidate.report if candidate is not None else raw
    )
