"""Finite JSON records for exact, portable RDF dataset patches."""

from typing import cast

from rdflib import URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.patches.types import DatasetPatch
from bijux_ontology.rdf.records import codecs as records
from bijux_ontology.rdf.records.types import (
    PatchBlank,
    PatchResource,
)


def patch_to_data(
    patch: DatasetPatch, *, limits: Limits | None = None, budget: Budget | None = None
) -> dict[str, object]:
    """Export a finite explicit JSON object suitable for another local process."""
    operation = records._budget(limits, budget)
    if not isinstance(patch, DatasetPatch):
        raise InputError("Expected a typed DatasetPatch")
    return {
        "version": 1,
        "base": records.witness_data(patch.base, operation),
        "target": records.witness_data(patch.target, operation),
        "removals": [records.quad_data(q, operation) for q in patch.removals],
        "additions": [records.quad_data(q, operation) for q in patch.additions],
        "removed_graphs": [
            records.term_data(g, operation) for g in patch.removed_graphs
        ],
        "added_graphs": [records.term_data(g, operation) for g in patch.added_graphs],
    }


def patch_from_data(
    value: object, *, limits: Limits | None = None, budget: Budget | None = None
) -> DatasetPatch:
    """Decode typed edits while retaining exact lexical and blank-reference fields."""
    operation = records._budget(limits, budget)
    data = records._object(
        value,
        {
            "version",
            "base",
            "target",
            "removals",
            "additions",
            "removed_graphs",
            "added_graphs",
        },
    )
    if type(data["version"]) is not int or data["version"] != 1:
        raise InputError("Unsupported RDF patch wire version")
    base = records.read_witness(data["base"], operation)
    target = records.read_witness(data["target"], operation)
    edits = [
        tuple(
            records.read_quad(q, operation) for q in records._list(data[key], operation)
        )
        for key in ("removals", "additions")
    ]
    graphs = [
        tuple(
            records.read_term(g, operation) for g in records._list(data[key], operation)
        )
        for key in ("removed_graphs", "added_graphs")
    ]
    if any(not isinstance(g, URIRef | PatchBlank) for values in graphs for g in values):
        raise InputError("Patch graph edits require resource terms")
    return DatasetPatch(
        base,
        target,
        edits[0],
        edits[1],
        cast(tuple[PatchResource, ...], graphs[0]),
        cast(tuple[PatchResource, ...], graphs[1]),
    )
