"""Context-preserving duplicate review using actual SSSOM production and checks."""

import json
from collections.abc import Iterator, Mapping, Sequence

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.mappings.admission import admit_mapping, decode_metadata
from bijux_ontology.vocabulary.mappings.checking import check_mappings
from bijux_ontology.vocabulary.mappings.composition.types import MappingOccurrence
from bijux_ontology.vocabulary.mappings.production import produce_mapping_table
from bijux_ontology.vocabulary.mappings.types import (
    MappingPolicy,
    MappingRead,
    MappingSet,
    _iri,
)

from .types import MappingGroup, MappingReconciliation

# These describe assertion provenance, not interpretation defaults or qualifiers.
_PROVENANCE = frozenset(
    (
        "mapping_set_id",
        "license",
        "mapping_set_version",
        "mapping_set_title",
        "mapping_set_description",
        "creator_id",
        "author_id",
        "curation_team",
        "mapping_provider",
        "mapping_set_source",
        "mapping_date",
        "publication_date",
        "comment",
    )
)


def _context(metadata: dict[str, object], budget: Budget) -> dict[str, object]:
    """Filter only reconciliation provenance from already qualified metadata."""
    budget.consume("work", len(metadata))
    budget.consume("states", len(metadata))
    budget.consume("bytes", 64 + 32 * len(metadata))
    return {key: value for key, value in metadata.items() if key not in _PROVENANCE}


def _reserve(budget: Budget, count: int = 1) -> None:
    budget.consume("states", count)
    budget.consume("bytes", 8 * count)


def produce_selection(
    groups: tuple[MappingGroup, ...],
    mapping_set_id: str,
    license: str,
    operation: Operation,
    *,
    contexts: dict[int, dict[str, object]] | None = None,
) -> MappingRead:
    """Frame original cells; the existing maintained producer qualifies the result.

    No TSV parser or independent semantic evaluator is introduced. Compatible
    interpretation metadata survives intact. Conflicting prefix meanings or
    set-level defaults cannot be flattened by source order.
    """
    _iri(mapping_set_id, operation)
    _iri(license, operation)
    if not groups:
        raise InputError(
            "Reconciliation requires a nonempty retained mapping selection"
        )
    context = None
    prefixes: dict[str, str] = {}
    columns: list[str] = []
    known_columns: set[str] = set()
    for group in groups:
        source = group.occurrences[0].mapping_set
        current = contexts.get(id(source)) if contexts is not None else None
        if current is None:
            current = _context(
                decode_metadata(source.metadata_json, operation), operation
            )
        serialized = json.dumps(current, sort_keys=True, separators=(",", ":"))
        bound_text(serialized, operation)
        if context is not None and current != context:
            raise _ContextUnavailable(
                "Retained rows have different set-level interpretation metadata"
            )
        context = current
        for prefix, iri in source.prefixes:
            bound_text(prefix, operation)
            bound_text(iri, operation)
            if prefix in prefixes and prefixes[prefix] != iri:
                raise _ContextUnavailable(
                    "Retained rows have incompatible actual prefix bindings"
                )
            _reserve(operation)
            prefixes[prefix] = iri
        for column in source.columns:
            operation.consume("work", len(column) + 1)
            if column not in known_columns:
                _reserve(operation, 2)
                known_columns.add(column)
                columns.append(column)
    assert context is not None
    metadata = {
        **context,
        "mapping_set_id": mapping_set_id,
        "license": license,
        "curie_map": prefixes,
    }
    return produce_mapping_table(
        columns,
        _selected_cells(groups, operation),
        metadata=metadata,
        mapping_set_id=mapping_set_id,
        license=license,
        prefixes=prefixes,
        operation=operation,
        framing="flow-map",
    )


def _selected_cells(
    groups: tuple[MappingGroup, ...], operation: Operation
) -> Iterator[Mapping[str, str]]:
    """Retain each selected original occurrence's lexical cells without rewriting."""
    for group in groups:
        operation.consume("work")
        yield dict(group.occurrences[0].row.cells)


class _ContextUnavailable(Exception):
    """Valid source contexts have no faithful common finite export representation."""


def _check(
    production: MappingRead,
    subject: VocabularyView,
    object: VocabularyView,
    policy: MappingPolicy,
    operation: Operation,
    issues: IssueCollector,
    reports: list[Report],
) -> None:
    """Retain actual native output before a later adoption or policy cut can fail."""
    try:
        _reserve(operation)
    except LimitExceeded:
        issues.retain_completed(production.report)
        raise
    reports.append(production.report)
    retain_report(production.report, operation, issues)
    if production.mapping_set is not None and production.report.verified:
        _reserve(operation)
        result = check_mappings(
            production.mapping_set,
            subject_vocabulary=subject,
            object_vocabulary=object,
            policy=policy,
            budget=operation,
        )
        reports.append(result.report)
        retain_report(result.report, operation, issues)


def reconcile_mappings(
    sources: Sequence[MappingSet],
    *,
    subject_vocabulary: VocabularyView,
    object_vocabulary: VocabularyView,
    policy: MappingPolicy,
    mapping_set_id: str,
    license: str,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
) -> MappingReconciliation:
    """Review exact duplicates and conflicts without choosing among alternatives.

    Row identity includes expanded directed endpoints/types/justification, every
    exact named lexical cell, actual prefix bindings and non-provenance metadata.
    Different qualifiers or confidence remain different groups. Release identity,
    authorship and license do not manufacture semantic differences; all originals
    remain witnesses. No relation inversion or confidence truth inference occurs.

    Host preparation/checks share budget and deadline. The maintained T054
    producer owns its existing separately bounded parser/serializer process;
    its internal work is not misrepresented as measured host work.
    """
    if (
        not isinstance(policy, MappingPolicy)
        or not isinstance(subject_vocabulary, VocabularyView)
        or not isinstance(object_vocabulary, VocabularyView)
    ):
        raise InputError(
            "Reconciliation requires explicit mapping policy and scoped endpoint views"
        )
    operation = Operation(limits, budget, timeout, None)
    issues = IssueCollector(operation)
    reports: list[Report] = []
    owned: list[MappingSet] = []
    contexts: dict[int, dict[str, object]] = {}
    occurrences: list[list[MappingOccurrence]] = []
    groups: tuple[MappingGroup, ...] = ()
    production = None
    complete = False
    try:
        supplied = capture_items(sources, MappingSet, operation)
        if len(supplied) < 2:
            raise InputError(
                "Reconciliation requires at least two supplied mapping sets"
            )
        index: dict[tuple[object, ...], int] = {}
        for source in supplied:
            admitted = admit_mapping(source, operation)
            captured = admitted.mapping_set
            _reserve(operation)
            owned.append(captured)
            current_context = _context(admitted.metadata, operation)
            _reserve(operation)
            contexts[id(source)] = current_context
            context = json.dumps(current_context, sort_keys=True, separators=(",", ":"))
            bound_text(context, operation)
            width = len(source.prefixes)
            size = sum(len(key) + len(value) for key, value in source.prefixes)
            operation.consume("work", (size + width) * max(1, width.bit_length()))
            _reserve(operation, width)
            prefix_context = tuple(sorted(source.prefixes))
            for number, row in enumerate(source.rows):
                width = len(row.cells)
                size = sum(len(key) + len(value) for key, value in row.cells)
                operation.consume("work", (size + width) * max(1, width.bit_length()))
                _reserve(operation, len(row.cells) + len(source.prefixes) + 2)
                key = (
                    row.subject,
                    row.predicate,
                    row.object,
                    row.justification,
                    row.subject_type,
                    row.object_type,
                    row.predicate_type,
                    tuple(sorted(row.cells)),
                    prefix_context,
                    context,
                )
                if key not in index:
                    _reserve(operation, 2)
                    index[key] = len(occurrences)
                    occurrences.append([])
                _reserve(operation)
                occurrences[index[key]].append(MappingOccurrence(source, number))
        _reserve(operation, len(occurrences) + sum(map(len, occurrences)))
        groups = tuple(MappingGroup(tuple(items)) for items in occurrences)
        complete = True
        _reserve(operation)
        production = produce_selection(
            groups, mapping_set_id, license, operation, contexts=contexts
        )
        _check(
            production,
            subject_vocabulary,
            object_vocabulary,
            policy,
            operation,
            issues,
            reports,
        )
        operation.consume("work", 0)
    except _ContextUnavailable as error:
        try:
            issues.append(
                Issue(
                    "mapping.reconciliation_unverified", str(error), kind="unverified"
                )
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining incompatible source context")
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(
                Issue("mapping.reconciliation_invalid", str(error), kind="input_error")
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining reconciliation input failure")
    return MappingReconciliation._owned(
        sources=tuple(owned),
        groups=groups,
        policy=policy,
        subject_vocabulary=subject_vocabulary,
        object_vocabulary=object_vocabulary,
        production=production,
        inventory_complete=complete,
        report=Report(
            "mapping.reconciliation",
            len(groups),
            tuple(issues),
            requested=max(1, len(groups)),
            complete=complete,
            children=tuple(reports),
            regime="explicit-context-qualified-mapping-reconciliation",
        ),
    )
