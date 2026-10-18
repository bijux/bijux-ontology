"""Finite scoped emission states shared by declaration and output checking."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from itertools import combinations
from typing import TYPE_CHECKING

from bijux_ontology.declarations.types import EmissionGroup, ModelDeclaration
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.terms import term_key
from bijux_ontology.reports import Issue

if TYPE_CHECKING:
    from .corpus.types import _CorpusEvidence

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
