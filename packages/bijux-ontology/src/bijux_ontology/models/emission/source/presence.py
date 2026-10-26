"""Actual compact node admission, field aliases and source presence."""

from __future__ import annotations

from bijux_ontology.absence import ABSENT
from bijux_ontology.declarations.types import FieldDeclaration, ModelDeclaration
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.json.paths import SourcePath, at_path, location
from bijux_ontology.jsonld.contracts.expectations import ContextExpectation
from bijux_ontology.jsonld.evidence.occurrences import ObservedUsage
from bijux_ontology.models.emission.context import EmissionContext
from bijux_ontology.models.emission.counts import check_occurrences
from bijux_ontology.models.emission.coverage import source_properties
from bijux_ontology.models.emission.types import ModelUsage


def check_node(
    state: EmissionContext, binding: ModelUsage, model: ModelDeclaration
) -> dict[str, object]:
    """Check the selected source node using its own maintained expansion evidence."""
    try:
        node = at_path(state.snapshot, binding.path)
    except (KeyError, IndexError) as error:
        raise InputError(
            "No supplied model usage", location=location(binding.path)
        ) from error
    if not isinstance(node, dict):
        raise InputError(
            "Model bindings select compact node objects",
            location=location(binding.path),
        )
    state.checked += 1
    node_traits = state.traits_for(binding.path)
    if len(node_traits) != 1:
        state.issues.append(
            Issue(
                "emission.node_unverified",
                (
                    "Selected compact node cannot be associated "
                    "with exactly one expanded node"
                ),
                location(binding.path),
                kind="unverified",
            )
        )
    elif node_traits[0].kind not in {"node", "iri", "blank"}:
        state.issues.append(
            Issue(
                "emission.node_kind_mismatch",
                "Selected output is not an RDF node",
                location(binding.path),
                actual=node_traits[0].kind,
            )
        )
    elif set(model.types) - set(node_traits[0].types):
        state.issues.append(
            Issue(
                "emission.types_mismatch",
                "Emitted node does not assert the declared RDF types",
                location(binding.path),
                expected=", ".join(model.types),
                actual=", ".join(node_traits[0].types),
            )
        )
    if len(node_traits) == 1:
        state.identity_issues(binding, model, node_traits[0], location(binding.path))
    return node


def select_fields(
    state: EmissionContext,
    binding: ModelUsage,
    model: ModelDeclaration,
    node: dict[str, object],
) -> tuple[list[tuple[FieldDeclaration, SourcePath]], set[str]]:
    """Keep original and aliased field paths before applying shared count bounds."""
    selected_fields = [
        (field, (*binding.path, field.key or field.name)) for field in model.fields
    ]
    if model.coverage is not None:
        by_name = {field.name: field for field in model.fields}
        by_key = {field.key or field.name: field for field in model.fields}
        by_key.update({key: by_name[name] for key, name in model.coverage.aliases})
        for prop in source_properties(node, binding.path, state.budget):
            alias_field = by_key.get(prop.key)
            if alias_field is not None and prop.path != (
                *binding.path,
                alias_field.key or alias_field.name,
            ):
                selected_fields.append((alias_field, prop.path))
    aggregate_fields = {
        field.name
        for field, selected_path in selected_fields
        if selected_path != (*binding.path, field.key or field.name)
    }
    return selected_fields, aggregate_fields


def check_presence(
    state: EmissionContext,
    field: FieldDeclaration,
    path: SourcePath,
    raw_value: object,
    expectations: list[ContextExpectation],
) -> ObservedUsage | None:
    """Distinguish omission, null, empty emission and unresolved observation."""
    if raw_value is ABSENT:
        state.issues.extend(
            check_occurrences(
                field.output,
                (),
                where=location(path),
                budget=state.budget,
                native_literals=state.native_literals,
                present=False,
            )
        )
        if field.emission == "guaranteed":
            state.issues.append(
                Issue(
                    "emission.required_occurrence_missing",
                    ("Guaranteed field was omitted from the representative output"),
                    location(path),
                )
            )
        return None
    expectations.append(
        ContextExpectation(path, field.predicate, reverse=field.reverse)
    )
    observed = state.prepared.observe(path)
    if observed.limitation:
        state.issues.append(
            Issue(
                "emission.value_unverified",
                observed.limitation,
                location(path),
                kind="unverified",
            )
        )
        return None
    if not observed.occurrences:
        state.issues.extend(
            check_occurrences(
                field.output,
                (),
                where=location(path),
                budget=state.budget,
                native_literals=state.native_literals,
                present=True,
                raw=raw_value,
            )
        )
        if field.emission == "guaranteed":
            state.issues.append(
                Issue(
                    "emission.required_occurrence_missing",
                    "Guaranteed field emitted no RDF occurrence",
                    location(path),
                )
            )
        elif (
            field.output is not None
            and field.output.allow_empty
            and (raw_value is not None or field.output.allow_null)
        ):
            expectations.pop()
        else:
            state.issues.append(
                Issue(
                    "emission.value_unverified",
                    "No value survived the selected representative usage",
                    location(path),
                    kind="unverified",
                )
            )
        return None
    return observed
