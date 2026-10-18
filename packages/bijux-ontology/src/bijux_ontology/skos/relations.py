"""Finite hierarchical and mapping integrity, with separate authoring policies."""

from rdflib import BNode, URIRef
from rdflib.namespace import SKOS
from rdflib.term import Node

from bijux_ontology.limits import Budget, IssueCollector

from ..reports import Issue
from .selection import RuleSelection, SkosScope
from .structure import _MAPPINGS, reachability


def _mapping_endpoints(
    scope: SkosScope, policy: RuleSelection, budget: Budget, issues: IssueCollector
) -> int:
    """Check incident mappings without selecting unrelated endpoint obligations."""
    facts, resources = scope.facts, scope.resources
    seen: set[tuple[Node, Node, str]] = set()
    checked = 0
    for subject, _predicate, target in facts.mapping_edges:
        budget.consume("work")
        if subject not in resources and target not in resources:
            continue
        checked += 1
        anchor = subject if subject in resources else target
        other = target if anchor == subject else subject
        for endpoint in (subject, target):
            if not isinstance(endpoint, (URIRef, BNode)):
                kind = "shape"
            elif facts.roles.get(endpoint, frozenset()) & {
                SKOS.ConceptScheme,
                SKOS.Collection,
            }:
                if "resource-integrity" in policy.names and endpoint in resources:
                    continue
                kind = "role"
            else:
                continue
            key = anchor, endpoint, kind
            if key in seen:
                continue
            budget.consume("states")
            seen.add(key)
            issues.append(
                Issue(
                    "skos.reference_shape_unverified"
                    if kind == "shape"
                    else "skos.resource_role_conflict",
                    "Mapping requires a resource endpoint"
                    if kind == "shape"
                    else f"Mapping endpoint {endpoint.n3()} has a disjoint known role",
                    str(anchor),
                    kind="unverified" if kind == "shape" else "finding",
                    actual=str(other),
                )
            )
    return checked


def check_relations(
    scope: SkosScope, policy: RuleSelection, budget: Budget, issues: IssueCollector
) -> int:
    """Check supported relation consequences without promoting close matches."""
    facts, resources, view = scope.facts, scope.resources, scope.view
    rules, closed_targets = policy.names, policy.closed_targets
    checked = 0
    closure = (
        reachability(facts.transitive, budget)
        if "relation-integrity" in rules
        or ("acyclic" in rules and policy.hierarchy_basis == "effective")
        else {}
    )
    policy_closure = (
        closure
        if policy.hierarchy_basis == "effective"
        else reachability(facts.authored_transitive, budget)
        if "acyclic" in rules
        else {}
    )
    policy_broader = (
        facts.authored_broader
        if policy.hierarchy_basis == "asserted"
        else facts.broader
    )
    incident_mappings = "mapping-endpoints" in rules
    if "relation-integrity" in rules:
        for subject, predicate, _target in facts.invalid_targets:
            if incident_mappings and predicate in _MAPPINGS:
                continue
            if subject in resources:
                issues.append(
                    Issue(
                        "skos.reference_shape_unverified",
                        "Finite SKOS relation view requires a resource endpoint",
                        str(subject),
                        kind="unverified",
                        actual=str(predicate),
                    )
                )
        for subject, target in facts.related:
            if subject not in resources and not (
                incident_mappings and target in resources
            ):
                continue
            budget.consume("work")
            checked += 1
            if target in closure.get(subject, {}):
                issues.append(
                    Issue(
                        "skos.related_hierarchy_conflict",
                        "Associative and hierarchy relations are disjoint",
                        str(subject if subject in resources else target),
                        actual=" -> ".join(map(str, closure[subject][target])),
                    )
                )
    if "acyclic" in rules:
        for node, paths in policy_closure.items():
            if node not in resources:
                continue
            checked += 1
            if node in paths:
                issues.append(
                    Issue(
                        "skos.policy.hierarchy_cycle",
                        "Selected hierarchy policy forbids this cycle",
                        str(node),
                        actual=" -> ".join(map(str, paths[node])),
                    )
                )
    if "mapping-integrity" in rules:
        exact = reachability(facts.exact, budget)
        seen_conflicts: set[tuple[Node, Node]] = set()
        for subject, target in facts.broad_mappings | facts.related_mappings:
            if subject not in resources and not (
                incident_mappings and target in resources
            ):
                continue
            budget.consume("work")
            checked += 1
            if target in exact.get(subject, {}):
                anchor = subject if subject in resources else target
                other = target if subject in resources else subject
                if (anchor, other) in seen_conflicts:
                    continue
                budget.consume("states")
                seen_conflicts.add((anchor, other))
                issues.append(
                    Issue(
                        "skos.mapping_conflict",
                        "Exact mapping conflicts with broad/narrow/related mapping",
                        str(anchor),
                        actual=str(other),
                    )
                )
    if incident_mappings:
        checked += _mapping_endpoints(scope, policy, budget, issues)
    if "same-scheme-hierarchy" in rules or "isolated" in rules:
        connected: set[Node] = set()
        for subject, targets in policy_broader.items():
            for target in targets:
                budget.consume("work")
                connected.update((subject, target))
                if (
                    "same-scheme-hierarchy" in rules
                    and subject in resources
                    and facts.schemes.get(subject, frozenset())
                    and facts.schemes.get(target, frozenset())
                    and not facts.schemes[subject] & facts.schemes[target]
                ):
                    issues.append(
                        Issue(
                            "skos.policy.cross_scheme_hierarchy",
                            "Selected policy forbids cross-scheme direct hierarchy",
                            str(subject),
                            actual=str(target),
                        )
                    )
        if "isolated" in rules:
            for node in resources - connected:
                issues.append(
                    Issue(
                        "skos.policy.isolated_concept",
                        "Selected policy requires a direct hierarchy connection",
                        str(node),
                    )
                )
    if "mapping-targets" in rules:
        for subject, _predicate, target in facts.mapping_edges:
            budget.consume("work")
            checked += 1
            if subject not in resources or not isinstance(target, URIRef):
                continue
            info = view.lookup(str(target))
            if facts.roles.get(target, frozenset()) & {
                SKOS.ConceptScheme,
                SKOS.Collection,
            }:
                issues.append(
                    Issue(
                        "skos.mapping_target_kind",
                        "Mapping target has a disjoint known resource role",
                        str(subject),
                        actual=str(target),
                    )
                )
            elif info.status != "declared":
                issues.append(
                    Issue(
                        "skos.policy.mapping_target_missing"
                        if closed_targets
                        else "skos.mapping_target_unresolved",
                        "Mapping target lacks a supplied declaration",
                        str(subject),
                        kind="finding" if closed_targets else "unverified",
                        actual=str(target),
                    )
                )
    return checked
