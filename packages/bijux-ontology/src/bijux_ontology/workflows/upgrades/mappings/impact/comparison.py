"""Exact selected row comparison without guessing cross-release row identity."""

import json
from collections.abc import Mapping, Sequence
from importlib.metadata import PackageNotFoundError, version
from typing import Literal

from bijux_ontology._json import decode_json, object_value
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.inputs import capture
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.vocabulary.mappings.composition.types import MappingOccurrence
from bijux_ontology.vocabulary.mappings.types import MappingRow, MappingSet

from .types import MappingChange, MappingQualifications

Signature = tuple[object, ...]
RowIndex = dict[Signature, tuple[MappingOccurrence, ...]]


def names(values: Sequence[str], budget: Budget) -> tuple[str, ...]:
    """Own exact nonempty unique lexical selectors before hashing them."""
    result = capture(values, budget)
    for value in result:
        if type(value) is not str or not value:
            raise InputError("Mapping scope and qualification names require text")
        bound_text(value, budget)
    if len(set(result)) != len(result):
        raise InputError("Mapping scope and qualification names must be unique")
    return result


def qualifications(
    value: MappingQualifications, budget: Budget
) -> MappingQualifications:
    """Capture the caller's exact selected cell and metadata contract."""
    if not isinstance(value, MappingQualifications):
        raise InputError("Mapping impact requires explicit typed qualifications")
    return MappingQualifications(
        names(value.cells, budget), names(value.metadata, budget)
    )


def signature(
    row: MappingRow,
    fields: MappingQualifications,
    metadata: tuple[tuple[str, bool, str], ...],
    budget: Budget,
) -> Signature:
    """Preserve named roles and lexical selected cells without CURIE alias guesses."""
    budget.consume("work", len(row.cells) + len(fields.cells) + 1)
    budget.consume("states", len(row.cells) + len(fields.cells) + 7)
    cells = dict(row.cells)
    return (
        row.subject,
        row.predicate,
        row.object,
        row.subject_type,
        row.object_type,
        row.predicate_type,
        tuple(
            (
                key,
                key in cells,
                row.justification
                if key == "mapping_justification"
                else cells.get(key, ""),
            )
            for key in fields.cells
        ),
        metadata,
    )


def index(
    value: MappingSet, fields: MappingQualifications, budget: Budget
) -> tuple[RowIndex, tuple[Signature, ...], dict[str, object]]:
    """Retain original duplicates and distinguish absent metadata from JSON null."""
    size = len(value.metadata_json)
    budget.consume("work", 4 * size)
    budget.consume("states", size)
    budget.consume("bytes", 4 * size)
    metadata = object_value(
        decode_json(
            value.metadata_json,
            max_bytes=budget.limits.max_bytes,
            max_depth=budget.limits.max_depth,
        ),
        location="mapping metadata",
    )
    if "curie_map" in fields.metadata:
        count = len(value.prefixes)
        budget.consume("states", count + 1)
        budget.consume("bytes", 64 + 64 * count)
        budget.consume("work", count)
        size += sum(len(prefix) + len(iri) + 8 for prefix, iri in value.prefixes)
        metadata["curie_map"] = dict(value.prefixes)
    selected = []
    for key in fields.metadata:
        budget.consume("work")
        budget.consume("bytes", 8 * size)
        budget.consume("states", size)
        encoded = json.dumps(
            metadata.get(key),
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
        )
        bound_text(encoded, budget)
        selected.append((key, key in metadata, encoded))
    chosen = tuple(selected)
    result: dict[Signature, list[MappingOccurrence]] = {}
    row_keys = []
    for position, row in enumerate(value.rows):
        row_key = signature(row, fields, chosen, budget)
        budget.consume("states", 3)
        row_keys.append(row_key)
        result.setdefault(row_key, []).append(MappingOccurrence(value, position))
    budget.consume("states", len(result))
    return {key: tuple(rows) for key, rows in result.items()}, tuple(row_keys), metadata


def differences(
    before: RowIndex, after: RowIndex, budget: Budget
) -> tuple[MappingChange, ...]:
    """Compare selected occurrence multiplicities without inventing matched row IDs."""
    result = []
    directions: tuple[tuple[Literal["removed", "added"], RowIndex, RowIndex], ...] = (
        ("removed", before, after),
        ("added", after, before),
    )
    for status, left, right in directions:
        for key, rows in left.items():
            budget.consume("work")
            common = len(right.get(key, ()))
            for position in range(common, len(rows)):
                budget.consume("states")
                result.append(MappingChange(status, rows[position]))
    return tuple(result)


def interpretation_fields(
    before: MappingSet,
    after: MappingSet,
    fields: MappingQualifications,
    budget: Budget,
) -> frozenset[str] | None:
    """Classify changed-prefix qualification semantics with the maintained schema.

    None means the selected identifier/literal distinction cannot be established.
    No CURIE expansion or namespace inference is implemented here. Endpoint and
    justification identities are already expanded by the original native reader.
    An explicitly selected curie_map compares the actual retained prefix map.
    """
    expanded = {
        "subject_id",
        "predicate_id",
        "object_id",
        "mapping_justification",
        "curie_map",
    }
    budget.consume("states", len(fields.cells) + len(fields.metadata))
    selected = (set(fields.cells) | set(fields.metadata)) - expanded
    budget.consume("states", len(before.prefixes) + len(after.prefixes))
    if not selected or dict(before.prefixes) == dict(after.prefixes):
        return frozenset()
    budget.consume("resources")
    try:
        if (
            version("sssom-schema") != before.schema_version
            or before.schema_version != after.schema_version
        ):
            return None
        from sssom.constants import SSSOMSchemaView

        identifiers = SSSOMSchemaView().entity_reference_slots
        result = set()
        for name in identifiers:
            budget.consume("work")
            bound_text(name, budget)
            if name in selected:
                budget.consume("states")
                result.add(name)
        budget.consume("work", 0)
        return frozenset(result)
    except (ModuleNotFoundError, PackageNotFoundError):
        return None


def interpretation_required(
    use_row: MappingRow,
    fields: MappingQualifications,
    classified: frozenset[str] | None,
    before: Mapping[str, object],
    after: Mapping[str, object],
    budget: Budget,
) -> bool:
    """Limit interpretation uncertainty to populated selected consumer qualifiers."""
    budget.consume("states", len(use_row.cells))
    budget.consume(
        "work", len(use_row.cells) + len(fields.cells) + len(fields.metadata)
    )
    cells = dict(use_row.cells)
    expanded = {
        "subject_id",
        "predicate_id",
        "object_id",
        "mapping_justification",
        "curie_map",
    }
    return any(
        name not in expanded
        and (classified is None or name in classified)
        and bool(cells.get(name))
        for name in fields.cells
    ) or any(
        (classified is None or name in classified)
        and (bool(before.get(name)) or bool(after.get(name)))
        for name in fields.metadata
    )
