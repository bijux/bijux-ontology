"""Pinned BioSamples shape and actual offline draft-07 schema execution."""

from hashlib import sha256

from bijux_ontology._json import decode_json, owned_json
from bijux_ontology._jsonpaths import bounded_location
from bijux_ontology._jsonschema import DRAFT07, validator
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.reports import Issue, Report

from .types import SchemaResource

CORE_VERSION = "9e552cf60451ad2b6dad20a1234bb07a111a4948"
CORE_SHA256 = "717dce14b247ce68b50521a09101d1db03caa4de5f734d2b782f90ca6c778700"


def resource(value: SchemaResource, budget: Budget) -> tuple[object, str]:
    """Qualify bytes and identity before parsing/hashing a caller's local schema."""
    if not isinstance(value, SchemaResource):
        raise InputError("Expected an explicit versioned schema resource")
    bound_text(value.uri, budget)
    bound_text(value.version, budget)
    absolute_iri(value.uri)
    if len(value.content) > budget.limits.max_bytes:
        raise LimitExceeded("Schema resource exceeds max_bytes")
    budget.consume("work", len(value.content))
    # The maintained duplicate-aware decoder owns container graphs and path
    # prefixes before the subsequent node measurement. Reserve a conservative
    # raw-byte node bound and depth-scaled workspace before entering it.
    budget.consume("states", len(value.content))
    budget.consume("bytes", (128 + 4 * budget.limits.max_depth) * len(value.content))
    decoded = decode_json(
        value.content,
        max_bytes=budget.limits.max_bytes,
        max_depth=budget.limits.max_depth,
    )
    owned, _ = owned_json(decoded, budget)
    return owned, sha256(value.content).hexdigest()


def check_schema(
    sample: object,
    input_cost: int,
    selected: SchemaResource,
    document: object,
    inventory: tuple[tuple[str, object], ...],
    budget: Budget,
    issues: IssueCollector,
) -> Report:
    """Apply the maintained dialect validator to the unchanged captured sample.

    The caller retains paid findings in its collector if a later error exhausts
    the allowance. Diagnostics use source paths, never arbitrary native messages.
    """
    engine, cost = validator(
        document,
        budget,
        dialect=DRAFT07,
        root_iri=selected.uri,
        resources=tuple(item for item in inventory if item[0] != selected.uri),
    )
    # Native errors and comparison workspace are bounded before iterator entry.
    budget.consume("work", cost * input_cost)
    budget.consume("bytes", 32 * cost * input_cost)
    start = len(issues)
    for error in engine.iter_errors(sample):
        budget.consume("work")
        budget.consume("states")
        where = bounded_location(tuple(error.absolute_path), budget)
        issues.append(
            Issue(
                "biosamples.schema_mismatch",
                "Sample violates its selected local draft-07 schema",
                where,
            )
        )
    return Report(
        "biosamples.schema",
        1,
        tuple(issues[start:]),
        regime="jsonschema-draft-07-offline",
    )
