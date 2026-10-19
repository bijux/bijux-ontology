"""Evaluate explicit ownership with existing exact selected-fact primitives."""

from collections.abc import Sequence
from typing import get_args

from rdflib import BNode, Graph, URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Location, Report
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.roles.types import TermRole
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.facts.comparison import (
    _collect,
    _graph_selected,
    _metadata,
    compare_selected_facts,
)
from bijux_ontology.vocabulary.facts.types import FactGraph, FactSelection, SelectedFact
from bijux_ontology.vocabulary.sources.types import source_role

from .types import OwnerClaim, OwnershipCheck, OwnershipEvidence


def _facts(
    view: VocabularyView,
    selection: FactSelection,
    budget: Budget,
    issues: IssueCollector,
) -> tuple[SelectedFact, ...]:
    """Keep original scope and graph witnesses; never widen an authoritative view."""
    scopes = []
    for scope, _ in view._reader.scopes:
        budget.consume("work")
        role = source_role(scope)
        if role not in scopes:
            budget.consume("states")
            scopes.append(role)
    for scope, handle in view._vocabulary._source_handles:
        budget.consume("work")
        if source_role(scope) not in scopes:
            continue
        metadata = SnapshotMetadata.capture(handle.prepared, budget)
        if (
            handle.union
            or metadata.union
            or metadata.graph_fidelity != "complete"
            or metadata.literal_fidelity not in ("source_terms", "supplied_terms")
        ):
            raise ResourceError("Ownership requires exact selected RDF assertions")
    selected = FactSelection(
        selection.predicates,
        entities=selection.entities,
        positions=("subject",),
        scopes=tuple(scopes),
        graphs=selection.graphs,
    )
    inventory = _collect(view, selected, _metadata(selected, budget), budget, issues)
    count = len(inventory.facts) + len(inventory.anonymous_facts)
    budget.consume("states", count)
    budget.consume("bytes", 8 * count)
    return (*inventory.facts.values(), *inventory.anonymous_facts)


def _roles(fact: SelectedFact) -> set[TermRole]:
    """Use occurrence roles; an IRI's other legal facets do not change this fact."""
    return {o.role for o in fact.occurrences if o.position == "subject"}


def _subjects(
    view: VocabularyView, selection: FactSelection, budget: Budget
) -> set[str]:
    """Establish current subject coverage independently of protected predicates.

    Editorial assertions establish presence, while references, excluded source
    roles and excluded physical graphs cannot satisfy a requested current term.
    """
    budget.consume("states", len(selection.entities))
    budget.consume("bytes", 8 * len(selection.entities))
    present: set[str] = set()
    for _, kind, name, graph in view._vocabulary._fact_sources(
        selection.scopes, budget
    ):
        if not _graph_selected(FactGraph(kind, name), selection, budget):
            continue
        for term in selection.entities:
            budget.consume("work")
            if term in present:
                continue
            bound_text(term, budget)
            subject = URIRef(term)
            if next(graph.triples((subject, None, None)), None) is not None:
                budget.consume("states")
                budget.consume("bytes", 8)
                present.add(term)
    return present


def _owners(
    term: str, role: TermRole, claims: tuple[OwnerClaim, ...], budget: Budget
) -> tuple[OwnerClaim, ...]:
    """Resolve only explicit matching claims at the effective specificity."""
    found: list[OwnerClaim] = []
    best = (-1, -1, -1)
    for claim in claims:
        selector = claim.term if claim.term is not None else claim.namespace
        assert selector is not None
        budget.consume("work", len(term) + len(selector) + 1)
        if claim.role not in (None, role) or not (
            term == selector if claim.term is not None else term.startswith(selector)
        ):
            continue
        rank = (int(claim.term is not None), len(selector), int(claim.role is not None))
        if rank > best:
            found = []
            best = rank
        if rank == best:
            budget.consume("states")
            budget.consume("bytes", 8)
            found.append(claim)
    return tuple(found)


def _project(facts: tuple[SelectedFact, ...], budget: Budget) -> Graph:
    """Project exact named statement content, deliberately excluding provenance."""
    graph = Graph(bind_namespaces="none")
    for fact in facts:
        budget.consume("work")
        budget.consume("triples")
        for node in fact.statement:
            bound_node(node, budget)
        graph.add(fact.statement)
    return graph


def check_ownership(
    current: VocabularyView,
    selection: FactSelection,
    claims: Sequence[OwnerClaim],
    *,
    publisher: str,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> OwnershipCheck:
    """Check explicit protected outgoing assertions against declared owner views.

    The nonempty entity inventory and predicates define the entire policy.
    Omitted predicates, including permitted editorial annotations, are outside
    this contract. This is no generic RDF/OWL legality or publisher certification.
    An external assertion must duplicate selected authoritative ground content;
    omissions are allowed because an extension need not repeat imported facts.
    New protected assertions are prohibited, without claiming OWL contradiction.
    Anonymous definitions remain unverified. Each claim's selected source roles
    are independent of current's roles; selection scopes must match current's
    view. A source/graph provenance change alone is not definition-content drift.
    Each selected current term requires a subject occurrence in its selected
    physical graphs, including an editorial assertion. Owner evidence cannot
    supply missing current-term coverage.
    """
    if not isinstance(current, VocabularyView) or not isinstance(
        selection, FactSelection
    ):
        raise InputError("Expected a selected VocabularyView and FactSelection")
    if type(publisher) is not str or not publisher.strip():
        raise InputError("Select an explicit nonempty publisher identity")
    if selection.positions != ("subject",) or not selection.entities:
        raise InputError("Ownership requires explicit terms and outgoing subject facts")
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("Expected explicit Limits")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Expected a shared Budget")
    if budget is not None and limits is not None and limits != budget.limits:
        raise InputError("Explicit limits conflict with the shared Budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    evidence: list[OwnershipEvidence] = []
    checked = 0
    requested = len(selection.entities)
    finished = False

    def reserve(count: int = 1) -> None:
        """Prepay result references before native operations can pay findings."""
        operation.consume("states", count)
        operation.consume("bytes", 8 * count)

    def unknown(message: str, term: str = "") -> None:
        """Retain the exact term whose selected ownership cannot be established."""
        issues.append(
            Issue("ontology.ownership_unverified", message, term, kind="unverified")
        )

    try:
        bound_text(publisher, operation)
        supplied = capture_items(claims, OwnerClaim, operation)
        for claim in supplied:
            for text in (claim.owner, claim.term, claim.namespace, claim.role):
                if text is not None:
                    bound_text(text, operation)
        available = set()
        for scope, _ in current._reader.scopes:
            operation.consume("work")
            available.add(source_role(scope))
        if available != set(selection.scopes):
            raise InputError("Ownership scopes must equal the supplied current view")
        facts = _facts(current, selection, operation, issues)
        present = _subjects(current, selection, operation)
        references: dict[tuple[int, str], tuple[SelectedFact, ...]] = {}
        for term in selection.entities:
            bound_text(term, operation)
            if term not in present:
                unknown(
                    "Selected current term has no subject occurrence in scope", term
                )
                checked += 1
                continue
            selected: list[SelectedFact] = []
            for fact in facts:
                operation.consume("work", len(term) + 1)
                if str(fact.statement[0]) == term:
                    reserve()
                    selected.append(fact)
            facets: set[TermRole] = set()
            for fact in selected:
                operation.consume("work", len(fact.occurrences))
                facets.update(_roles(fact))
            current_facets = frozenset(facets)
            for claim in supplied:
                selector = claim.term if claim.term is not None else claim.namespace
                assert selector is not None
                operation.consume("work", len(term) + len(selector) + 1)
                if not (
                    term == selector
                    if claim.term is not None
                    else term.startswith(selector)
                ):
                    continue
                if not any(
                    any(
                        match is claim
                        for match in _owners(term, role, supplied, operation)
                    )
                    for role in current_facets or get_args(TermRole)
                ):
                    continue
                key = id(claim.source), term
                if key not in references:
                    reserve()
                    authoritative_selection = FactSelection(
                        selection.predicates,
                        entities=(term,),
                        positions=("subject",),
                    )
                    references[key] = _facts(
                        claim.source, authoritative_selection, operation, issues
                    )
                for fact in references[key]:
                    operation.consume("work", len(term) + len(fact.occurrences) + 1)
                    if str(fact.statement[0]) == term:
                        roles = _roles(fact)
                        if claim.role is not None:
                            roles.intersection_update((claim.role,))
                        if not current_facets:
                            facets.update(roles)
            if not facets:
                unknown(
                    "No protected current or authoritative assertions "
                    "establish this selection",
                    term,
                )
            for role in sorted(facets):
                matches = _owners(term, role, supplied, operation)
                owners = {claim.owner for claim in matches}
                operation.consume("work", sum(len(f.occurrences) for f in selected))
                reserve(len(selected) + 1)
                original = tuple(f for f in selected if role in _roles(f))
                authority: list[SelectedFact] = []
                for claim in matches:
                    key = id(claim.source), term
                    for fact in references[key]:
                        operation.consume("work", len(term) + len(fact.occurrences) + 1)
                        if str(fact.statement[0]) == term and role in _roles(fact):
                            reserve()
                            authority.append(fact)
                reserve(len(authority))
                authoritative = tuple(authority)
                reserve()
                item = OwnershipEvidence(term, role, matches, original, authoritative)
                evidence.append(item)
                if len(owners) > 1:
                    size = sum(len(owner) for owner in owners)
                    operation.consume(
                        "work", (size + len(owners)) * len(owners).bit_length()
                    )
                    operation.consume("bytes", size + 2 * len(owners))
                    issues.append(
                        Issue(
                            "ontology.owner_conflict",
                            "Effective explicit owner declarations conflict",
                            term,
                            actual=", ".join(sorted(owners)),
                        )
                    )
                elif not matches or role == "unknown":
                    unknown(
                        "No unambiguous supported explicit owner facet was established",
                        term,
                    )
                elif any(
                    isinstance(n, BNode)
                    for f in (*original, *authoritative)
                    for n in (*f.statement, f.graph.name)
                ):
                    unknown(
                        "Anonymous protected definition content remains unverified",
                        term,
                    )
                elif publisher not in owners:
                    before = _project(authoritative, operation)
                    after = _project((*authoritative, *original), operation)
                    content = FactSelection(
                        selection.predicates, entities=(term,), positions=("subject",)
                    )
                    reserve()
                    comparison = compare_selected_facts(
                        before, after, content, budget=operation
                    )
                    evidence[-1] = OwnershipEvidence(
                        term, role, matches, original, authoritative, comparison
                    )
                    retained = IssueCollector(operation)
                    retain_report(comparison.report, operation, retained)
                    issues.extend(i for i in retained if i.kind != "finding")
                    for change in comparison.differences:
                        operation.consume("work")
                        for fact in original:
                            operation.consume(
                                "work",
                                1
                                + sum(
                                    len(str(node))
                                    for node in (
                                        *fact.statement,
                                        *change.fact.statement,
                                    )
                                ),
                            )
                            if fact.statement == change.fact.statement:
                                issues.append(
                                    Issue(
                                        "ontology.definition_redefined",
                                        "Protected assertion is absent from the "
                                        "declared "
                                        "external owner's supplied facts",
                                        term,
                                        actual=str(fact.statement[1]),
                                        details=Location(
                                            source=fact.source_scope,
                                            term=term,
                                            graph=str(fact.graph.name)
                                            if fact.graph.name is not None
                                            else None,
                                        ),
                                    )
                                )
            checked += 1
        finished = True
    except LimitExceeded as error:
        issues.exhausted(str(error), location="ontology.ownership")
    except InputError as error:
        try:
            unavailable = isinstance(error, ResourceError)
            issues.append(
                Issue(
                    "ontology.ownership_unverified"
                    if unavailable
                    else "ontology.ownership_input_error",
                    str(error),
                    error.location,
                    kind="unverified" if unavailable else "input_error",
                    details=Location.from_error(error),
                )
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining ownership scope failure")
    return OwnershipCheck(
        tuple(evidence),
        Report(
            "ontology.ownership",
            checked,
            tuple(issues),
            requested=requested if finished else max(requested, checked + 1),
            complete=finished,
            regime="explicit-protected-outgoing-owner-content",
        ),
    )
