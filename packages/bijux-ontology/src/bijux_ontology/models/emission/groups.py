"""Finite scoped emission states shared by declaration and output checking."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import TYPE_CHECKING

from rdflib import RDF, Literal, URIRef

from bijux_ontology.declarations.types import EmissionGroup, ModelDeclaration
from bijux_ontology.jsonld.entities import subject_key
from bijux_ontology.jsonld.observations import ObservedOccurrence, at_path, location
from bijux_ontology.limits import Budget
from bijux_ontology.models.emission.bindings import same_contribution
from bijux_ontology.models.emission.coverage import source_properties
from bijux_ontology.rdf.datatype_evaluators.registry import DatatypeRegistry
from bijux_ontology.rdf.terms import literal_term, term_key
from bijux_ontology.reports import Issue

from .context import EmissionContext
from .values import _literal_result, _matches

if TYPE_CHECKING:
    from bijux_ontology.models.corpus.types import _CorpusEvidence


TermKey = tuple[str | None, ...]


@dataclass(frozen=True)
class EmissionState:
    """One finite declaration case, retaining absent fields and conditions."""

    present: frozenset[str]
    absent: frozenset[str] = frozenset()
    conditions: tuple[tuple[str, TermKey], ...] = ()
    covered: bool = True
    promised: frozenset[str] = frozenset()


def _options(group: EmissionGroup, budget: Budget) -> tuple[EmissionState, ...]:
    options = []
    if not group.scenarios or not group.closed:
        for size in range(
            group.minimum,
            (group.maximum if group.maximum is not None else len(group.fields)) + 1,
        ):
            for fields in combinations(group.fields, size):
                budget.consume("states")
                present = frozenset(fields)
                covered = not group.scenarios or any(
                    frozenset(case.present) == present and case.condition is None
                    for case in group.scenarios
                )
                options.append(
                    EmissionState(
                        present, frozenset(group.fields) - present, covered=covered
                    )
                )
    for case in group.scenarios:
        budget.consume("states")
        present = frozenset(case.present)
        absent = frozenset(group.fields) - present
        conditions: tuple[tuple[str, TermKey], ...] = ()
        if case.condition is not None:
            if case.condition.field in absent:
                continue
            present |= {case.condition.field}
            conditions = ((case.condition.field, term_key(case.condition.equals)),)
        options.append(EmissionState(present, absent, conditions, promised=present))
    return tuple(dict.fromkeys(options))


def declaration_states(
    model: ModelDeclaration, budget: Budget
) -> tuple[EmissionState, ...]:
    """Cross only explicitly governed finite states, preserving overlapping facts."""
    required = frozenset(
        field.name
        for field in model.fields
        if field.emission == "guaranteed"
        or field.output is not None
        and (field.output.minimum_values or 0) > 0
    )
    states: tuple[EmissionState, ...] = (EmissionState(required, promised=required),)
    for group in model.emission_groups:
        merged = []
        options = _options(group, budget)
        for left in states:
            for right in options:
                budget.consume("states")
                if left.present & right.absent or right.present & left.absent:
                    continue
                conditions = dict(left.conditions)
                if any(
                    name in conditions and conditions[name] != value
                    for name, value in right.conditions
                ):
                    continue
                conditions.update(right.conditions)
                merged.append(
                    EmissionState(
                        left.present | right.present,
                        left.absent | right.absent,
                        tuple(sorted(conditions.items())),
                        left.covered and right.covered,
                        left.promised | right.promised,
                    )
                )
        states = tuple(dict.fromkeys(merged))
    return states


def group_limitation(model: ModelDeclaration, group: EmissionGroup) -> str | None:
    """Identify field identities that cannot establish distinct RDF emissions."""
    fields = {field.name: field for field in model.fields}
    identities = [
        (fields[name].predicate, fields[name].reverse) for name in group.fields
    ]
    governed_names = {
        name for candidate in model.emission_groups for name in candidate.fields
    }
    governed_identities: dict[tuple[str, bool], str] = {}
    relevant = {(fields[name].predicate, fields[name].reverse) for name in group.fields}
    for name in sorted(governed_names):
        field = fields[name]
        identity = (field.predicate, field.reverse)
        if (
            identity in governed_identities
            and governed_identities[identity] != name
            and identity in relevant
        ):
            return (
                "Group field aliases across groups need one explicit RDF field contract"
            )
        governed_identities[identity] = name
    condition_names = {
        case.condition.field
        for candidate in model.emission_groups
        for case in candidate.scenarios
        if case.condition is not None
    }
    condition_identities: dict[tuple[str, bool], str] = {}
    for name in sorted(condition_names):
        field = fields[name]
        identity = (field.predicate, field.reverse)
        if (
            identity in condition_identities
            and condition_identities[identity] != name
            and any(case.condition is not None for case in group.scenarios)
        ):
            return (
                "Discriminator fields sharing an RDF predicate require "
                "a single explicit field contract"
            )
        condition_identities[identity] = name
    if any(predicate == "@id" for predicate, _ in identities):
        return "Subject identity is not an RDF predicate emission"
    if len(set(identities)) != len(identities):
        return (
            "Group fields with the same predicate and direction "
            "are indistinguishable in RDF"
        )
    return None


def check_groups(
    model: ModelDeclaration,
    states: Mapping[str, bool | None],
    values: Mapping[str, tuple[TermKey, ...] | None],
    *,
    where: str,
    budget: Budget,
    _corpus: _CorpusEvidence | None = None,
) -> tuple[Issue, ...]:
    """Check actual RDF occurrences in one established source/entity/graph scope."""
    issues = []
    for group in model.emission_groups:
        budget.consume("work")
        location = f"{where.rstrip('/')}/groups/{group.identifier}"
        limitation = group_limitation(model, group)
        if limitation:
            issues.append(
                Issue(
                    "emission.group_unverified", limitation, location, kind="unverified"
                )
            )
        fields_by_name = {field.name: field for field in model.fields}
        identities = [
            (fields_by_name[name].predicate, fields_by_name[name].reverse)
            for name in group.fields
        ]
        ambiguous = any(predicate == "@id" for predicate, _ in identities) or len(
            set(identities)
        ) != len(identities)
        selected = [None if ambiguous else states.get(name) for name in group.fields]
        count = sum(value is True for value in selected)
        unknown = sum(value is None for value in selected)
        if (
            count > (group.maximum if group.maximum is not None else len(group.fields))
            or count + unknown < group.minimum
        ):
            issues.append(
                Issue(
                    "emission.group_count_mismatch",
                    "Emitted field count violates the group bounds",
                    location,
                    expected=f"{group.minimum}..{group.maximum}",
                    actual=str(count),
                )
            )
        elif unknown:
            issues.append(
                Issue(
                    "emission.group_unverified",
                    "Some governed field emissions are unresolved",
                    location,
                    kind="unverified",
                )
            )
        if limitation or not group.scenarios or unknown:
            continue
        present = frozenset(name for name in group.fields if states.get(name) is True)
        collect = _corpus is not None and _corpus.wants_scenarios(
            model.identifier, group.identifier
        )
        matched = unresolved = False
        matched_ids = []
        for case in group.scenarios:
            budget.consume("work")
            if frozenset(case.present) != present:
                continue
            if case.condition is None:
                matched = True
                if not collect:
                    break
                matched_ids.append(case.identifier)
                continue
            terms = values.get(case.condition.field)
            if terms is None or len(terms) != 1:
                unresolved = True
            elif terms[0] == term_key(case.condition.equals):
                matched = True
                if not collect:
                    break
                matched_ids.append(case.identifier)
        if collect and _corpus is not None:
            _corpus.scenarios(
                model.identifier,
                group.identifier,
                tuple(matched_ids),
                unresolved=unresolved,
            )
        if not matched:
            issues.append(
                Issue(
                    "emission.scenario_unverified"
                    if unresolved or not group.closed
                    else "emission.scenario_mismatch",
                    "No declared emission scenario could be established"
                    if unresolved or not group.closed
                    else "Output emits a prohibited field combination or discriminator",
                    location,
                    kind="unverified" if unresolved or not group.closed else "finding",
                )
            )
    return tuple(issues)


def _group_terms(
    items: Sequence[ObservedOccurrence],
    native: dict[str, Literal],
    budget: Budget,
    datatype_registry: DatatypeRegistry | None = None,
) -> tuple[TermKey, ...] | None:
    """Read exact discriminator terms; keep list heads separate from members."""
    keys = []
    for item in items:
        budget.consume("work")
        if item.container == "list":
            if item.empty:
                keys.append(term_key(RDF.nil))
                continue
            return None
        traits = item.traits
        if traits.direction is not None:
            return None
        if traits.identifier_kind == "iri" and traits.identifier is not None:
            keys.append(term_key(URIRef(traits.identifier)))
        elif traits.kind == "literal":
            raw = item.value
            if not isinstance(raw, dict) or "@value" not in raw:
                return None
            if isinstance(raw["@value"], str):
                term = literal_term(
                    raw["@value"], datatype=traits.datatype, language=traits.language
                )
            else:
                if item.value_json not in native:
                    result = _literal_result(
                        item, "", budget, native, datatype_registry=datatype_registry
                    )
                    if not result.verified:
                        return None
                cached = native.get(item.value_json)
                if cached is None:
                    return None
                term = cached
            keys.append(term_key(term))
        else:
            return None
    return tuple(dict.fromkeys(keys))


def check_output_groups(state: EmissionContext) -> None:
    """Check grouped emission guarantees over established source or entity facts."""
    for binding in state.bindings:
        model = state.registry.get(binding.model)
        if model is None or not (
            model.emission_groups
            or (state.corpus is not None and state.corpus.fields(model.identifier))
        ):
            continue
        states: dict[str, bool | None] = {}
        terms: dict[str, tuple[TermKey, ...] | None] = {}
        needed = {name for group in model.emission_groups for name in group.fields}
        needed.update(
            case.condition.field
            for group in model.emission_groups
            for case in group.scenarios
            if case.condition is not None
        )
        if state.corpus is not None:
            needed.update(state.corpus.fields(model.identifier))
        entity = binding.reference is not None or binding.entity
        key = state.entity_for(binding) if entity else None
        description = state.prepared.entities.get(key) if key is not None else None
        node = None if entity else at_path(state.snapshot, binding.path)
        established = (
            description is not None and description.described
            if entity
            else len(state.traits_for(binding.path)) == 1
        )
        source_facts: tuple[ObservedOccurrence, ...] | None = None
        source_complete = True
        if established and (not entity) and isinstance(node, dict):
            owner = state.entity_for(binding)
            needed_predicates = {
                field.predicate for field in model.fields if field.name in needed
            }
            gathered: list[ObservedOccurrence] = []
            if owner is not None:
                for raw_key in node:
                    if raw_key in {"@context", "@included", "@graph"}:
                        continue
                    if raw_key in {"@id", "@type"} and raw_key not in needed_predicates:
                        continue
                    usage = state.prepared.observe((*binding.path, raw_key))
                    if usage.limitation:
                        source_complete = False
                        continue
                    gathered.extend(
                        item
                        for item in usage.occurrences
                        if subject_key(item) == owner
                        or (
                            item.predicate == "@id"
                            and item.traits.identifier == owner.identifier
                        )
                    )
                source_facts = tuple(dict.fromkeys(gathered))
        for field in model.fields:
            if field.name not in needed:
                continue
            state.budget.consume("work")
            selected_facts: tuple[ObservedOccurrence, ...] | None = None
            if established and entity and (description is not None):
                selected_facts = tuple(
                    item
                    for item in description.facts
                    if item.predicate == field.predicate
                    and item.reverse == field.reverse
                )
            elif source_facts is not None:
                selected_facts = tuple(
                    item
                    for item in source_facts
                    if item.predicate == field.predicate
                    and item.reverse == field.reverse
                )
            if not entity and selected_facts is not None and isinstance(node, dict):
                source_key = field.key or field.name
                declared_facts = (
                    state.prepared.observe((*binding.path, source_key)).occurrences
                    if source_key in node
                    else ()
                )
                if model.coverage is not None:
                    declared_facts += tuple(
                        item
                        for prop in source_properties(node, binding.path, state.budget)
                        if (prop.key, field.name) in model.coverage.aliases
                        for item in state.prepared.observe(prop.path).occurrences
                    )
                for fact in selected_facts:
                    if fact in declared_facts:
                        continue
                    state.budget.consume("work", 1 + len(field.values))
                    field_where = f"{location(binding.path)}.{field.name}"
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
                            binding,
                            model.identifier,
                            field.name,
                            matches,
                            empty=fact.empty,
                        )
                    if True not in matches:
                        state.issues.append(
                            Issue(
                                "emission.value_unverified"
                                if None in matches
                                else "emission.value_mismatch",
                                (
                                    "Aliased RDF contribution does not establish "
                                    "a declared value branch"
                                ),
                                field_where,
                                kind="unverified" if None in matches else "finding",
                            )
                        )
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
                    matched_child = False
                    for child in state.bindings if targets else ():
                        state.budget.consume("work")
                        if (
                            child.model not in targets
                            or child.reference is not None
                            or child.entity
                            or (child.path[: len(binding.path)] != binding.path)
                        ):
                            continue
                        child_usage = state.prepared.observe(child.path)
                        state.budget.consume("work", len(child_usage.occurrences))
                        if not child_usage.limitation and any(
                            same_contribution(fact, item)
                            for item in child_usage.occurrences
                        ):
                            matched_child = True
                            break
                    if targets and (not matched_child):
                        state.issues.append(
                            Issue(
                                "emission.nested_scope_unverified",
                                (
                                    "Aliased child descriptions require an "
                                    "established model binding"
                                ),
                                field_where,
                                kind="unverified",
                            )
                        )
            states[field.name] = (
                None
                if selected_facts is None
                or (not selected_facts and (not source_complete))
                else bool(selected_facts)
            )
            terms[field.name] = (
                None
                if selected_facts is None or not source_complete
                else _group_terms(
                    selected_facts,
                    state.native_literals,
                    state.budget,
                    state.datatype_registry,
                )
            )
        if state.corpus is not None:
            state.corpus.scope(binding, states, terms)
        state.checked += len(model.emission_groups)
        state.issues.extend(
            check_groups(
                model,
                states,
                terms,
                _corpus=state.corpus,
                where=f"entity[{binding.reference}]"
                if binding.reference is not None
                else location(binding.path),
                budget=state.budget,
            )
        )
