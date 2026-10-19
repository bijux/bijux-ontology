"""Model contracts over joined, graph-qualified entity descriptions."""

from __future__ import annotations

from bijux_ontology._jsonpaths import location
from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.jsonld.entities import object_key
from bijux_ontology.jsonld.values import ValueTraits
from bijux_ontology.models.emission.counts import check_occurrences
from bijux_ontology.models.emission.types import ModelUsage

from .context import EmissionContext
from .values import _literal_result, _matches


def check_entity(
    state: EmissionContext, binding: ModelUsage, model: ModelDeclaration
) -> None:
    """Check joined entity facts separately from source-object guarantees."""
    key = state.entity_for(binding)
    where = (
        f"entity[{binding.reference}]"
        if binding.reference is not None
        else location(binding.path)
    )
    description = state.prepared.entities.get(key) if key is not None else None
    traits = ValueTraits(
        "node",
        identifier=key.identifier if key is not None else None,
        identifier_kind="iri"
        if key is not None
        and key.identifier is not None
        and (not key.identifier.startswith("_:"))
        else "blank",
        types=description.types if description is not None else (),
    )
    state.checked += 1
    if key is None:
        state.issues.append(
            Issue(
                "emission.node_unverified",
                "Selected source subject identity could not be established",
                where,
                kind="unverified",
            )
        )
        return
    state.identity_issues(binding, model, traits, where)
    if description is None or not description.described:
        state.issues.append(
            Issue(
                "emission.reference_missing"
                if binding.closed
                else "emission.reference_unverified",
                "No local description was supplied in this effective graph",
                where,
                kind="finding" if binding.closed else "unverified",
                actual=key.identifier,
            )
        )
        return
    if set(model.types) - set(description.types):
        state.issues.append(
            Issue(
                "emission.types_mismatch",
                "Entity lacks required asserted types in this graph",
                where,
                expected=", ".join(model.types),
                actual=", ".join(description.types),
            )
        )
    for field in model.fields:
        state.budget.consume("work")
        state.checked += 1
        facts = []
        for item in description.facts:
            state.budget.consume("work")
            if item.predicate == field.predicate and item.reverse == field.reverse:
                facts.append(item)
        field_where = f"{where}.{field.name}"
        if not facts and field.emission == "guaranteed":
            state.issues.append(
                Issue(
                    "emission.required_entity_fact_missing",
                    "Required predicate is absent from the selected graph",
                    field_where,
                )
            )
        for fact in facts:
            state.budget.consume("work", 1 + len(field.values))
            literal_valid = None
            if fact.traits.kind == "literal":
                result = _literal_result(
                    fact,
                    field_where,
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
                    fact,
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
                    binding, model.identifier, field.name, matches, empty=fact.empty
                )
            if True not in matches:
                state.issues.append(
                    Issue(
                        "emission.value_unverified"
                        if None in matches
                        else "emission.value_mismatch",
                        "Entity value does not establish a declared branch",
                        field_where,
                        kind="unverified" if None in matches else "finding",
                    )
                )
            state.budget.consume("work", len(field.values))
            targets = {
                value.model
                for value in field.values
                if value.model is not None
                and _matches(
                    value,
                    fact,
                    state.registry,
                    state.prepared.entities,
                    state.rdf_direction,
                    budget=state.budget,
                    native_literals=state.native_literals,
                    role_checks=state.role_checks,
                )
                is not False
            }
            child_key = object_key(fact)
            if (
                targets
                and child_key is not None
                and (not state.reference_binding(child_key, targets))
                and (not state.bind_reference(child_key, targets))
            ):
                state.issues.append(
                    Issue(
                        "emission.nested_scope_unverified",
                        "Linked model entity requires a binding in this graph",
                        field_where,
                        kind="unverified",
                    )
                )
        state.issues.extend(
            check_occurrences(
                field.output,
                facts,
                where=field_where,
                budget=state.budget,
                native_literals=state.native_literals,
            )
        )
