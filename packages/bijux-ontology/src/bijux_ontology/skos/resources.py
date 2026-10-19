"""Compose selected SKOS resource evidence, integrity and publication families."""

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import RDF, SKOS

from bijux_ontology.diagnostics.errors import LimitExceeded
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import Budget, IssueCollector

from .integrity.resources import (
    check_collection_role,
    check_label_integrity,
    check_resource_roles,
    check_xl_forms,
    check_xl_role,
)
from .publication.resources import (
    check_active_deprecation,
    check_collection_member_shape,
    check_required_notation,
    check_resource_publication,
    check_unique_notation,
    check_xl_form_policy,
)
from .resource_facts import LABEL_PREDICATES, is_lexical, prepare_resource_facts
from .selection import RuleSelection, SkosScope
from .xl import ROLES, XL, describe_xl_label_from_facts, xl_labels_from_facts


def check_resources(
    scope: SkosScope, policy: RuleSelection, budget: Budget, issues: IssueCollector
) -> int:
    """Apply coherent families at their original incremental invocation points."""
    graph, facts, resources, view = (
        scope.graph,
        scope.facts,
        scope.resources,
        scope.view,
    )
    rules = policy.names
    checked = 0
    prepared = prepare_resource_facts(scope, policy, budget, issues)
    labels, members = prepared.labels, prepared.members
    if "xl" in rules:
        label_resources: set[URIRef | BNode] = {
            node
            for node in graph.subjects(RDF.type, XL.Label)
            if isinstance(node, URIRef | BNode)
            and (not scope.narrowed or node in resources)
        }
        for node in resources:
            if not isinstance(node, URIRef | BNode):
                continue
            selected = xl_labels_from_facts(prepared, node, budget=budget)
            list.extend(issues, selected.report.issues)
            checked += selected.report.checked
            if any(issue.kind == "exhausted" for issue in selected.report.issues):
                raise LimitExceeded(
                    "XL label view could not complete within its allowance"
                )
            by_role = {role: set(graph.objects(node, role)) for role in ROLES}
            for index, role in enumerate(ROLES):
                for other in ROLES[index + 1 :]:
                    if by_role[role] & by_role[other]:
                        issues.append(
                            Issue(
                                "skos.xl_role_overlap",
                                "Same label resource has disjoint XL label roles",
                                str(node),
                            )
                        )
            for label in selected.items:
                label_resources.add(label.resource)
                if label.link is not None:
                    plain = LABEL_PREDICATES[ROLES.index(label.link.predicate)]
                    labels[node][plain].update(
                        edge.object for edge in label.forms if is_lexical(edge.object)
                    )
        for label_node in sorted(label_resources, key=lambda value: value.n3()):
            selected = describe_xl_label_from_facts(prepared, label_node, budget=budget)
            list.extend(issues, selected.report.issues)
            checked += selected.report.checked
            if any(issue.kind == "exhausted" for issue in selected.report.issues):
                raise LimitExceeded("XL label description could not complete")
            forms = tuple(edge.object for edge in selected.items[0].forms)
            check_xl_forms(label_node, forms, issues)
            check_xl_form_policy(label_node, forms, issues)
            check_xl_role(label_node, scope, issues)
    for node in sorted(resources, key=lambda value: value.n3()):
        prepared.qualify(budget)
        budget.consume("work")
        checked += 1
        if (
            scope.explicit_concepts
            and isinstance(node, URIRef)
            and view.lookup(str(node)).status == "absent"
        ):
            issues.append(
                Issue(
                    "skos.selection_unresolved",
                    "Selected resource is absent from the supplied scope",
                    str(node),
                    kind="unverified",
                )
            )
        values = labels.get(node, {predicate: set() for predicate in LABEL_PREDICATES})
        check_label_integrity(node, values, rules, budget, issues)
        check_resource_publication(prepared, policy, node, values, budget, issues)
    checked += check_required_notation(prepared, policy, budget, issues)
    checked += check_resource_roles(prepared, policy, budget, issues)
    if "collections" in rules:
        for collection, roles in facts.roles.items():
            if SKOS.Collection not in roles or (
                scope.narrowed and collection not in resources
            ):
                continue
            checked += 1
            budget.consume("work")
            for member in members.get(collection, ()):
                budget.consume("work")
                if isinstance(member, Literal):
                    check_collection_member_shape(collection, issues)
                else:
                    check_collection_role(scope, collection, member, issues)
    checked += check_unique_notation(prepared, policy, budget, issues)
    checked += check_active_deprecation(prepared, policy, budget, issues)
    return checked
