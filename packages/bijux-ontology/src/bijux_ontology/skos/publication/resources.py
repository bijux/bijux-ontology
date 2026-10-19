"""Explicit resource publication expectations over the selected SKOS scope."""

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import SKOS
from rdflib.term import Node

from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import Budget, IssueCollector

from ..resource_facts import ResourceFacts, is_lexical, literal_key
from ..selection import RuleSelection


def check_resource_publication(
    selected: ResourceFacts,
    policy: RuleSelection,
    node: Node,
    values: dict[URIRef, set[Node]],
    budget: Budget,
    issues: IssueCollector,
) -> None:
    selected.qualify(budget)
    scope = selected.scope
    graph, facts = (scope.graph, scope.facts)
    rules = policy.names
    languages, language_fallback = (policy.languages, policy.language_fallback)
    definition_predicates = policy.definition_predicates
    if "lexical-labels" in rules:
        for predicate, label_values in values.items():
            for value in label_values:
                budget.consume("work")
                if not is_lexical(value):
                    issues.append(
                        Issue(
                            "skos.policy.label_representation",
                            "Label policy requires a string literal",
                            str(node),
                            actual=str(predicate),
                        )
                    )
    if "language-coverage" in rules:
        present = {
            (value.language or "").lower()
            for value in values[SKOS.prefLabel]
            if is_lexical(value) and isinstance(value, Literal)
        }
        for tag in languages:
            if tag.lower() not in present and (
                not present.intersection(
                    fallback.lower() for fallback in language_fallback
                )
            ):
                issues.append(
                    Issue(
                        "skos.policy.language_missing",
                        "Missing requested preferred-label language/fallback",
                        str(node),
                        expected=tag,
                    )
                )
    memberships = facts.schemes.get(node, frozenset())
    if "scheme-required" in rules and (not memberships):
        issues.append(
            Issue(
                "skos.policy.scheme_missing",
                "Publication policy requires a known scheme membership",
                str(node),
            )
        )
    if "single-scheme" in rules and len(memberships) > 1:
        issues.append(
            Issue(
                "skos.policy.multiple_schemes",
                "Publication policy requires one scheme",
                str(node),
            )
        )
    if "definitions" in rules and (
        not any(
            is_lexical(value)
            for predicate in definition_predicates
            for value in graph.objects(node, URIRef(predicate))
        )
    ):
        issues.append(
            Issue(
                "skos.policy.definition_missing",
                "Missing requested literal definition",
                str(node),
            )
        )


def check_xl_form_policy(
    label_node: Node, forms: tuple[Node, ...], issues: IssueCollector
) -> None:
    for form in forms:
        if not is_lexical(form):
            issues.append(
                Issue(
                    "skos.policy.xl_form_shape",
                    "XL projection requires a string literal",
                    str(label_node),
                )
            )


def check_required_notation(
    selected: ResourceFacts,
    policy: RuleSelection,
    budget: Budget,
    issues: IssueCollector,
) -> int:
    selected.qualify(budget)
    scope = selected.scope
    graph = scope.graph
    rules = policy.names
    notation_nodes = selected.notation_nodes
    checked = 0
    if "notation-required" in rules:
        for node in sorted(notation_nodes, key=lambda value: value.n3()):
            budget.consume("work")
            checked += 1
            if not tuple(graph.objects(node, SKOS.notation)):
                issues.append(
                    Issue(
                        "skos.policy.notation_missing",
                        "Publication policy requires notation",
                        str(node),
                    )
                )
    return checked


def check_unique_notation(
    selected: ResourceFacts,
    policy: RuleSelection,
    budget: Budget,
    issues: IssueCollector,
) -> int:
    selected.qualify(budget)
    scope = selected.scope
    graph, facts, resources = (scope.graph, scope.facts, scope.resources)
    rules = policy.names
    notation_groups = selected.notation_groups
    checked = 0
    if "notation-unique" in rules:
        codes: dict[tuple[Node, tuple[str, str, str]], set[Node]] = {}
        groups: dict[Node, set[Node]] = {}
        if policy.notation_collections:
            for collection, selected_members in notation_groups.items():
                for node in selected_members:
                    budget.consume("work")
                    groups.setdefault(node, set()).add(collection)
        else:
            groups = {node: set(facts.schemes.get(node, ())) for node in resources}
        for node, notation_scopes in groups.items():
            for notation in graph.objects(node, SKOS.notation):
                budget.consume("work")
                if not isinstance(notation, Literal):
                    issues.append(
                        Issue(
                            "skos.policy.notation_shape",
                            "Notation policy requires a literal",
                            str(node),
                        )
                    )
                    continue
                if not notation_scopes:
                    issues.append(
                        Issue(
                            "skos.policy.notation_scope_unavailable",
                            "Uniqueness needs a known scheme or chosen collection",
                            str(node),
                            kind="unverified",
                        )
                    )
                for scheme in notation_scopes:
                    codes.setdefault((scheme, literal_key(notation)), set()).add(node)
        for (scheme, code), owners in codes.items():
            checked += 1
            if len(owners) > 1:
                issues.append(
                    Issue(
                        "skos.policy.notation_collision",
                        "Selected notation scope requires unique RDF terms",
                        str(scheme),
                        actual=repr(code),
                    )
                )
    return checked


def check_active_deprecation(
    selected: ResourceFacts,
    policy: RuleSelection,
    budget: Budget,
    issues: IssueCollector,
) -> int:
    selected.qualify(budget)
    scope = selected.scope
    graph, facts, resources, view = (
        scope.graph,
        scope.facts,
        scope.resources,
        scope.view,
    )
    rules = policy.names
    members = selected.members
    checked = 0
    if "deprecated-active" in rules:
        active = (
            {node for edge in facts.mapping_edges for node in (edge[0], edge[2])}
            | {
                node
                for subject, values in facts.broader.items()
                for node in (subject, *values)
            }
            | {node for values in members.values() for node in values}
            | set(graph.subjects(SKOS.topConceptOf))
            | set(graph.objects(None, SKOS.hasTopConcept))
        )
        for node in active & resources:
            budget.consume("work")
            checked += 1
            if not isinstance(node, URIRef | BNode):
                continue
            state = view.deprecation(
                node if isinstance(node, BNode) else str(node),
                replacement_predicates=policy.replacement_predicates,
                budget=budget,
            )
            list.extend(issues, state.report.issues)
            if state.state in ("conflicting", "unverified"):
                issues.append(
                    Issue(
                        "skos.policy.deprecation_unverified",
                        "Active deprecation metadata is conflicting or unsupported",
                        str(node),
                        kind="unverified",
                    )
                )
            elif state.state == "deprecated":
                issues.append(
                    Issue(
                        "skos.policy.deprecated_active",
                        "Deprecated resource remains in active relationships",
                        str(node),
                        severity="warning",
                        actual=", ".join(
                            sorted({str(edge.object) for edge in state.replacements})
                        )
                        or None,
                    )
                )
    return checked


def check_collection_member_shape(collection: Node, issues: IssueCollector) -> None:
    issues.append(
        Issue(
            "skos.policy.collection_member_shape",
            "Selected collection policy requires resource members",
            str(collection),
        )
    )
