"""Controlled concept selection over existing owned scheme/collection witnesses."""

from collections.abc import Sequence, Set
from typing import Literal

from rdflib import BNode, URIRef
from rdflib.namespace import RDF, SKOS
from rdflib.term import Node

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.collections.expansion import expand_collection
from bijux_ontology.vocabulary.collections.types import CollectionOccurrence
from bijux_ontology.vocabulary.inspection import VocabularyView
from bijux_ontology.vocabulary.navigation import Membership, scheme_members
from bijux_ontology.vocabulary.results import Deprecation

from ..publication.coverage import _Hierarchy, _roots, _walk
from ..publication.scope import _PublicationRead
from ..publication.types import ConceptReachability, _iri
from .types import ConceptSubset


def select_concepts(
    view: VocabularyView,
    *,
    scheme: str | None = None,
    collection: str | None = None,
    roots: Sequence[str] | None = None,
    closed: bool = False,
    deprecation: Literal["allow", "exclude", "require-active"] = "exclude",
    direction: Literal["descendants", "ancestors", "both"] = "descendants",
    cross_scheme: Literal["exclude", "include"] = "exclude",
    budget: Budget | None = None,
) -> ConceptSubset:
    """Select one scheme, nested collection, or rooted selected-scheme subset.

    Scheme scope uses existing finite SKOS Concept roles. Roots can be interior
    concepts; authored broader/inverse narrower links supply reachability.
    Collection scope reuses the existing explicit nested concept expansion.
    Closure is the consumer's declared completeness boundary, never OWL negation.

    Allow ignores lifecycle. Exclude forbids asserted deprecated members and
    permits absent metadata. Require-active needs explicit nondeprecated state.
    Conflicting, malformed or incomplete lifecycle remains unresolved.
    """
    if not isinstance(view, VocabularyView) or (scheme is None) == (collection is None):
        raise InputError("Select one owned scheme or collection scope")
    if type(closed) is not bool or deprecation not in (
        "allow",
        "exclude",
        "require-active",
    ):
        raise InputError("Select explicit supported closure and deprecation policies")
    if direction not in ("descendants", "ancestors", "both") or cross_scheme not in (
        "exclude",
        "include",
    ):
        raise InputError("Select supported hierarchy policies")
    if collection is not None and (
        roots is not None or direction != "descendants" or cross_scheme != "exclude"
    ):
        raise InputError("Hierarchy options apply only to scheme selections")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Expected a shared Budget")
    operation = budget or Budget(view.limits)
    issues = IssueCollector(operation)
    children: list[Report] = []
    read = _PublicationRead(view, operation)
    hierarchy = _Hierarchy(read)
    witnesses: list[Membership | ConceptReachability | CollectionOccurrence] = []
    reached: list[ConceptReachability] = []
    lifecycle: list[tuple[URIRef, Deprecation]] = []
    values: list[URIRef] = []
    excluded: list[URIRef] = []
    scheme_concepts: set[Node] = set()
    selected: Set[Node] = scheme_concepts
    collection_witnesses: tuple[CollectionOccurrence, ...] = ()
    exhausted: str | None = None
    complete = True
    checked = 0
    anchor = scheme if scheme is not None else collection
    assert anchor is not None
    try:
        _iri(anchor)
        if len(anchor) > operation.limits.max_bytes:
            raise LimitExceeded("Concept selection IRI exceeds max_bytes")
        size = len(anchor.encode("utf-8"))
        if size > operation.limits.max_bytes:
            raise LimitExceeded("Concept selection IRI exceeds max_bytes")
        operation.consume("bytes", size)
        if scheme is not None:
            selected_roots = _roots(roots, operation) if roots is not None else None
            if selected_roots == ():
                raise InputError("Rooted subsets require a nonempty root selection")
            membership = scheme_members(view, scheme, budget=operation)
            children.append(membership.report)
            complete &= membership.report.complete
            classified: dict[Node, bool] = {}
            for membership_item in membership.items:
                operation.consume("work")
                if not isinstance(membership_item.member, URIRef | BNode):
                    issues.append(
                        Issue(
                            "skos.subset.scope_unverified",
                            "Membership does not establish a concept resource",
                            anchor,
                            kind="unverified",
                        )
                    )
                    complete = False
                    continue
                if membership_item.member not in classified:
                    operation.consume("states")
                    classified[membership_item.member] = hierarchy.concept(
                        membership_item.member
                    )
                if classified[membership_item.member]:
                    operation.consume("states")
                    scheme_concepts.add(membership_item.member)
                    if selected_roots is None:
                        witnesses.append(membership_item)
            if not membership.items:
                typed = view.statements(
                    URIRef(scheme), predicates=(str(RDF.type),), budget=operation
                )
                children.append(typed.report)
                if not any(edge.object == SKOS.ConceptScheme for edge in typed.items):
                    complete = False
                    issues.append(
                        Issue(
                            "skos.subset.scope_unverified",
                            "Selected scheme is not supplied",
                            scheme,
                            kind="unverified",
                        )
                    )
            if selected_roots is not None:
                for root in selected_roots:
                    operation.consume("work")
                    if root not in scheme_concepts:
                        complete = False
                        issues.append(
                            Issue(
                                "skos.subset.scope_unverified",
                                "Selected root is not an established concept "
                                "of this scheme",
                                str(root),
                                kind="unverified",
                            )
                        )
                operation.consume("states", len(selected_roots))
                walk_roots: set[Node] = {
                    root for root in selected_roots if root in scheme_concepts
                }
                _walk(
                    hierarchy,
                    walk_roots,
                    scheme_concepts,
                    direction,
                    cross_scheme,
                    reached,
                )
                operation.consume("states", len(reached))
                selected = {item.concept for item in reached}
        else:
            assert collection is not None
            expansion = expand_collection(
                view, collection, nested="expand", budget=operation
            )
            children.append(expansion.report)
            complete &= expansion.report.complete
            selected = expansion.concepts
            collection_witnesses = expansion.members
        checked += 1
        operation.consume("states", len(selected))
        for member in sorted(selected, key=lambda node: node.n3()):
            operation.consume("work")
            if not isinstance(member, URIRef):
                complete = False
                issues.append(
                    Issue(
                        "skos.subset.scope_unverified",
                        "A controlled IRI set cannot establish "
                        "an anonymous member name",
                        anchor,
                        kind="unverified",
                    )
                )
                continue
            operation.consume("states", 3)
            operation.consume("bytes", len(str(member).encode("utf-8")))
            if deprecation == "allow":
                values.append(member)
                checked += 1
                continue
            state = view.deprecation(str(member), budget=operation)
            lifecycle.append((member, state))
            children.append(state.report)
            if state.report.complete and state.state == "deprecated":
                excluded.append(member)
            elif state.report.complete and (
                state.state == "active"
                or state.state == "absent"
                and deprecation == "exclude"
            ):
                values.append(member)
            else:
                complete = False
                issues.append(
                    Issue(
                        "skos.subset.lifecycle_unverified",
                        "Selected lifecycle policy is not established for this member",
                        str(member),
                        kind="unverified",
                        actual=state.state,
                    )
                )
            checked += 1
        if not closed:
            complete = False
            issues.append(
                Issue(
                    "skos.subset.scope_unverified",
                    "Open membership cannot establish complete allowed-set "
                    "coverage or exclusion by absence",
                    anchor,
                    kind="unverified",
                )
            )
    except LimitExceeded as error:
        complete = False
        exhausted = str(error)
    complete &= read.complete
    list.extend(issues, read.issues)
    if exhausted is not None:
        issues.exhausted(exhausted, location=anchor)
    final_witnesses = (
        tuple(reached)
        if roots is not None
        else collection_witnesses
        if collection is not None
        else tuple(witnesses)
    )
    report = Report(
        "skos.concept_subset",
        checked,
        tuple(issues),
        requested=checked if complete else checked + 1,
        complete=complete,
        regime="consumer-controlled-concepts",
        children=tuple(children),
    )
    return ConceptSubset(
        tuple(values),
        tuple(excluded),
        closed,
        report,
        final_witnesses,
        tuple(lifecycle),
    )
