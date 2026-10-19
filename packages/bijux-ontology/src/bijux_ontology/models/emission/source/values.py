"""Per-occurrence literal, branch, corpus and count obligations."""

from __future__ import annotations

from bijux_ontology.declarations.types import FieldDeclaration, ModelDeclaration
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.json.paths import SourcePath, location
from bijux_ontology.jsonld.evidence.occurrences import ObservedUsage
from bijux_ontology.models.emission.context import EmissionContext
from bijux_ontology.models.emission.counts import check_occurrences
from bijux_ontology.models.emission.types import ModelUsage
from bijux_ontology.models.emission.values import _literal_result, _matches


def check_values(
    state: EmissionContext,
    binding: ModelUsage,
    model: ModelDeclaration,
    field: FieldDeclaration,
    path: SourcePath,
    observed: ObservedUsage,
    raw_value: object,
) -> int:
    """Retain each paid value finding before dependent child binding begins."""
    minimum_depth = min(item.depth for item in observed.occurrences)
    for index, occurrence in enumerate(observed.occurrences):
        state.budget.consume("work")
        if occurrence.depth != minimum_depth:
            continue
        literal_valid = None
        if not occurrence.empty and occurrence.traits.kind == "literal":
            result = _literal_result(
                occurrence,
                f"{location(path)}[{index}]",
                state.budget,
                state.native_literals,
                state.direction_check,
                state.datatype_registry,
            )
            state.checked += result.checked
            state.issues.extend(result.issues)
            literal_valid = result.verified
        matches = tuple(
            _matches(
                value,
                occurrence,
                state.registry,
                state.prepared.entities,
                state.rdf_direction,
                budget=state.budget,
                native_literals=state.native_literals,
                role_checks=state.role_checks,
                lexical_valid=literal_valid,
            )
            for value in field.values
        )
        if state.corpus is not None:
            state.corpus.branch(
                binding,
                model.identifier,
                field.name,
                matches,
                empty=occurrence.empty,
            )
        if True not in matches:
            state.issues.append(
                Issue(
                    "emission.value_unverified"
                    if None in matches
                    else "emission.value_mismatch",
                    ("Observed value cannot be verified against a supported branch")
                    if None in matches
                    else ("Observed value matches none of the declared alternatives"),
                    f"{location(path)}[{index}]",
                    kind="unverified" if None in matches else "finding",
                    actual=occurrence.traits.kind,
                )
            )
    state.issues.extend(
        check_occurrences(
            field.output,
            tuple(item for item in observed.occurrences if item.depth == minimum_depth),
            where=location(path),
            budget=state.budget,
            native_literals=state.native_literals,
            present=True,
            raw=raw_value,
        )
    )
    return minimum_depth
