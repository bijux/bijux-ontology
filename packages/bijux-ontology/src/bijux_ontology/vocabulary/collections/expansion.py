"""Bounded nested SKOS membership using the owned view and shared list decoder."""

import json
from collections.abc import Generator
from dataclasses import dataclass
from itertools import chain
from typing import Literal

from rdflib import BNode, URIRef
from rdflib import Literal as RdfLiteral
from rdflib.namespace import RDF, SKOS
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.limits import Budget, IssueCollector
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.terms import term_key
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.browsing.navigation import (
    Membership,
    _ordered_collection_members,
)

from .types import CollectionExpansion, CollectionOccurrence


@dataclass(frozen=True)
class _MemberInfo:
    kind: Literal["concept", "collection", "unresolved"]
    direct: tuple[Membership, ...] = ()
    ordered: bool = False
    complete: bool = True


class _Expansion:
    """Operation-local metadata/list reuse; output paths retain multiplicity."""

    def __init__(self, view: VocabularyView, budget: Budget) -> None:
        self.view = view
        self.budget = budget
        self.issues = IssueCollector(budget)
        self.info: dict[Node, _MemberInfo] = {}
        self.decoded: dict[Node, tuple[Membership, ...]] = {}
        self.complete = True
        self.ordered = False
        self.unordered = False

    def describe(self, node: Node) -> _MemberInfo:
        """Classify actual selected RDF roles once, without an inferred leaf kind."""
        if node in self.info:
            return self.info[node]
        self.budget.consume("states")
        self.budget.consume("bytes", len(node.n3().encode("utf-8")))
        if not isinstance(node, URIRef | BNode):
            self.issues.append(
                Issue(
                    "terms.collection_member_invalid",
                    "Collection members must be concept or collection resources",
                    str(node),
                )
            )
            result = _MemberInfo("unresolved", complete=False)
        else:
            selected = self.view.statements(
                node,
                predicates=(str(RDF.type), str(SKOS.member), str(SKOS.memberList)),
                budget=self.budget,
            )
            list.extend(self.issues, selected.report.issues)
            kinds = {
                edge.object for edge in selected.items if edge.predicate == RDF.type
            }
            direct = tuple(
                Membership(edge.object, edge)
                for edge in selected.items
                if edge.predicate == SKOS.member
            )
            ordered = SKOS.OrderedCollection in kinds or any(
                edge.predicate == SKOS.memberList for edge in selected.items
            )
            collection = ordered or SKOS.Collection in kinds or bool(direct)
            concept = SKOS.Concept in kinds
            if not selected.report.complete or concept == collection:
                self.issues.append(
                    Issue(
                        "terms.collection_member_unverified",
                        "Member needs one unambiguous asserted concept "
                        "or collection role",
                        str(node),
                        kind="unverified",
                    )
                )
                result = _MemberInfo("unresolved", complete=False)
            elif concept:
                result = _MemberInfo("concept")
            else:
                result = _MemberInfo("collection", direct, ordered)
        self.info[node] = result
        if not result.complete:
            self.complete = False
        return result

    def members(
        self, node: URIRef | BNode, info: _MemberInfo
    ) -> Generator[Membership, None, None]:
        """Stream first-use positions into an operation cache, then replay them."""
        self.ordered |= info.ordered
        self.unordered |= not info.ordered or bool(info.direct)
        if node in self.decoded:
            yield from self.decoded[node]
            return
        values: list[Membership] = []
        structural = IssueCollector(self.budget)
        decoder = (
            _ordered_collection_members(self.view, node, self.budget, structural)
            if info.ordered
            else None
        )
        try:
            if decoder is not None:
                for value in decoder:
                    values.append(value)
                    yield value
            for value in info.direct:
                self.budget.consume("states")
                values.append(value)
                yield value
        finally:
            if decoder is not None:
                decoder.close()
            self.decoded[node] = tuple(values)
            list.extend(self.issues, structural)
            if structural:
                self.complete = False

    def path(
        self, parent: tuple[Membership, ...], step: Membership
    ) -> tuple[Membership, ...]:
        """Charge the entire retained inclusion path before allocating its tuple."""
        length = len(parent) + 1
        self.budget.consume("work", length)
        self.budget.consume("states", 2 * length)
        self.budget.consume(
            "bytes",
            sum(
                len(item.member.n3().encode("utf-8"))
                + len(item.assertion.subject.n3().encode("utf-8"))
                + len(item.assertion.scope.encode("utf-8"))
                for item in chain(parent, (step,))
            ),
        )
        return (*parent, step)


def expand_collection(
    view: VocabularyView,
    collection: str,
    *,
    nested: Literal["retain", "expand"] = "expand",
    max_depth: int | None = None,
    budget: Budget | None = None,
) -> CollectionExpansion:
    """Retain child collections or expand typed concepts with exact inclusion paths.

    Metadata and list decoding are reused within this call. Each inclusion path
    and duplicate occurrence consumes the shared finite budget independently.
    Containment cycles and unresolved tails retain proved results and diagnostics.
    Unordered membership can complete a concept set, but never supplies total
    order. ``max_depth`` bounds child-collection nesting below the selected root.
    """
    if not isinstance(view, VocabularyView):
        raise InputError("Collection expansion requires an owned VocabularyView")
    if not isinstance(collection, str) or isinstance(collection, BNode | RdfLiteral):
        raise InputError("Collection expansion requires a named IRI root")
    absolute_iri(collection)
    if nested not in ("retain", "expand"):
        raise InputError("Nested collection mode must be retain or expand")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Expected a shared Budget")
    operation = budget if budget is not None else Budget(view.limits)
    maximum = operation.limits.max_depth if max_depth is None else max_depth
    if type(maximum) is not int or maximum < 0:
        raise InputError("Collection depth requires a nonnegative integer")
    maximum = min(maximum, operation.limits.max_depth)
    state = _Expansion(view, operation)
    root = URIRef(collection)
    output: list[CollectionOccurrence] = []
    concepts: set[Node] = set()
    frames: list[
        tuple[
            Generator[Membership, None, None], tuple[Membership, ...], tuple[Node, ...]
        ]
    ] = []
    try:
        info = state.describe(root)
        if info.kind != "collection":
            state.complete = False
            state.issues.append(
                Issue(
                    "terms.collection_root_unverified",
                    "Selected root does not establish a collection role",
                    str(root),
                    kind="unverified",
                )
            )
        else:
            frames.append((state.members(root, info), (), (root,)))
            while frames:
                members, parent_path, ancestors = frames[-1]
                step = next(members, None)
                if step is None:
                    frames.pop()
                    continue
                operation.consume("work")
                path = state.path(parent_path, step)
                child = step.member
                child_info = state.describe(child)
                terminal = child_info.kind != "collection" or nested == "retain"
                if not terminal:
                    operation.consume("work", len(ancestors))
                    if child in ancestors:
                        state.complete = False
                        state.issues.append(
                            Issue(
                                "terms.collection_containment_cycle",
                                "Nested collection containment returns to an ancestor; "
                                "remove or retain this inclusion",
                                str(child),
                                evidence_json=json.dumps(
                                    {"path": [term_key(item.member) for item in path]}
                                ),
                            )
                        )
                        terminal = True
                    elif len(ancestors) > maximum:
                        state.complete = False
                        state.issues.exhausted(
                            "Nested collection expansion exceeded max_depth",
                            location=str(child),
                        )
                        terminal = True
                    else:
                        assert isinstance(child, URIRef | BNode)
                        frames.append(
                            (
                                state.members(child, child_info),
                                path,
                                (*ancestors, child),
                            )
                        )
                if terminal:
                    new_concept = child_info.kind == "concept" and child not in concepts
                    operation.consume("states", 2 + int(new_concept))
                    output.append(CollectionOccurrence(child, child_info.kind, path))
                    if new_concept:
                        concepts.add(child)
    except LimitExceeded as error:
        state.complete = False
        state.issues.exhausted(str(error), location=str(root))
    finally:
        for iterator, _, _ in reversed(frames):
            iterator.close()
    ordering: Literal["total", "unordered", "mixed", "incomplete"] = (
        "incomplete"
        if not state.complete
        else "mixed"
        if state.ordered and state.unordered
        else "unordered"
        if state.unordered
        else "total"
    )
    checked = len(state.info)
    report = Report(
        "terms.collection_expansion",
        checked,
        tuple(state.issues),
        requested=checked if state.complete else checked + 1,
        complete=state.complete,
        regime="asserted",
        families=("terms.collection_expansion",),
    )
    return CollectionExpansion(
        root,
        nested,
        tuple(output),
        frozenset(concepts),
        tuple(item.member for item in output) if ordering == "total" else None,
        ordering,
        report,
    )
