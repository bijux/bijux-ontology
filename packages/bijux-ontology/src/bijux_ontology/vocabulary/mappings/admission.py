"""Bounded whole-table mapping admission and independently owned metadata facts."""

from dataclasses import dataclass

from rdflib import Variable

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.json.values import decode_json, object_value

from .types import MappingRead, MappingSet, _iri


@dataclass(frozen=True)
class AdmittedMapping:
    """Original qualified table, its actual producer, and inert interpretation facts.

    This record defines no endpoint admissibility, logical meaning, duplicate
    equality or producer acceptance policy. Consumers retain those decisions.
    """

    mapping_set: MappingSet
    producer: Report | None
    metadata: dict[str, object]


def named_identifier(value: str, budget: Budget) -> str:
    """Reuse the mapping identifier qualifier without erasing variable kinds."""
    if isinstance(value, Variable):
        raise InputError("Mapping identifiers require named IRIs")
    return _iri(value, budget)


def decode_metadata(metadata_json: str, budget: Budget) -> dict[str, object]:
    """Decode all metadata under the actual enclosing work and ownership grant."""
    return object_value(
        decode_json(
            metadata_json,
            max_bytes=budget.limits.max_bytes,
            max_depth=budget.limits.max_depth,
            budget=budget,
        ),
        location="mapping metadata",
    )


def admit_mapping(value: MappingSet | MappingRead, budget: Budget) -> AdmittedMapping:
    """Qualify every original field before any consumer selects matching rows.

    Producer diagnostics are caller-owned and must be retained before entering
    this admission when a later bound could otherwise hide their paid prefix.
    """
    producer = value.report if isinstance(value, MappingRead) else None
    table = value.mapping_set if isinstance(value, MappingRead) else value
    if not isinstance(table, MappingSet):
        raise InputError("Mapping admission requires owned mapping inputs")
    value = table
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
        named_identifier(iri, budget)
    budget.consume("states", len(value.columns) + len(value.uninterpreted))
    for names in (value.columns, value.uninterpreted):
        for text in names:
            bound_text(text, budget)
    budget.consume("states", len(value.rows))
    budget.consume("bytes", 64 + 32 * len(value.rows))
    for row in value.rows:
        budget.consume("work")
        for iri in (row.subject, row.predicate, row.object, row.justification):
            named_identifier(iri, budget)
        for kind in (row.subject_type, row.object_type, row.predicate_type):
            if kind:
                named_identifier(kind, budget)
        budget.consume("states", len(row.cells))
        for key, text in row.cells:
            bound_text(key, budget)
            bound_text(text, budget)
    metadata = decode_metadata(value.metadata_json, budget)
    budget.consume("states")
    budget.consume("bytes", 128)
    return AdmittedMapping(value, producer, metadata)
