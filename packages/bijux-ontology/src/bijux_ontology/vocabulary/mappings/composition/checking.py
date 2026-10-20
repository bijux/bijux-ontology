"""Bounded explicit two-table composition, without a general mapping reasoner."""

import threading
from collections.abc import Sequence

from rdflib import Variable
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
from bijux_ontology.json.values import decode_json, object_value
from bijux_ontology.vocabulary.mappings.types import (
    MappingRead,
    MappingRow,
    MappingSet,
    _iri,
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


def _named(value: str, budget: Budget) -> str:
    """Reuse the mapping identifier qualifier without erasing variable kinds."""
    if isinstance(value, Variable):
        raise InputError("Composition requires named identifiers")
    return _iri(value, budget)


def _input(
    value: MappingSet | MappingRead,
    budget: Budget,
    issues: IssueCollector,
    children: list[Report],
) -> tuple[MappingSet | None, bool]:
    """Requalify every retained input field before indexing even unmatched rows."""
    if isinstance(value, MappingRead):
        retain_report(value.report, budget, issues)
        children.append(value.report)
        if not value.report.verified or value.mapping_set is None:
            return None, False
        value = value.mapping_set
    if not isinstance(value, MappingSet):
        raise InputError("Composition requires owned mapping inputs")
    for payload in (value.source, value._content):
        if len(payload) > budget.limits.max_bytes:
            raise LimitExceeded("Mapping source exceeds max_bytes")
        budget.consume("bytes", len(payload))
        budget.consume("work", len(payload))
    for text in (
        value.metadata_json,
        value.backend_version,
        value.schema_version,
        value.supplied_id,
        value.supplied_license,
    ):
        bound_text(text, budget)
    for key, iri in value.prefixes:
        budget.consume("states")
        bound_text(key, budget)
        _named(iri, budget)
    budget.consume("states", len(value.columns) + len(value.uninterpreted))
    for names in (value.columns, value.uninterpreted):
        for text in names:
            bound_text(text, budget)
    budget.consume("states", len(value.rows))
    budget.consume("bytes", 64 + 32 * len(value.rows))
    for row in value.rows:
        budget.consume("work")
        for iri in (row.subject, row.predicate, row.object, row.justification):
            _named(iri, budget)
        for kind in (row.subject_type, row.object_type, row.predicate_type):
            if kind:
                _named(kind, budget)
        budget.consume("states", len(row.cells))
        for key, text in row.cells:
            bound_text(key, budget)
            bound_text(text, budget)
    # Metadata interpretation uses the existing strict decoder. A conservative
    # character-sized node reservation bounds its allocation before parsing.
    budget.consume("states", len(value.metadata_json))
    budget.consume("work", len(value.metadata_json))
    budget.consume("bytes", 4 * len(value.metadata_json))
    metadata = object_value(
        decode_json(
            value.metadata_json,
            max_bytes=budget.limits.max_bytes,
            max_depth=budget.limits.max_depth,
        ),
        location="mapping metadata",
    )
    return value, bool(metadata.get("predicate_modifier") or metadata.get("other"))


def _rule(value: MappingCompositionRule, budget: Budget) -> MappingCompositionRule:
    """Capture a typed chain before any relation-index hashing or comparison."""
    if not isinstance(value, MappingCompositionRule):
        raise InputError("Composition rules require typed records")
    left, right, result, identifier = (
        _named(value.left, budget),
        _named(value.right, budget),
        _named(value.result, budget),
        _named(value.identifier, budget),
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
            iri = _named(value, operation)
            if iri not in seen:
                seen.add(iri)
                selected.append(iri)
        raw_rules = capture(rules, operation)
        if not raw_rules:
            raise InputError("Select a nonempty composition rule table")
        for value in raw_rules:
            approved.append(_rule(value, operation))
        first, left_metadata_unknown = _input(left, operation, issues, children)
        second, right_metadata_unknown = _input(right, operation, issues, children)
        if first is None or second is None:
            issues.append(
                Issue(
                    "mapping.composition_input_unverified",
                    "Both mapping producers must complete before composition",
                    kind="unverified",
                )
            )
        else:
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
            # Pay the actual selected join product and worst-case rule outputs
            # before entering fanout, rather than copying an unbounded join.
            operation.consume("work", product * (len(approved) + 2))
            operation.consume("states", product * (len(approved) * 2 + 6))
            operation.consume("bytes", product * (len(approved) * 256 + 512))
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
            for iri in selected:
                for left_index in index_left[iri]:
                    for right_index in index_right[iri]:
                        operation.consume("work")
                        left_row, right_row = (
                            first.rows[left_index],
                            second.rows[right_index],
                        )
                        operation.consume(
                            "work", len(left_row.cells) + len(right_row.cells)
                        )
                        usable = _interpretable(
                            left_row, extension_left, left_metadata_unknown
                        ) and _interpretable(
                            right_row, extension_right, right_metadata_unknown
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
