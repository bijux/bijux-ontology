"""Finite field and co-emission constraints over declared RDF contracts.

Each model definition and each field occurrence is checked independently. A
registry index resolves child types without recursively expanding model fields;
recursive declarations therefore terminate without losing reused-child checks.
Results describe supported declarations, not general ontology consistency.
"""

from collections.abc import Sequence
from dataclasses import replace
from itertools import chain, combinations

from rdflib import Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD
from rdflib.term import Node

from bijux_ontology.absence import ABSENT
from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    TermDeclaration,
    ValueDeclaration,
)
from bijux_ontology.jsonld.conversion import DirectionPolicy
from bijux_ontology.jsonld.values import ValueTraits
from bijux_ontology.limits import Budget
from bijux_ontology.models.contracts.propagation import ModelContracts
from bijux_ontology.models.emission.application import application_matches
from bijux_ontology.models.emission.groups import declaration_states, group_limitation
from bijux_ontology.models.emission.matching import matches_value
from bijux_ontology.owl.classes import ClassContracts
from bijux_ontology.owl.properties import PropertyContract
from bijux_ontology.rdf.datatype_evaluators.registry import DatatypeRegistry
from bijux_ontology.rdf.literals import Regime, check_literal, datatype_relation
from bijux_ontology.reports import Issue
from bijux_ontology.vocabulary.checking import check_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary


def _class_comparison(
    actual: Sequence[str],
    constraints: Sequence[Node],
    classes: ClassContracts,
    location: str,
    role: str,
    budget: Budget,
) -> list[Issue]:
    issues: list[Issue] = []
    ancestors, anonymous = classes.ancestors(actual)
    named = tuple(str(item) for item in constraints if isinstance(item, URIRef))
    _, required_expression = classes.ancestors(named)
    anonymous |= required_expression or any(
        not isinstance(item, URIRef) for item in constraints
    )
    for conflict in classes.conflicts((*actual, *named)):
        issues.append(
            Issue(
                "models.disjoint_conflict",
                f"{role} types imply explicitly disjoint classes",
                location,
                actual=conflict,
            )
        )
    for constraint in constraints:
        budget.consume("work")
        if isinstance(constraint, URIRef) and (
            str(constraint) in ancestors or constraint in (OWL.Thing, RDFS.Resource)
        ):
            continue
        issues.append(
            Issue(
                "models.relationship_unverified",
                f"{role} relationship is not established by named-class edges",
                location,
                kind="unverified",
                expected=str(constraint)
                if isinstance(constraint, URIRef)
                else "anonymous expression",
                actual=", ".join(sorted(actual)),
            )
        )
    if anonymous:
        issues.append(
            Issue(
                "models.expression_unverified",
                "Relevant class uses an anonymous expression",
                location,
                kind="unverified",
            )
        )
    return issues


def _literal_comparison(
    value: ValueDeclaration,
    ranges: Sequence[Node],
    location: str,
    classes: ClassContracts,
    regime: Regime,
    budget: Budget,
    *,
    encoded: bool = False,
    datatype_registry: DatatypeRegistry | None = None,
) -> list[Issue]:
    datatype = (
        str(RDF.langString) if isinstance(value.language, str) else value.datatype
    )
    issues: list[Issue] = []
    if (
        not encoded
        and datatype
        and datatype_relation(
            datatype,
            datatype,
            regime=regime,
            datatype_registry=datatype_registry,
            budget=budget,
        )
        == "unknown"
    ):
        issues.append(
            Issue(
                "models.datatype_unverified",
                "Declared datatype semantics are unsupported",
                location,
                kind="unverified",
                actual=datatype,
            )
        )
    for required in ranges:
        budget.consume("work")
        if required in (RDFS.Literal, RDFS.Resource):
            continue
        expected = (
            str(required) if isinstance(required, URIRef) else "anonymous expression"
        )
        expression = not isinstance(required, URIRef) or bool(
            classes.ancestors((expected,))[1]
        )
        relation = (
            datatype_relation(
                datatype,
                expected,
                regime=regime,
                datatype_registry=datatype_registry,
                budget=budget,
            )
            if datatype and not expression
            else "unknown"
        )
        if relation in ("equal", "subset"):
            continue
        conflict = relation == "disjoint"
        issue = Issue(
            "models.datatype_conflict" if conflict else "models.datatype_unverified",
            "Literal serialisation conflicts with the declared datatype range"
            if conflict
            else "Datatype compatibility is outside the supported comparisons",
            location,
            kind="finding" if conflict else "unverified",
            expected=expected,
            actual=datatype,
        )
        issues.append(issue)
    return issues


def _value_contract_issues(value: ValueDeclaration, location: str) -> list[Issue]:
    issues: list[Issue] = []
    if value.kind == "unknown":
        issues.append(
            Issue(
                "models.value_unverified",
                "Unknown serialisation cannot establish RDF value kind",
                location,
                kind="unverified",
            )
        )
    if value.container not in (None, "@set", "@list"):
        issues.append(
            Issue(
                "models.container_unverified",
                "Container requires a representative JSON-LD usage",
                location,
                kind="unverified",
                actual=value.container,
            )
        )
    return issues


def _branch_issues(
    field: FieldDeclaration,
    value: ValueDeclaration,
    registry: dict[str, ModelDeclaration],
    classes: ClassContracts,
    location: str,
    vocabulary: Vocabulary,
    contract: PropertyContract,
    regime: Regime,
    budget: Budget,
    *,
    rdf_direction: DirectionPolicy | None = None,
    datatype_registry: DatatypeRegistry | None = None,
) -> list[Issue]:
    issues: list[Issue] = []
    if (
        value.role is not None
        and value.role.kind == "instance"
        and value.role.bridge is None
        and value.role.types
    ):
        value = replace(
            value, types=tuple(dict.fromkeys((*value.types, *value.role.types)))
        )
    if (
        value.model is not None
        and value.model not in registry
        and value.container != "@list"
    ):
        return issues
    if value.container not in (None, "@set", "@list"):
        return issues
    if value.container == "@list":
        if contract.literal_value:
            issues.append(
                Issue(
                    "models.list_head_kind_conflict",
                    "An RDF list head is a resource, not a data-property literal",
                    f"{location}.head",
                )
            )
        issues.extend(
            _class_comparison(
                (str(RDF.List),),
                contract.values,
                classes,
                f"{location}.head",
                "List head",
                budget,
            )
        )
        issues.extend(
            _branch_issues(
                replace(field, reverse=False),
                replace(value, container=None),
                registry,
                classes,
                f"{location}.items",
                vocabulary,
                PropertyContract((), ()),
                regime,
                budget,
                rdf_direction=rdf_direction,
                datatype_registry=datatype_registry,
            )
        )
        return issues
    if value.kind == "unknown":
        return issues
    if (
        value.kind == "literal"
        and value.direction is None
        and value.datatype is None
        and rdf_direction is not None
    ):
        if rdf_direction == "compound-literal":
            if (
                field.reverse
                or contract.object_property
                or contract.literal_value
                or contract.values
            ):
                return [
                    Issue(
                        "models.direction_unverified",
                        "Unchecked direction permits either a physical literal "
                        "or a compound resource",
                        location,
                        kind="unverified",
                    )
                ]
            return issues
        # The literal kind is stable under i18n encoding, but its datatype is
        # conditional on direction. A language tag cannot promise langString.
        value = replace(value, language=None)
    encoded = isinstance(value.direction, str)
    declared_datatype = value.datatype
    if encoded:
        if rdf_direction is None:
            return [
                Issue(
                    "models.direction_unverified",
                    "Directional output requires an explicit RDF representation policy",
                    location,
                    kind="unverified",
                )
            ]
        if rdf_direction == "compound-literal":
            if value.datatype is not None:
                issues.append(
                    Issue(
                        "models.value_kind_conflict",
                        "A compound literal is a physical RDF resource, "
                        "not a typed literal",
                        location,
                    )
                )
            value = ValueDeclaration("object")
        else:
            language = value.language
            datatype = (
                "https://www.w3.org/ns/i18n#"
                + (language.lower() if isinstance(language, str) else "")
                + "_"
                + str(value.direction)
                if language is ABSENT or isinstance(language, str)
                else None
            )
            if value.datatype is not None and (
                datatype is not None
                and value.datatype != datatype
                or not value.datatype.startswith("https://www.w3.org/ns/i18n#")
                or not value.datatype.endswith("_" + str(value.direction))
            ):
                issues.append(
                    Issue(
                        "models.datatype_conflict",
                        "Explicit physical datatype differs from the "
                        "selected direction encoding",
                        location,
                        expected=value.datatype,
                        actual=datatype,
                    )
                )
            value = ValueDeclaration("literal", datatype=datatype or value.datatype)
    child = registry.get(value.model) if value.model else None
    types = tuple(dict.fromkeys((*value.types, *(child.types if child else ()))))
    if value.types:
        issues.extend(
            check_vocabulary(
                vocabulary,
                [
                    TermDeclaration(iri, (str(OWL.Class), str(RDFS.Class)), location)
                    for iri in value.types
                ],
                regime="owl-dl" if regime == "owl-dl" else "rdf",
                budget=budget,
            ).issues
        )
    literal = value.kind == "literal"
    if field.reverse and literal:
        issues.append(
            Issue(
                "models.reverse_literal_conflict",
                "A reversed property cannot use a literal as its subject",
                location,
            )
        )
        return issues
    if (literal and contract.object_property) or (
        not literal and contract.literal_value
    ):
        issues.append(
            Issue(
                "models.value_kind_conflict",
                "Serialised value kind conflicts with the property declaration",
                location,
                actual=value.kind,
            )
        )
    constraints = contract.values
    if literal:
        datatype = (
            str(RDF.langString) if isinstance(value.language, str) else value.datatype
        )
        checked_datatype = declared_datatype if encoded else datatype
        if checked_datatype:
            issues.extend(
                check_vocabulary(
                    vocabulary,
                    [
                        TermDeclaration(
                            checked_datatype, (str(RDFS.Datatype),), location
                        )
                    ],
                    regime="owl-dl" if regime == "owl-dl" else "rdf",
                    budget=budget,
                ).issues
            )
        issues.extend(
            _literal_comparison(
                value,
                constraints,
                location,
                classes,
                regime,
                budget,
                encoded=encoded and declared_datatype is None,
                datatype_registry=datatype_registry,
            )
        )
    else:
        issues.extend(
            _class_comparison(types, constraints, classes, location, "Value", budget)
        )
    return issues


def _field_issues(
    model: ModelDeclaration,
    registry: dict[str, ModelDeclaration],
    classes: ClassContracts,
    vocabulary: Vocabulary,
    regime: Regime,
    contracts: ModelContracts,
    *,
    rdf_direction: DirectionPolicy | None = None,
) -> tuple[int, list[Issue]]:
    if model.emission_groups:
        states = declaration_states(model, contracts.budget)
        governed = {name for group in model.emission_groups for name in group.fields}
        group_issues: list[Issue] = []
        checked = 0
        for state in states:
            promised = set(contracts.guaranteed_types(model))
            for field in model.fields:
                if field.name in state.promised and not field.predicate.startswith("@"):
                    promised.update(
                        str(node)
                        for node in contracts.property(field).parents
                        if isinstance(node, URIRef)
                    )
            for field in model.fields:
                if field.name in governed and field.name not in state.present:
                    continue
                parent_types = set(promised)
                if field.name in governed and not field.predicate.startswith("@"):
                    parent_types.update(
                        str(node)
                        for node in contracts.property(field).parents
                        if isinstance(node, URIRef)
                    )
                count, findings = _field_issues(
                    replace(
                        model,
                        types=tuple(sorted(parent_types)),
                        fields=(field,),
                        emission_groups=(),
                        coverage=None,
                    ),
                    registry,
                    classes,
                    vocabulary,
                    regime,
                    contracts,
                    rdf_direction=rdf_direction,
                )
                checked += count
                group_issues.extend(findings)
        return checked, list(dict.fromkeys(group_issues))
    issues: list[Issue] = []
    checked = 0
    for field in model.fields:
        contracts.budget.consume("work")
        location = field.location or f"{model.identifier}.{field.name}"
        checked += 1 + len(field.values)
        if field.predicate.startswith("@"):
            issues.append(
                Issue(
                    "models.keyword_unverified",
                    "Keyword semantics require a representative JSON-LD usage",
                    location,
                    kind="unverified",
                )
            )
            continue
        issues.extend(
            check_vocabulary(
                vocabulary,
                [TermDeclaration(field.predicate, (str(RDF.Property),), location)],
                regime="owl-dl" if regime == "owl-dl" else "rdf",
                budget=contracts.budget,
            ).issues
        )
        contract = contracts.property(field)
        constraints = contract.parents
        if contract.unsupported:
            issues.append(
                Issue(
                    "models.property_expression_unverified",
                    "Relevant property relations require unsupported expressions",
                    location,
                    kind="unverified",
                )
            )
        if contract.bottom:
            issues.append(
                Issue(
                    "models.bottom_property_conflict",
                    "An emitted occurrence cannot use an OWL bottom property",
                    location,
                )
            )
        if contract.literal_parent:
            issues.append(
                Issue(
                    "models.parent_kind_conflict",
                    "A resource model cannot be the literal endpoint "
                    "of a data property",
                    location,
                )
            )
        issues.extend(
            _class_comparison(
                model.types,
                constraints,
                classes,
                location,
                "Parent",
                contracts.budget,
            )
        )
        if not field.values:
            issues.append(
                Issue(
                    "models.value_unverified",
                    "Field has no known serialised value branches",
                    location,
                    kind="unverified",
                )
            )
        for index, branch in enumerate(field.values):
            issues.extend(
                _branch_issues(
                    field,
                    branch,
                    registry,
                    classes,
                    f"{location}[{index}]",
                    vocabulary,
                    contract,
                    regime,
                    contracts.budget,
                    rdf_direction=rdf_direction,
                    datatype_registry=contracts.datatype_registry,
                )
            )
    return checked, issues


def _emission_issues(
    model: ModelDeclaration,
    classes: ClassContracts,
    contracts: ModelContracts,
) -> list[Issue]:
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
                if isinstance(term, Literal):
                    group_issues.extend(
                        check_literal(
                            term,
                            location=f"{where}/groups/{group.identifier}/{scenario.identifier}",
                            datatype_registry=contracts.datatype_registry,
                            budget=contracts.budget,
                        ).issues
                    )
                traits = (
                    ValueTraits(
                        "literal",
                        datatype=str(term.datatype or XSD.string)
                        if not term.language
                        else str(RDF.langString),
                        language=term.language,
                    )
                    if isinstance(term, Literal)
                    else ValueTraits("iri", identifier=str(term), identifier_kind="iri")
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
                        value.constraints, term, budget=contracts.budget
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
