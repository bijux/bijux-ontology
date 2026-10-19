"""Bounded checks of caller-declared SKOS-XL label relation inventories."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal as Direction
from typing import cast

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import RDF, SKOS, XSD
from rdflib.term import Node

from bijux_ontology.absence import Absent
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector
from bijux_ontology.rdf.terms import term_key
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.browsing.results import Selection

from ..xl import XLLabel, describe_xl_label
from .types import XLRelation, XLRelationPolicy


@dataclass(frozen=True)
class _Endpoint:
    label: XLLabel | None
    form: Literal | None
    valid: bool | None


class _RelationRead:
    """Own only operation-local descriptions keyed by actual resource terms."""

    def __init__(self, view: VocabularyView, budget: Budget) -> None:
        self.view = view
        self.budget = budget
        self.issues = IssueCollector(budget)
        self.complete = True
        self.labels: dict[Node, _Endpoint] = {}

    def endpoint(self, node: Node) -> _Endpoint:
        """Retain known shape failures even if a later description is incomplete."""
        if node in self.labels:
            return self.labels[node]
        self.budget.consume("states")
        if not isinstance(node, URIRef | BNode):
            self.issues.append(
                Issue(
                    "skos.xl_relation.endpoint_not_resource",
                    "XL relation endpoint must be a label resource",
                    actual=node.n3(),
                )
            )
            result = _Endpoint(None, None, False)
        else:
            selected = describe_xl_label(self.view, node, budget=self.budget)
            list.extend(self.issues, selected.report.issues)
            self.complete &= selected.report.complete
            label = selected.items[0]
            valid: bool | None = True if selected.report.complete else None
            forms: dict[tuple[str | None, ...], Literal] = {}
            for edge in label.forms:
                self.budget.consume("work")
                value = edge.object
                if not isinstance(value, Literal) or (
                    value.language is None and value.datatype not in (None, XSD.string)
                ):
                    valid = False
                    self.issues.append(
                        Issue(
                            "skos.xl_relation.form_shape",
                            "XL relation label requires a string literal form",
                            str(node),
                            actual=value.n3(),
                        )
                    )
                else:
                    forms[term_key(value)] = value
            if len(forms) > 1:
                valid = False
                self.issues.append(
                    Issue(
                        "skos.xl_relation.form_conflict",
                        "XL relation label has distinct literal forms",
                        str(node),
                    )
                )
            elif not forms and valid is True:
                valid = None
            for edge in label.metadata:
                self.budget.consume("work")
                if edge.predicate == RDF.type and edge.object in (
                    SKOS.Concept,
                    SKOS.ConceptScheme,
                    SKOS.Collection,
                    SKOS.OrderedCollection,
                ):
                    valid = False
                    self.issues.append(
                        Issue(
                            "skos.xl_relation.role_conflict",
                            "XL label has a disjoint SKOS resource role",
                            str(node),
                            actual=str(edge.object),
                        )
                    )
            result = _Endpoint(
                label, next(iter(forms.values())) if len(forms) == 1 else None, valid
            )
        self.labels[node] = result
        return result


def _language(expected: str | Absent | None, actual: str | None) -> bool:
    if expected is None:
        return True
    if isinstance(expected, Absent):
        return actual is None
    return actual is not None and expected.lower() == actual.lower()


def _orientation(
    source: _Endpoint, target: _Endpoint, policy: XLRelationPolicy
) -> bool | None:
    if source.valid is False or target.valid is False:
        return False
    if source.valid is None or target.valid is None:
        return None
    assert source.form is not None and target.form is not None
    if not _language(policy.source_language, source.form.language) or not _language(
        policy.target_language, target.form.language
    ):
        return False
    same_language = (source.form.language or "").lower() == (
        target.form.language or ""
    ).lower()
    same_form = term_key(source.form) == term_key(target.form)
    return all(
        comparison == "any" or same == (comparison == "same")
        for comparison, same in (
            (policy.language_relation, same_language),
            (policy.form_relation, same_form),
        )
    )


def _policies(
    supplied: Sequence[XLRelationPolicy], budget: Budget
) -> dict[str, XLRelationPolicy]:
    if not isinstance(supplied, Sequence) or isinstance(
        cast(object, supplied), (str, bytes)
    ):
        raise InputError("XL relations require a finite policy sequence")
    count = len(supplied)
    if not count:
        raise InputError("XL relations require an explicit nonempty policy inventory")
    budget.consume("states", count)
    owned = tuple(supplied[index] for index in range(count))
    result = {}
    for policy in owned:
        if not isinstance(policy, XLRelationPolicy):
            raise InputError("Expected typed XLRelationPolicy values")
        for predicate in (policy.predicate, *policy.subrelations):
            budget.consume("work")
            budget.consume("states")
            if predicate in result:
                raise InputError("XL predicate belongs to multiple selected policies")
            result[predicate] = policy
            size = len(predicate.encode("utf-8"))
            if size > budget.limits.max_bytes:
                raise LimitExceeded("XL predicate exceeds selected max_bytes")
            budget.consume("bytes", size)
        for language in (policy.source_language, policy.target_language):
            if isinstance(language, str):
                size = len(language.encode("utf-8"))
                if size > budget.limits.max_bytes:
                    raise LimitExceeded("XL language exceeds selected max_bytes")
                budget.consume("bytes", size)
    return result


def check_xl_relations(
    view: VocabularyView,
    policies: Sequence[XLRelationPolicy],
    *,
    budget: Budget | None = None,
) -> Selection[XLRelation]:
    """Check supplied relation assertions without inferring subproperties or inverses.

    Every endpoint needs one supported literal form; explicit XL.Label typing is
    not mandatory. Missing descriptions remain unknown, and an empty completed
    assertion selection promises no relation existence. Form equality never
    identifies label resources. All policy ownership and semantic work share the
    selected budget; known findings survive later exhaustion.
    """
    if not isinstance(view, VocabularyView) or (
        budget is not None and not isinstance(budget, Budget)
    ):
        raise InputError("Expected an owned VocabularyView and optional Budget")
    operation = budget if budget is not None else Budget(view.limits)
    read = _RelationRead(view, operation)
    relations = []
    checked = 0
    try:
        inventory = _policies(policies, operation)
        selected = view.all_statements(budget=operation)
        list.extend(read.issues, selected.report.issues)
        read.complete &= selected.report.complete
        for edge in selected.items:
            operation.consume("work")
            policy = inventory.get(str(edge.predicate))
            if policy is None:
                continue
            evidence = json.dumps(
                {
                    "assertion": [
                        term_key(edge.subject),
                        term_key(edge.predicate),
                        term_key(edge.object),
                    ],
                    "scope": edge.scope,
                }
            )
            operation.consume("bytes", len(evidence.encode("utf-8")))
            if policy.forbidden:
                read.issues.append(
                    Issue(
                        "skos.xl_relation.forbidden",
                        "Selected XL relation is forbidden by its caller policy",
                        str(edge.subject),
                        actual=str(edge.predicate),
                        evidence_json=evidence,
                    )
                )
            source, target = read.endpoint(edge.subject), read.endpoint(edge.object)
            matched = _orientation(source, target, policy)
            orientation: Direction["forward", "reversed"] | None = (
                "forward" if matched is True else None
            )
            if policy.direction == "either" and matched is not True:
                reverse = _orientation(target, source, policy)
                if reverse is True:
                    matched, orientation = True, "reversed"
                elif reverse is None:
                    matched = None
            if matched is False and source.valid is True and target.valid is True:
                read.issues.append(
                    Issue(
                        "skos.xl_relation.policy_mismatch",
                        "XL endpoints violate selected language or form constraints",
                        str(edge.subject),
                        actual=str(edge.object),
                        evidence_json=evidence,
                    )
                )
            operation.consume("states")
            relations.append(
                XLRelation(
                    edge,
                    source.label,
                    target.label,
                    False if policy.forbidden else matched,
                    orientation,
                )
            )
            checked += 1
    except LimitExceeded as error:
        read.complete = False
        read.issues.exhausted(str(error))
    return Selection(
        tuple(relations),
        Report(
            "skos.xl_relations",
            checked,
            tuple(read.issues),
            allow_empty=True,
            complete=read.complete,
            requested=checked if read.complete else checked + 1,
            regime="asserted",
            families=("skos.xl-relations",),
        ),
    )
