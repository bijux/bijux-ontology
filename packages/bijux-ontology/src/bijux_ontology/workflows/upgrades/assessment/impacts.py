"""Direct and asserted hierarchy impacts with original deprecation evidence."""

from collections import defaultdict, deque
from collections.abc import Sequence

from rdflib import URIRef
from rdflib.namespace import OWL, RDFS

from bijux_ontology.diagnostics.errors import LimitExceeded
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import Budget, IssueCollector
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.comparison import (
    Change,
    VocabularyChanges,
)
from bijux_ontology.vocabulary.facts.types import FactSelection
from bijux_ontology.workflows.upgrades.facts import (
    change_roles,
    role_matches,
)
from bijux_ontology.workflows.upgrades.uses import Use, UseIndex

from .types import AffectedUse, DeprecatedUse


def _dependencies(change: Change) -> tuple[tuple[str, str], ...]:
    """Preserve class/property direction instead of traversing a mixed hierarchy."""
    domains: tuple[str, ...] = ()
    predicate = URIRef(change.predicate) if change.predicate is not None else None
    if change.kind in ("term-removed", "kinds"):
        domains = ("class", "property")
    elif change.kind == "characteristics":
        domains = ("property",)
    elif change.kind == "disjointness":
        domains = ("property",) if predicate == OWL.propertyDisjointWith else ("class",)
    elif change.kind == "structure":
        domains = (
            ("class",)
            if predicate
            in (
                RDFS.subClassOf,
                OWL.equivalentClass,
            )
            else ("property",)
            if predicate
            in (
                RDFS.subPropertyOf,
                OWL.equivalentProperty,
                OWL.inverseOf,
                RDFS.domain,
                RDFS.range,
            )
            else ()
        )
    seeds = {(change.iri, domain) for domain in domains if change.iri}
    if change.kind in ("structure", "disjointness"):
        for node in (*change.before, *change.after):
            if isinstance(node, URIRef):
                for domain in (
                    ("class",)
                    if predicate
                    in (
                        RDFS.domain,
                        RDFS.range,
                    )
                    else domains
                ):
                    seeds.add((str(node), domain))
    return tuple(sorted(seeds))


def _affected_uses(
    changes: VocabularyChanges,
    uses: UseIndex,
    views: tuple[VocabularyView, ...],
    budget: Budget,
    issues: IssueCollector,
    result: list[AffectedUse],
    *,
    role_aware: bool = False,
    fact_selection: FactSelection | None = None,
) -> dict[tuple[str, str], set[str]]:
    """Join actual references and role-specific named dependency paths."""
    by_iri: dict[str, list[Use]] = defaultdict(list)
    for use in uses.entries:
        budget.consume("work")
        by_iri[use.iri].append(use)
    children: dict[tuple[str, str], set[str]] = defaultdict(set)
    for view in views:
        selected = view.all_statements(budget=budget)
        list.extend(issues, selected.report.issues)
        for edge in selected.items:
            budget.consume("work")
            domain = (
                "class"
                if edge.predicate
                in (
                    RDFS.subClassOf,
                    OWL.equivalentClass,
                )
                else "property"
                if edge.predicate
                in (
                    RDFS.subPropertyOf,
                    OWL.equivalentProperty,
                )
                else None
            )
            if domain is None:
                continue
            if isinstance(edge.subject, URIRef) and isinstance(edge.object, URIRef):
                children[(str(edge.object), domain)].add(str(edge.subject))
                if edge.predicate in (OWL.equivalentClass, OWL.equivalentProperty):
                    children[(str(edge.subject), domain)].add(str(edge.object))
    seen: set[tuple[Change, Use]] = set()
    for change in changes.changes:
        if change.iri:
            for use in by_iri.get(change.iri, ()):
                budget.consume("work")
                if role_aware:
                    roles = change_roles(
                        change,
                        change.iri,
                        changes.selected_facts,
                        fact_selection,
                        budget,
                    )
                    answers = tuple(role_matches(use, role) for role in roles)
                    if use.role != "term" and (not roles or None in answers):
                        issues.append(
                            Issue(
                                "upgrade.fact_impact_unverified",
                                "Categorized change lacks complete occurrence "
                                "role coverage for this use",
                                use.location,
                                kind="unverified",
                            )
                        )
                    if use.role != "term" and True not in answers:
                        continue
                budget.consume("states")
                result.append(AffectedUse(change, use, "direct"))
                seen.add((change, use))
        for seed, domain in _dependencies(change):
            if role_aware:
                roles = change_roles(
                    change, seed, changes.selected_facts, fact_selection, budget
                )
                if not any(
                    role == "class"
                    if domain == "class"
                    else role
                    in (
                        "property",
                        "object_property",
                        "datatype_property",
                        "annotation_property",
                    )
                    for role in roles
                ):
                    continue
            queue: deque[tuple[str, tuple[str, ...]]] = deque([(seed, ())])
            visited = {seed}
            while queue:
                iri, path = queue.popleft()
                budget.consume("work")
                for use in by_iri.get(iri, ()):
                    budget.consume("work")
                    compatible = use.role == "term" or (
                        use.role == "predicate"
                        if domain == "property"
                        else use.role == "class"
                        or not role_aware
                        and use.role == "datatype"
                    )
                    if compatible and (change, use) not in seen:
                        budget.consume("states")
                        result.append(
                            AffectedUse(
                                change, use, "hierarchy" if path else "direct", path
                            )
                        )
                        seen.add((change, use))
                for child in sorted(children.get((iri, domain), ())):
                    budget.consume("work")
                    if child in visited:
                        continue
                    if len(path) >= budget.limits.max_depth:
                        raise LimitExceeded("Affected hierarchy exceeded max_depth")
                    budget.consume("states")
                    visited.add(child)
                    queue.append((child, (*path, iri)))
    return children


def _used_deprecations(
    uses: UseIndex,
    left: VocabularyView,
    right: VocabularyView,
    predicates: Sequence[str],
    budget: Budget,
    issues: IssueCollector,
    result: list[DeprecatedUse],
) -> None:
    """Reuse asserted deprecation states without selecting replacement winners."""
    grouped: dict[str, list[Use]] = defaultdict(list)
    for use in uses.entries:
        budget.consume("work")
        grouped[use.iri].append(use)
    for iri in sorted(grouped):
        budget.consume("work")
        old = left.deprecation(iri, replacement_predicates=predicates, budget=budget)
        new = right.deprecation(iri, replacement_predicates=predicates, budget=budget)
        list.extend(issues, (*old.report.issues, *new.report.issues))
        if old.state in ("conflicting", "unverified") or new.state in (
            "conflicting",
            "unverified",
        ):
            issues.append(
                Issue(
                    "upgrade.deprecation_unverified",
                    "Conflicting or unsupported deprecation metadata",
                    iri,
                    kind="unverified",
                )
            )
        elif (
            old.report.complete
            and new.report.complete
            and (old.state != "deprecated" and new.state == "deprecated")
        ):
            for use in grouped[iri]:
                budget.consume("states")
                result.append(DeprecatedUse(use, old, new))
                issues.append(
                    Issue(
                        "upgrade.newly_deprecated",
                        "Used term is newly deprecated",
                        use.location,
                        severity="warning",
                        actual=iri,
                    )
                )
