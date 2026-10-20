"""Bounded explicit two-table composition, without a general mapping reasoner."""

import threading
from collections.abc import Sequence

from rdflib.namespace import OWL, RDF, RDFS, SKOS

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.vocabulary.mappings.admission import (
    AdmittedMapping,
    admit_mapping,
    named_identifier,
)
from bijux_ontology.vocabulary.mappings.types import (
    MappingRead,
    MappingRow,
    MappingSet,
)

from .types import (
    ComposedMapping,
    MappingComposition,
    MappingCompositionPath,
    MappingCompositionRule,
    MappingOccurrence,
)

_EXACT = str(SKOS.exactMatch)
_IDENTITY = frozenset(
    str(term) for term in (OWL.sameAs, OWL.equivalentClass, OWL.equivalentProperty)
)
_SENTINEL = "https://w3id.org/sssom/NoTermFound"
_ENDPOINT_TYPES = frozenset(("", str(SKOS.Concept), str(RDFS.Resource)))
_PREDICATE_TYPES = frozenset(
    ("", str(RDF.Property), str(OWL.ObjectProperty), str(RDFS.Resource))
)


def _admit_input(
    value: MappingSet | MappingRead,
    budget: Budget,
    issues: IssueCollector,
    children: list[Report],
) -> AdmittedMapping | None:
    """Retain required composition producer truth before whole-table admission."""
    if isinstance(value, MappingRead):
        retain_report(value.report, budget, issues)
        children.append(value.report)
        if not value.report.verified or value.mapping_set is None:
            return None
    return admit_mapping(value, budget)


def _rule(value: MappingCompositionRule, budget: Budget) -> MappingCompositionRule:
    """Capture a typed chain before any relation-index hashing or comparison."""
    if not isinstance(value, MappingCompositionRule):
        raise InputError("Composition rules require typed records")
    left, right, result, identifier = (
        named_identifier(value.left, budget),
        named_identifier(value.right, budget),
        named_identifier(value.result, budget),
        named_identifier(value.identifier, budget),
    )
    bound_text(value.basis, budget)
    if value.basis not in ("skos", "application"):
        raise InputError("Select a SKOS or explicit application basis")
    budget.consume("states", 2)
    budget.consume("bytes", 256)
    return MappingCompositionRule(left, right, result, value.basis, identifier)


def _interpretable(
    row: MappingRow, extension_names: frozenset[str], metadata_uninterpreted: bool
) -> bool:
    """Named positive concept mappings exclude unsupported semantic qualifiers."""
    return (
        _SENTINEL not in (row.subject, row.object)
        and row.subject_type in _ENDPOINT_TYPES
        and row.object_type in _ENDPOINT_TYPES
        and row.predicate_type in _PREDICATE_TYPES
        and not metadata_uninterpreted
        and not any(
            text and (key in ("predicate_modifier", "other") or key in extension_names)
            for key, text in row.cells
        )
    )


def compose_mappings(
    left: MappingSet | MappingRead,
    right: MappingSet | MappingRead,
    *,
    intermediates: Sequence[str],
    rules: Sequence[MappingCompositionRule],
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: threading.Event | None = None,
) -> MappingComposition:
    """Compose selected positive named-endpoint rows under explicit chain rules.

    SKOS exactMatch transitivity is the only supported standard entailment.
    Broad, close, mixed and custom relation chains need an application basis.
    OWL identity/equivalence chains are unsupported even as application rules.
    No inverses, namespace equality or confidence arithmetic are invented.
    Every original row occurrence and selected two-row path remains distinct.
    No matching rule means a completed no_rule path, not an entailment.

    Uninterpreted selected qualifiers, literal/complex endpoint types and unmapped
    sentinel rows cannot yield candidates. Such required paths remain unknown.
    Passing MappingRead also retains actual producer failures and honest missing
    optional interchange capability. Composition of already-owned inputs itself
    needs no optional backend. Work, index, join fanout and result storage share
    the caller budget; finite loops honor the operation deadline/cancellation.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children: list[Report] = []
    first = second = None
    selected: list[str] = []
    approved: list[MappingCompositionRule] = []
    paths: list[MappingCompositionPath] = []
    results: list[ComposedMapping] = []
    checked = 0
    requested = 1
    complete = False
    try:
        raw_intermediates = capture(intermediates, operation)
        if not raw_intermediates:
            raise InputError("Select a nonempty intermediate identifier inventory")
        operation.consume("states", len(raw_intermediates))
        seen = set()
        for value in raw_intermediates:
            iri = named_identifier(value, operation)
            if iri not in seen:
                seen.add(iri)
                selected.append(iri)
        raw_rules = capture(rules, operation)
        if not raw_rules:
            raise InputError("Select a nonempty composition rule table")
        for value in raw_rules:
            approved.append(_rule(value, operation))
        admitted_left = _admit_input(left, operation, issues, children)
        first = admitted_left.mapping_set if admitted_left is not None else None
        admitted_right = _admit_input(right, operation, issues, children)
        second = admitted_right.mapping_set if admitted_right is not None else None
        if first is None or second is None:
            issues.append(
                Issue(
                    "mapping.composition_input_unverified",
                    "Both mapping producers must complete before composition",
                    kind="unverified",
                )
            )
        else:
            assert admitted_left is not None and admitted_right is not None
            left_metadata_unknown = bool(
                admitted_left.metadata.get("predicate_modifier")
                or admitted_left.metadata.get("other")
            )
            right_metadata_unknown = bool(
                admitted_right.metadata.get("predicate_modifier")
                or admitted_right.metadata.get("other")
            )
            operation.consume("states", len(selected) * 4)
            operation.consume("bytes", len(selected) * 256)
            extension_left = frozenset(first.uninterpreted)
            extension_right = frozenset(second.uninterpreted)
            index_left: dict[str, list[int]] = {iri: [] for iri in selected}
            index_right: dict[str, list[int]] = {iri: [] for iri in selected}
            for mapping, target_index, side in (
                (first, index_left, "object"),
                (second, index_right, "subject"),
            ):
                for position, row in enumerate(mapping.rows):
                    operation.consume("work")
                    key = row.object if side == "object" else row.subject
                    if key in target_index:
                        operation.consume("states")
                        operation.consume("bytes", 32)
                        target_index[key].append(position)
            product = 0
            for iri in selected:
                operation.consume("work")
                product += len(index_left[iri]) * len(index_right[iri])
            requested = len(selected) + product
            operation.consume("states", 3 * len(approved))
            operation.consume("bytes", 192 * len(approved))
            rule_index: dict[tuple[str, str], list[MappingCompositionRule]] = {}
            for index, rule in enumerate(approved):
                operation.consume("work", len(rule.left) + len(rule.right) + 1)
                if (
                    rule.basis == "skos"
                    and (rule.left, rule.right, rule.result) != (_EXACT, _EXACT, _EXACT)
                    or any(
                        term in _IDENTITY
                        for term in (rule.left, rule.right, rule.result)
                    )
                ):
                    issues.append(
                        Issue(
                            "mapping.composition_rule_unverified",
                            "This chain is outside the declared composition profile",
                            "rule:" + str(index),
                            kind="unverified",
                        )
                    )
                    continue
                key_pair = rule.left, rule.right
                group = rule_index.get(key_pair)
                if group is None:
                    group = []
                    rule_index[key_pair] = group
                group.append(rule)
            usable_rows: list[dict[int, bool]] = []
            for mapping, index_rows, extensions, metadata_unknown in (
                (first, index_left, extension_left, left_metadata_unknown),
                (second, index_right, extension_right, right_metadata_unknown),
            ):
                qualified: dict[int, bool] = {}
                usable_rows.append(qualified)
                for positions in index_rows.values():
                    for position in positions:
                        row = mapping.rows[position]
                        operation.consume("work", len(row.cells) + 1)
                        operation.consume("states")
                        operation.consume("bytes", 96)
                        qualified[position] = _interpretable(
                            row, extensions, metadata_unknown
                        )
            # Reserve paths independently of rule inventory size. Only admitted
            # rules matching usable selected rows can allocate derived outputs.
            operation.consume("work", product * 2)
            operation.consume("states", product * 6)
            operation.consume("bytes", product * 512)
            outputs = 0
            for iri in selected:
                for left_index in index_left[iri]:
                    for right_index in index_right[iri]:
                        left_row, right_row = (
                            first.rows[left_index],
                            second.rows[right_index],
                        )
                        operation.consume(
                            "work",
                            len(left_row.predicate) + len(right_row.predicate) + 1,
                        )
                        if usable_rows[0][left_index] and usable_rows[1][right_index]:
                            outputs += len(
                                rule_index.get(
                                    (left_row.predicate, right_row.predicate), ()
                                )
                            )
            operation.consume("work", outputs)
            operation.consume("states", outputs * 2)
            operation.consume("bytes", outputs * 256)
            for iri in selected:
                for left_index in index_left[iri]:
                    for right_index in index_right[iri]:
                        operation.consume("work")
                        left_row, right_row = (
                            first.rows[left_index],
                            second.rows[right_index],
                        )
                        usable = (
                            usable_rows[0][left_index] and usable_rows[1][right_index]
                        )
                        operation.consume(
                            "work", len(left_row.predicate) + len(right_row.predicate)
                        )
                        matching = rule_index.get(
                            (left_row.predicate, right_row.predicate), []
                        )
                        path = MappingCompositionPath(
                            iri,
                            MappingOccurrence(first, left_index),
                            MappingOccurrence(second, right_index),
                            "unverified"
                            if not usable
                            else "derived"
                            if matching
                            else "no_rule",
                        )
                        paths.append(path)
                        checked += 1
                        if not usable:
                            issues.append(
                                Issue(
                                    "mapping.composition_semantics_unverified",
                                    "Selected path semantics remain uninterpreted",
                                    "path:" + str(len(paths) - 1),
                                    kind="unverified",
                                )
                            )
                            continue
                        for rule in matching:
                            operation.consume("work", 0)
                            results.append(
                                ComposedMapping(
                                    left_row.subject,
                                    rule.result,
                                    right_row.object,
                                    path,
                                    rule,
                                )
                            )
                checked += 1
            operation.consume("work", 0)
            complete = True
    except (LimitExceeded, MemoryError, RecursionError):
        issues.exhausted("Mapping composition exceeded its operation allowance")
    except InputError:
        try:
            issues.append(
                Issue(
                    "mapping.composition_invalid",
                    "Composition requires owned inputs and explicit finite rules",
                    kind="input_error",
                )
            )
        except LimitExceeded:
            issues.exhausted("Mapping composition diagnostic allowance exhausted")
    return MappingComposition(
        first,
        second,
        tuple(selected),
        tuple(approved),
        tuple(paths),
        tuple(results),
        Report(
            "mapping.composition",
            checked,
            tuple(issues),
            requested=requested,
            complete=complete,
            children=tuple(children),
            regime="explicit-mapping-composition",
        ),
    )
