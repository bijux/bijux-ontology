"""Resource annotations, roles and collection/publication requirements."""

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import RDF, SKOS, XSD
from rdflib.term import Node

from bijux_ontology.limits import Budget, IssueCollector

from ..errors import LimitExceeded
from ..reports import Issue
from .collections import collection_memberships
from .selection import RuleSelection, SkosScope
from .xl import ROLES, XL, describe_xl_label, xl_labels

_LABELS = (SKOS.prefLabel, SKOS.altLabel, SKOS.hiddenLabel)


def _literal_key(value: Literal) -> tuple[str, str, str]:
    return (
        str(value),
        (value.language or "").lower(),
        str(
            value.datatype
            if value.datatype is not None
            else RDF.langString
            if value.language
            else XSD.string
        ),
    )


def _lexical(value: Node) -> bool:
    return isinstance(value, Literal) and (
        value.language is not None or value.datatype in (None, XSD.string)
    )


def check_resources(
    scope: SkosScope, policy: RuleSelection, budget: Budget, issues: IssueCollector
) -> int:
    """Check selected resource facts and explicitly requested publication metadata."""
    graph, facts, resources, view = (
        scope.graph,
        scope.facts,
        scope.resources,
        scope.view,
    )
    rules = policy.names
    languages, language_fallback = policy.languages, policy.language_fallback
    definition_predicates = policy.definition_predicates
    checked = 0
    members = (
        collection_memberships(
            graph,
            facts.members,
            required=policy.notation_collections,
            budget=budget,
            issues=issues,
        )
        if set(rules) & {"collections", "deprecated-active"}
        or set(rules) & {"notation-required", "notation-unique"}
        and policy.notation_collections
        else {}
    )
    notation_nodes = (
        {
            node
            for iri in policy.notation_collections
            for node in members.get(URIRef(iri), ())
        }
        if policy.notation_collections
        else set(resources)
    )
    if policy.notation_collections and set(rules) & {
        "notation-required",
        "notation-unique",
    }:
        for iri in policy.notation_collections:
            if SKOS.Collection not in facts.roles.get(URIRef(iri), ()):
                issues.append(
                    Issue(
                        "skos.policy.notation_scope_unavailable",
                        "Requested collection lacks supplied membership evidence",
                        iri,
                        kind="unverified",
                    )
                )
    labels = {
        node: {predicate: set(graph.objects(node, predicate)) for predicate in _LABELS}
        for node in resources
    }
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
            selected = xl_labels(view, node, budget=budget)
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
                    plain = _LABELS[ROLES.index(label.link.predicate)]
                    labels[node][plain].update(
                        edge.object for edge in label.forms if _lexical(edge.object)
                    )
        for label_node in sorted(label_resources, key=lambda value: value.n3()):
            selected = describe_xl_label(view, label_node, budget=budget)
            list.extend(issues, selected.report.issues)
            checked += selected.report.checked
            if any(issue.kind == "exhausted" for issue in selected.report.issues):
                raise LimitExceeded("XL label description could not complete")
            forms = tuple(edge.object for edge in selected.items[0].forms)
            if (
                len(
                    {
                        _literal_key(value)
                        for value in forms
                        if isinstance(value, Literal)
                    }
                )
                > 1
            ):
                issues.append(
                    Issue(
                        "skos.xl_form_conflict",
                        "XL label has distinct literal forms",
                        str(label_node),
                    )
                )
            for form in forms:
                if not _lexical(form):
                    issues.append(
                        Issue(
                            "skos.policy.xl_form_shape",
                            "XL projection requires a string literal",
                            str(label_node),
                        )
                    )
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
    for node in sorted(resources, key=lambda value: value.n3()):
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
        values = labels.get(node, {predicate: set() for predicate in _LABELS})
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
                        _literal_key(value)
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
            for index, first in enumerate(_LABELS):
                keys = {
                    ("literal", *_literal_key(value))
                    if isinstance(value, Literal)
                    else ("resource", value.n3())
                    for value in values[first]
                }
                for second in _LABELS[index + 1 :]:
                    if keys & {
                        ("literal", *_literal_key(value))
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
        if "lexical-labels" in rules:
            for predicate, label_values in values.items():
                for value in label_values:
                    budget.consume("work")
                    if not _lexical(value):
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
                if _lexical(value) and isinstance(value, Literal)
            }
            for tag in languages:
                if tag.lower() not in present and not present.intersection(
                    fallback.lower() for fallback in language_fallback
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
        if "scheme-required" in rules and not memberships:
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
        if "definitions" in rules and not any(
            _lexical(value)
            for predicate in definition_predicates
            for value in graph.objects(node, URIRef(predicate))
        ):
            issues.append(
                Issue(
                    "skos.policy.definition_missing",
                    "Missing requested literal definition",
                    str(node),
                )
            )
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
                    issues.append(
                        Issue(
                            "skos.policy.collection_member_shape",
                            "Selected collection policy requires resource members",
                            str(collection),
                        )
                    )
                elif SKOS.ConceptScheme in facts.roles.get(member, frozenset()):
                    issues.append(
                        Issue(
                            "skos.collection_member_role_conflict",
                            "Known scheme cannot be a concept/collection member",
                            str(collection),
                            actual=str(member),
                        )
                    )
    if "notation-unique" in rules:
        codes: dict[tuple[Node, tuple[str, str, str]], set[Node]] = {}
        groups: dict[Node, set[Node]] = {}
        if policy.notation_collections:
            for iri in policy.notation_collections:
                collection = URIRef(iri)
                for node in members.get(collection, ()):
                    budget.consume("work")
                    if not scope.narrowed or node in resources:
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
                    codes.setdefault((scheme, _literal_key(notation)), set()).add(node)
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
