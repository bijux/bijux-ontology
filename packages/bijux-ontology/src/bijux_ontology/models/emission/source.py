"""Representative source-field checks and automatic child model binding."""

from __future__ import annotations

from dataclasses import replace

from bijux_ontology.absence import ABSENT
from bijux_ontology.errors import InputError
from bijux_ontology.jsonld.entities import object_key
from bijux_ontology.jsonld.expectations import ContextExpectation
from bijux_ontology.jsonld.paths import SourcePath, at_path, location
from bijux_ontology.models.emission.bindings import same_contribution
from bijux_ontology.models.emission.counts import check_occurrences
from bijux_ontology.models.emission.coverage import source_properties
from bijux_ontology.reports import Issue

from .context import EmissionContext
from .entities import check_entity
from .values import _literal_result, _matches


def check_sources(state: EmissionContext) -> list[ContextExpectation]:
    """Check actual source fields and discover only proven child model bindings."""
    expectations = []
    for binding in state.bindings:
        state.budget.consume("work")
        model = state.registry.get(binding.model)
        if model is None:
            continue
        if binding.reference is not None or binding.entity:
            check_entity(state, binding, model)
            continue
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
            state.identity_issues(
                binding, model, node_traits[0], location(binding.path)
            )
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
                            (
                                "Guaranteed field was omitted from the "
                                "representative output"
                            ),
                            location(path),
                        )
                    )
                continue
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
                continue
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
                continue
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
                            (
                                "Observed value cannot be verified against a "
                                "supported branch"
                            )
                            if None in matches
                            else (
                                "Observed value matches none of the declared "
                                "alternatives"
                            ),
                            f"{location(path)}[{index}]",
                            kind="unverified" if None in matches else "finding",
                            actual=occurrence.traits.kind,
                        )
                    )
            state.issues.extend(
                check_occurrences(
                    field.output,
                    tuple(
                        item
                        for item in observed.occurrences
                        if item.depth == minimum_depth
                    ),
                    where=location(path),
                    budget=state.budget,
                    native_literals=state.native_literals,
                    present=True,
                    raw=raw_value,
                )
            )
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
                        same_contribution(occurrence, item)
                        for item in usage.occurrences
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
                            (
                                "Nested model output needs an established "
                                "source-node binding"
                            ),
                            location(path),
                            kind="unverified",
                        )
                    )

    return expectations
