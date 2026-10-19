"""Explicit and ordered-list-implied SKOS collection membership."""

import json
from collections.abc import Mapping, Sequence

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF, SKOS
from rdflib.term import Node

from bijux_ontology.limits import Budget, IssueCollector
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.structure import read_rdf_list
from bijux_ontology.rdf.terms import term_key
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.browsing.navigation import _ordered_collection_members

from ..errors import InputError, LimitExceeded
from ..reports import Issue, Report
from .selection import CollectionScope


def collection_memberships(
    graph: Graph,
    explicit: Mapping[Node, frozenset[Node]],
    *,
    scope: CollectionScope,
    budget: Budget,
    issues: IssueCollector,
) -> dict[Node, frozenset[Node]]:
    """Retain known members without treating missing ordered data as complete."""
    collections = scope.required | scope.supporting
    cached: dict[Node, tuple[Node, ...] | InputError] = {}
    result: dict[Node, frozenset[Node]] = {}
    for collection in sorted(collections, key=lambda node: node.n3()):
        budget.consume("work")
        values = set(explicit.get(collection, ()))
        heads = tuple(graph.objects(collection, SKOS.memberList))
        ordered = (collection, RDF.type, SKOS.OrderedCollection) in graph
        if collection in scope.required and (ordered or heads) and len(heads) != 1:
            issues.append(
                Issue(
                    "skos.policy.member_list_ambiguous",
                    "Complete ordered membership needs one supplied member list",
                    str(collection),
                    kind="unverified",
                )
            )
        for head in heads:
            budget.consume("work")
            if head not in cached:
                try:
                    members = read_rdf_list(
                        graph, head, max_items=budget.limits.max_work, budget=budget
                    )
                    budget.consume("work", len(members))
                    budget.consume("states", len(members))
                    cached[head] = members
                except LimitExceeded:
                    raise
                except InputError as error:
                    cached[head] = error
            cached_members = cached[head]
            if isinstance(cached_members, InputError):
                if collection in scope.required:
                    issues.append(
                        Issue(
                            "skos.policy.member_list_malformed",
                            str(cached_members),
                            str(collection),
                        )
                    )
            else:
                values.update(cached_members)
        budget.consume("states", len(values))
        result[collection] = frozenset(values)
    return result


def check_collection_sequence(
    view: VocabularyView,
    collection: str,
    *,
    expected: Sequence[str] | None = None,
    minimum_size: int | None = None,
    maximum_size: int | None = None,
    unique_members: bool = False,
) -> Report:
    """Check an authored member list, independently of extra direct members.

    Expected positions are exact named IRIs. Size counts list positions and
    uniqueness compares RDF terms within this owned view, without entailment.
    Omitting expected disables the order constraint. Missing or ambiguous lists
    are unverified; proved prefix violations survive incomplete list tails.
    """
    for bound in (minimum_size, maximum_size):
        if bound is not None and (type(bound) is not int or bound < 0):
            raise InputError("Collection sizes must be nonnegative integers")
    if (
        minimum_size is not None
        and maximum_size is not None
        and minimum_size > maximum_size
    ):
        raise InputError("Minimum collection size exceeds maximum")
    if type(unique_members) is not bool:
        raise InputError("unique_members must be boolean")
    if expected is not None and (
        not isinstance(expected, Sequence) or isinstance(expected, str | bytes)
    ):
        raise InputError("Expected members must be a finite IRI sequence")
    budget = Budget(view.limits)
    issues = IssueCollector(budget)
    structural = IssueCollector(budget)
    captured: list[URIRef] | None = None
    count = 0
    checked = 0
    complete = False
    try:
        _charge_iri(collection, budget)
        if expected is not None:
            length = len(expected)
            if length > budget.limits.max_states:
                raise LimitExceeded("Expected sequence exceeds max_states")
            captured = []
            for index in range(length):
                budget.consume("work")
                budget.consume("states")
                value = expected[index]
                if not isinstance(value, str) or isinstance(value, BNode | Literal):
                    raise InputError("Expected members must be named IRI strings")
                _charge_iri(value, budget)
                captured.append(URIRef(value))
        seen: dict[tuple[str | None, ...], int] = {}
        maximum_reported = False
        for membership in _ordered_collection_members(
            view, collection, budget, structural
        ):
            budget.consume("work")
            position = count
            count += 1
            checked += 1
            if captured is not None and (
                position >= len(captured) or membership.member != captured[position]
            ):
                issues.append(
                    Issue(
                        "skos.policy.collection_sequence_mismatch",
                        "Member does not match the selected authored position",
                        collection,
                        evidence_json=json.dumps({"position": position}),
                    )
                )
            if unique_members:
                key = term_key(membership.member)
                if key in seen:
                    issues.append(
                        Issue(
                            "skos.policy.collection_duplicate_member",
                            "Selected list repeats an RDF member term",
                            collection,
                            evidence_json=json.dumps(
                                {
                                    "first_position": seen[key],
                                    "position": position,
                                }
                            ),
                        )
                    )
                else:
                    budget.consume("states")
                    seen[key] = position
            if (
                maximum_size is not None
                and count > maximum_size
                and not maximum_reported
            ):
                issues.append(
                    Issue(
                        "skos.policy.collection_size_mismatch",
                        "Selected list exceeds maximum size",
                        collection,
                        expected=str(maximum_size),
                        actual=str(count),
                    )
                )
                maximum_reported = True
        complete = not structural
        if complete:
            # Completing the list establishes its shape even when rdf:nil has
            # no member comparisons. Tail-dependent bounds require this proof.
            budget.consume("work")
            checked += 1
            if captured is not None and count < len(captured):
                issues.append(
                    Issue(
                        "skos.policy.collection_sequence_mismatch",
                        "Selected list ends before the expected sequence",
                        collection,
                        expected=str(len(captured)),
                        actual=str(count),
                    )
                )
            if minimum_size is not None and count < minimum_size:
                issues.append(
                    Issue(
                        "skos.policy.collection_size_mismatch",
                        "Selected list is below minimum size",
                        collection,
                        expected=str(minimum_size),
                        actual=str(count),
                    )
                )
    except LimitExceeded as error:
        issues.exhausted(str(error), location=collection)
        complete = False
    list.extend(issues, structural)
    return Report(
        "skos.collection_sequence",
        checked,
        tuple(issues),
        requested=checked if complete else checked + 1,
        complete=complete,
        families=("skos.collection_sequence",),
    )


def _charge_iri(value: str, budget: Budget) -> None:
    """Validate exact caller IRIs while bounding retained expectation bytes."""
    if not isinstance(value, str):
        raise InputError("Expected an absolute IRI string")
    if len(value) > budget.limits.max_bytes:
        raise LimitExceeded("IRI exceeds max_bytes")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise InputError("IRI must contain valid Unicode text") from error
    if size > budget.limits.max_bytes:
        raise LimitExceeded("IRI exceeds max_bytes")
    budget.consume("bytes", size)
    absolute_iri(value)
