"""Dependent child bindings established by actual compact source contributions."""

from __future__ import annotations

from bijux_ontology.declarations.types import FieldDeclaration
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.json.paths import SourcePath, location
from bijux_ontology.jsonld.evidence.entities import object_key
from bijux_ontology.jsonld.evidence.occurrences import ObservedUsage
from bijux_ontology.models.emission.bindings import same_contribution
from bijux_ontology.models.emission.context import EmissionContext
from bijux_ontology.models.emission.values import _matches


def bind_children(
    state: EmissionContext,
    field: FieldDeclaration,
    path: SourcePath,
    observed: ObservedUsage,
    minimum_depth: int,
) -> None:
    """Discover descendants without joining distinct source or graph identities."""
    covered: set[SourcePath] = set()
    for occurrence in observed.occurrences:
        state.budget.consume("work")
        if occurrence.depth != minimum_depth or occurrence.empty:
            continue
        state.budget.consume("work", len(field.values))
        targets = {
            value.model
            for value in field.values
            if value.model is not None
            and _matches(
                value,
                occurrence,
                state.registry,
                state.prepared.entities,
                state.rdf_direction,
                budget=state.budget,
                native_literals=state.native_literals,
                role_checks=state.role_checks,
            )
            is not False
        }
        if not targets:
            continue
        child_key = object_key(occurrence)
        discovered = state.bind_source(path, occurrence, targets)
        covered.update(discovered)
        matched = bool(discovered) or (
            child_key is not None
            and state.reference_source(path, child_key)
            and state.reference_binding(child_key, targets)
        )
        for child in state.bindings:
            if matched:
                break
            state.budget.consume("work")
            if (
                child.reference is not None
                or child.entity
                or child.model not in targets
                or (child.path in covered)
                or (child.path[: len(path)] != path)
            ):
                continue
            traits = state.traits_for(child.path)
            if len(traits) != 1 or traits[0].kind not in {
                "node",
                "iri",
                "blank",
            }:
                continue
            usage = state.prepared.observe(child.path)
            state.budget.consume("work", len(usage.occurrences))
            if not usage.limitation and any(
                same_contribution(occurrence, item) for item in usage.occurrences
            ):
                covered.add(child.path)
                matched = True
                break
        if (
            not matched
            and child_key is not None
            and state.reference_source(path, child_key)
        ):
            matched = state.bind_reference(child_key, targets)
        if not matched:
            state.issues.append(
                Issue(
                    "emission.nested_scope_unverified",
                    ("Nested model output needs an established source-node binding"),
                    location(path),
                    kind="unverified",
                )
            )
