"""Finite SKOS label and resource-role integrity over selected original facts."""

from rdflib import Literal, URIRef
from rdflib.namespace import SKOS
from rdflib.term import Node

from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import Budget, IssueCollector

from ..resource_facts import LABEL_PREDICATES, ResourceFacts, literal_key
from ..selection import RuleSelection, SkosScope


def check_label_integrity(
    node: Node,
    values: dict[URIRef, set[Node]],
    rules: tuple[str, ...],
    budget: Budget,
    issues: IssueCollector,
) -> None:
    if "label-integrity" in rules:
        per_language: dict[str, set[tuple[str, str, str]]] = {}
        for value in values[SKOS.prefLabel]:
            budget.consume("work")
            if not isinstance(value, Literal):
                issues.append(
                    Issue(
                        "skos.preferred_label_language_unverified",
                        "Preferred-label language is unavailable for this resource",
                        str(node),
                        kind="unverified",
                    )
                )
            if isinstance(value, Literal):
                per_language.setdefault((value.language or "").lower(), set()).add(
                    literal_key(value)
                )
        for language, literals in per_language.items():
            if len(literals) > 1:
                issues.append(
                    Issue(
                        "skos.preferred_label_conflict",
                        "Distinct preferred labels share one language",
                        str(node),
                        actual=language or "untagged",
                    )
                )
        for index, first in enumerate(LABEL_PREDICATES):
            keys = {
                ("literal", *literal_key(value))
                if isinstance(value, Literal)
                else ("resource", value.n3())
                for value in values[first]
            }
            for second in LABEL_PREDICATES[index + 1 :]:
                if keys & {
                    ("literal", *literal_key(value))
                    if isinstance(value, Literal)
                    else ("resource", value.n3())
                    for value in values[second]
                }:
                    issues.append(
                        Issue(
                            "skos.label_role_conflict",
                            "Same literal has incompatible SKOS label roles",
                            str(node),
                            actual=f"{first}, {second}",
                        )
                    )


def check_xl_forms(
    label_node: Node, forms: tuple[Node, ...], issues: IssueCollector
) -> None:
    if len({literal_key(value) for value in forms if isinstance(value, Literal)}) > 1:
        issues.append(
            Issue(
                "skos.xl_form_conflict",
                "XL label has distinct literal forms",
                str(label_node),
            )
        )


def check_xl_role(label_node: Node, scope: SkosScope, issues: IssueCollector) -> None:
    facts = scope.facts
    if facts.roles.get(label_node, frozenset()) & {
        SKOS.Concept,
        SKOS.ConceptScheme,
        SKOS.Collection,
    }:
        issues.append(
            Issue(
                "skos.xl_role_conflict",
                "XL Label has a disjoint SKOS resource role",
                str(label_node),
            )
        )


def check_resource_roles(
    selected: ResourceFacts,
    policy: RuleSelection,
    budget: Budget,
    issues: IssueCollector,
) -> int:
    scope = selected.scope
    facts, resources, rules = scope.facts, scope.resources, policy.names
    checked = 0
    if "resource-integrity" in rules:
        for node, roles in facts.roles.items():
            if scope.narrowed and node not in resources:
                continue
            budget.consume("work")
            checked += 1
            if (
                SKOS.Concept in roles
                and SKOS.ConceptScheme in roles
                or SKOS.Collection in roles
                and roles & {SKOS.Concept, SKOS.ConceptScheme}
            ):
                issues.append(
                    Issue(
                        "skos.resource_role_conflict",
                        "Supplied SKOS classifications are disjoint",
                        str(node),
                        actual=", ".join(sorted(map(str, roles))),
                    )
                )
    return checked


def check_collection_role(
    scope: SkosScope, collection: Node, member: Node, issues: IssueCollector
) -> None:
    facts = scope.facts
    if SKOS.ConceptScheme in facts.roles.get(member, frozenset()):
        issues.append(
            Issue(
                "skos.collection_member_role_conflict",
                "Known scheme cannot be a concept/collection member",
                str(collection),
                actual=str(member),
            )
        )
