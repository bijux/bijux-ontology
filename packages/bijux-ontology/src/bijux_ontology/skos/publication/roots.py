"""Opt-in published top-concept inventories and authored parent policies."""

import json

from rdflib import BNode, URIRef
from rdflib.namespace import SKOS
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.terms import term_key
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.browsing.results import Edge

from .scope import _PublicationRead
from .types import TopConceptCheck, TopConceptPolicy


def check_top_concepts(
    view: VocabularyView, policy: TopConceptPolicy, *, budget: Budget | None = None
) -> TopConceptCheck:
    """Check one selected scheme's consumer policy without changing SKOS integrity.

    Either topConceptOf or hasTopConcept publishes a root. Broader checks include
    authored broader and inverse narrower only. ``forbid-within-scheme`` allows
    a parent with a proved other-scheme inventory; missing membership is unknown.
    Known inventory violations survive later bounded or unresolved lookups.
    """
    if not isinstance(view, VocabularyView) or not isinstance(policy, TopConceptPolicy):
        raise InputError("Expected an owned VocabularyView and typed TopConceptPolicy")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Expected a shared Budget")
    operation = budget if budget is not None else Budget(view.limits)
    read = _PublicationRead(view, operation)
    scheme = URIRef(policy.scheme)
    roots: frozenset[Node] = frozenset()
    assertions: tuple[Edge, ...] = ()
    checked = 0
    try:
        for value in (policy.scheme, *policy.required, *(policy.allowed or ())):
            operation.consume("work")
            size = len(value.encode("utf-8"))
            if size > operation.limits.max_bytes:
                raise LimitExceeded("Publication IRI exceeds selected max_bytes")
            operation.consume("bytes", size)
        selected = read.scheme(scheme)
        roots, assertions = selected.roots, selected.assertions
        if selected.complete:
            checked += 1
        for required in policy.required:
            operation.consume("work")
            if URIRef(required) in roots:
                checked += 1
            elif selected.complete:
                checked += 1
                read.issues.append(
                    Issue(
                        "skos.publication.required_root_missing",
                        "Selected scheme does not publish a required top concept",
                        required,
                        expected=policy.scheme,
                    )
                )
        if policy.allowed is not None:
            allowed = frozenset(map(URIRef, policy.allowed))
            for root in sorted(roots, key=lambda node: node.n3()):
                operation.consume("work")
                if root not in allowed:
                    read.issues.append(
                        Issue(
                            "skos.publication.root_not_allowed",
                            "Selected scheme publishes a root outside "
                            "its allowed inventory",
                            str(root),
                            actual=policy.scheme,
                        )
                    )
            checked += int(selected.complete)
        if policy.broader != "allow":
            for root in sorted(roots, key=lambda node: node.n3()):
                assert isinstance(root, URIRef | BNode)
                parents = read.broader(root)
                checked += int(parents.report.complete)
                for edge in parents.items:
                    operation.consume("work")
                    parent = (
                        edge.object if edge.predicate == SKOS.broader else edge.subject
                    )
                    forbidden = policy.broader == "forbid"
                    if not forbidden:
                        if not read._resource(parent, root):
                            continue
                        assert isinstance(parent, URIRef | BNode)
                        memberships = read.memberships(parent)
                        forbidden = scheme in memberships.schemes
                        if not forbidden and (
                            not memberships.complete or not memberships.schemes
                        ):
                            read.complete = False
                            read.issues.append(
                                Issue(
                                    "skos.publication.broader_scope_unverified",
                                    "Parent lacks complete scheme membership needed "
                                    "by the selected root policy",
                                    str(root),
                                    actual=str(parent),
                                    kind="unverified",
                                )
                            )
                    if forbidden:
                        evidence = json.dumps(
                            {
                                "scheme": term_key(scheme),
                                "assertion": [
                                    term_key(edge.subject),
                                    term_key(edge.predicate),
                                    term_key(edge.object),
                                ],
                                "scope": edge.scope,
                            }
                        )
                        operation.consume("bytes", len(evidence.encode("utf-8")))
                        read.issues.append(
                            Issue(
                                "skos.publication.root_has_broader",
                                "Published root violates the selected "
                                "authored broader policy",
                                str(root),
                                evidence_json=evidence,
                            )
                        )
    except LimitExceeded as error:
        read.complete = False
        read.issues.exhausted(str(error), location=policy.scheme)
    report = Report(
        "skos.top_concepts",
        checked,
        tuple(read.issues),
        requested=checked if read.complete else checked + 1,
        complete=read.complete,
        families=("skos.publication",),
    )
    return TopConceptCheck(
        scheme, tuple(sorted(roots, key=lambda node: node.n3())), assertions, report
    )
