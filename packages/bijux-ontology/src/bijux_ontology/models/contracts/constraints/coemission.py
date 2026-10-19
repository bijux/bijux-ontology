"""Finite simultaneous field and discriminator implications for model contracts."""

from itertools import chain, combinations

from rdflib import Literal, URIRef
from rdflib.namespace import RDF, XSD

from bijux_ontology.declarations.types import (
    ModelDeclaration,
)
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.models.contracts.groups import declaration_states, group_limitation
from bijux_ontology.models.contracts.matching import matches_value
from bijux_ontology.models.contracts.propagation import ModelContracts
from bijux_ontology.models.rdf.observations import RdfObservation
from bijux_ontology.models.value_constraints.checking import application_matches
from bijux_ontology.owl.relations.classes import ClassContracts
from bijux_ontology.rdf.literals.checking import (
    check_literal,
)


def coemission_issues(
    model: ModelDeclaration,
    classes: ClassContracts,
    contracts: ModelContracts,
) -> list[Issue]:
    """Check declared simultaneous emissions without treating alternatives as facts."""
    if model.emission_groups:
        states = declaration_states(model, contracts.budget)
        group_issues: list[Issue] = []
        where = model.location or model.identifier
        if not states:
            return [
                Issue(
                    "models.emission_group_conflict",
                    "No emission state satisfies all groups and required fields",
                    where,
                )
            ]
        governed = {name for group in model.emission_groups for name in group.fields}
        for group in model.emission_groups:
            limitation = group_limitation(model, group)
            if limitation:
                group_issues.append(
                    Issue(
                        "models.emission_group_unverified",
                        limitation,
                        where,
                        kind="unverified",
                    )
                )
        fields_by_name = {field.name: field for field in model.fields}
        for group in model.emission_groups:
            for scenario in group.scenarios:
                condition = scenario.condition
                if condition is None:
                    continue
                contracts.budget.consume(
                    "work", 1 + len(fields_by_name[condition.field].values)
                )
                term = condition.equals
                lexical_valid = None
                if isinstance(term, Literal):
                    literal_report = check_literal(
                        term,
                        location=f"{where}/groups/{group.identifier}/{scenario.identifier}",
                        datatype_registry=contracts.datatype_registry,
                        budget=contracts.budget,
                    )
                    group_issues.extend(literal_report.issues)
                    lexical_valid = literal_report.verified
                traits = (
                    RdfObservation(
                        "literal",
                        datatype=str(term.datatype or XSD.string)
                        if not term.language
                        else str(RDF.langString),
                        language=term.language,
                    )
                    if isinstance(term, Literal)
                    else RdfObservation(
                        "iri", identifier=str(term), identifier_kind="iri"
                    )
                )
                matches = tuple(
                    None
                    if isinstance(value.direction, str)
                    else matches_value(value, traits, {model.identifier: model})
                    for value in fields_by_name[condition.field].values
                )
                constrained: list[bool | None] = []
                for value, matched in zip(
                    fields_by_name[condition.field].values, matches, strict=True
                ):
                    if matched is False or value.constraints is None:
                        constrained.append(matched)
                        continue
                    application = application_matches(
                        value.constraints,
                        term,
                        budget=contracts.budget,
                        lexical_valid=lexical_valid,
                    )
                    constrained.append(
                        False
                        if application is False
                        else None
                        if matched is None or application is None
                        else True
                    )
                matches = tuple(constrained)
                if True not in matches:
                    unknown = None in matches
                    group_issues.append(
                        Issue(
                            "models.scenario_condition_unverified"
                            if unknown
                            else "models.scenario_condition_conflict",
                            "Scenario discriminator is not established "
                            "by its field contract",
                            f"{where}/groups/{group.identifier}/{scenario.identifier}",
                            kind="unverified" if unknown else "finding",
                        )
                    )
        unconditional = set(contracts.guaranteed_types(model))
        base_conflicts = set(classes.conflicts(model.types))
        for state in states:
            if not state.covered:
                group_issues.append(
                    Issue(
                        "models.scenario_coverage_unverified",
                        "Some emission states lack declared scenario coverage",
                        where,
                        kind="unverified",
                    )
                )
            active = set(unconditional)
            promised = set(unconditional)
            for field in model.fields:
                if field.name in state.present and not field.predicate.startswith("@"):
                    domains = {
                        str(node)
                        for node in contracts.property(field).parents
                        if isinstance(node, URIRef)
                    }
                    active.update(domains)
                    if field.name in state.promised:
                        promised.update(domains)
            certain = set(classes.conflicts(tuple(promised)))
            for conflict in set(classes.conflicts(tuple(active))) - base_conflicts:
                group_issues.append(
                    Issue(
                        "models.emission_scenario_conflict"
                        if conflict in certain
                        else "models.conditional_emission_unverified",
                        "Emitted fields imply disjoint types in a finite state",
                        where,
                        kind="finding" if conflict in certain else "unverified",
                        actual=conflict,
                    )
                )
            # Ungoverned optional fields retain the existing finite uncertainty.
            remaining = [
                field
                for field in model.fields
                if field.name not in governed
                and field.emission != "guaranteed"
                and not field.predicate.startswith("@")
            ]
            for fields in chain(
                ((field,) for field in remaining), combinations(remaining, 2)
            ):
                contracts.budget.consume("work")
                types = set(active)
                for field in fields:
                    types.update(
                        str(node)
                        for node in contracts.property(field).parents
                        if isinstance(node, URIRef)
                    )
                for conflict in set(classes.conflicts(tuple(types))) - set(
                    classes.conflicts(tuple(active))
                ):
                    group_issues.append(
                        Issue(
                            "models.conditional_emission_unverified",
                            "Ungoverned field co-emission requires additional coverage",
                            where,
                            kind="unverified",
                            actual=conflict,
                        )
                    )
        return list(dict.fromkeys(group_issues))
    issues: list[Issue] = []
    guaranteed = contracts.guaranteed_types(model)
    budget = contracts.budget
    base_conflicts = set(classes.conflicts(model.types))
    for conflict in classes.conflicts(guaranteed):
        if conflict not in base_conflicts:
            issues.append(
                Issue(
                    "models.guaranteed_emission_conflict",
                    "Guaranteed emitted fields imply disjoint types "
                    "on the same resource",
                    model.location or model.identifier,
                    actual=conflict,
                )
            )
    conditional = [
        field
        for field in model.fields
        if field.emission != "guaranteed" and not field.predicate.startswith("@")
    ]
    for fields in chain(
        ((field,) for field in conditional), combinations(conditional, 2)
    ):
        budget.consume("work")
        types = set(guaranteed)
        for field in fields:
            types.update(
                str(node)
                for node in contracts.property(field).parents
                if isinstance(node, URIRef)
            )
        for conflict in set(classes.conflicts(tuple(types))) - set(
            classes.conflicts(guaranteed)
        ):
            names = ", ".join(field.name for field in fields)
            issues.append(
                Issue(
                    "models.conditional_emission_unverified",
                    f"Disjointness requires co-emission of: {names}",
                    model.location or model.identifier,
                    kind="unverified",
                    actual=conflict,
                )
            )
    return issues
