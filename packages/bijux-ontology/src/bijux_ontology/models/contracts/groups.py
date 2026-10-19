"""Finite declaration co-emission states and RDF field identity limitations."""

from dataclasses import dataclass
from itertools import combinations

from bijux_ontology.declarations.types import EmissionGroup, ModelDeclaration
from bijux_ontology.execution.limits import Budget
from bijux_ontology.rdf.terms import term_key

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
