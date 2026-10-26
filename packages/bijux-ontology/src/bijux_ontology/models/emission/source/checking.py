"""Interleave compact-source obligations while draining newly bound descendants."""

from __future__ import annotations

from dataclasses import replace

from bijux_ontology.absence import ABSENT
from bijux_ontology.json.paths import at_path
from bijux_ontology.jsonld.contracts.expectations import ContextExpectation
from bijux_ontology.models.emission.context import EmissionContext
from bijux_ontology.models.emission.entities import check_entity

from .children import bind_children
from .presence import check_node, check_presence, select_fields
from .values import check_values


def check_sources(state: EmissionContext) -> list[ContextExpectation]:
    """Check actual source fields and discover only proven child model bindings."""
    expectations: list[ContextExpectation] = []
    for binding in state.bindings:
        state.budget.consume("work")
        model = state.registry.get(binding.model)
        if model is None:
            continue
        if binding.reference is not None or binding.entity:
            check_entity(state, binding, model)
            continue
        node = check_node(state, binding, model)
        selected_fields, aggregate_fields = select_fields(state, binding, model, node)
        for field, path in selected_fields:
            state.budget.consume("work")
            if field.output is not None and field.name in aggregate_fields:
                field = replace(
                    field,
                    output=replace(
                        field.output, minimum_values=None, maximum_values=None
                    ),
                )
            state.checked += 1
            try:
                raw_value = at_path(state.snapshot, path)
            except KeyError:
                raw_value = ABSENT
            observed = check_presence(state, field, path, raw_value, expectations)
            if observed is None:
                continue
            minimum_depth = check_values(
                state, binding, model, field, path, observed, raw_value
            )
            bind_children(state, field, path, observed, minimum_depth)
    return expectations
