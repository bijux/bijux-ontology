"""Consumer-scoped upgrade evidence, distinct from global ontology compatibility."""

from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.data.query_usage.types import QueryUsage
from bijux_ontology.declarations.types import ModelDeclaration, TermDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.jsonld.observations import ObservedUsage
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.models.contracts.checking import check_models
from bijux_ontology.models.resource_roles.checking import check_resource_roles
from bijux_ontology.models.resource_roles.types import RoleUse
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.browsing.results import Deprecation
from bijux_ontology.vocabulary.checking import check_vocabulary
from bijux_ontology.vocabulary.comparison import (
    Change,
    VocabularyChanges,
    compare_vocabularies,
    selected_view,
)
from bijux_ontology.vocabulary.facts.types import FactSelection
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary
from bijux_ontology.workflows.upgrades.anonymous import (
    AnonymousAffectedUse,
    anonymous_uses,
)
from bijux_ontology.workflows.upgrades.facts import (
    FactAffectedUse,
    change_roles,
    role_matches,
    selected_fact_uses,
)
from bijux_ontology.workflows.upgrades.qualification import consumer_change_report
from bijux_ontology.workflows.upgrades.uses import Use, UseIndex, index_uses


@dataclass(frozen=True)
class ContractChange:
    """Stable check/location identity with original evidence from both versions."""

    status: Literal["introduced", "resolved", "persistent", "uncertain"]
    contract: str
    before: Issue | None
    after: Issue | None


@dataclass(frozen=True)
class AffectedUse:
    """An actual direct reference or a possible asserted-hierarchy dependency."""

    change: Change
    use: Use
    relation: Literal["direct", "hierarchy"]
    via: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeprecatedUse:
    """Newly deprecated occurrence with untouched replacement hints."""

    use: Use
    before: Deprecation
    after: Deprecation


@dataclass(frozen=True)
class UpgradeAssessment:
    """Finite contract verdict, raw differences and retained per-version checks."""

    verdict: Literal["demonstrated-break", "unverified", "informational", "agreement"]
    changes: VocabularyChanges
    uses: UseIndex
    affected: tuple[AffectedUse, ...]
    contracts: tuple[ContractChange, ...]
    deprecated: tuple[DeprecatedUse, ...]
    before: tuple[Report, ...]
    after: tuple[Report, ...]
    report: Report
    fact_uses: tuple[FactAffectedUse, ...] = field(default=(), kw_only=True)
    anonymous_uses: tuple[AnonymousAffectedUse, ...] = field(default=(), kw_only=True)
    change_qualification: Report | None = field(default=None, kw_only=True)


def _key(issue: Issue) -> tuple[object, ...]:
    """Prose and changed diagnostic values do not invent a newly failing contract."""
    return issue.code, issue.location, issue.kind, issue.severity, issue.details


def _contract_changes(
    name: str,
    before: Report,
    after: Report,
    budget: Budget,
    changes: list[ContractChange],
) -> None:
    """Compare stable finding occurrences without erasing partial results."""
    previous: dict[tuple[object, ...], list[Issue]] = defaultdict(list)
    current: dict[tuple[object, ...], list[Issue]] = defaultdict(list)
    for issue in before.issues:
        budget.consume("work")
        previous[_key(issue)].append(issue)
    for issue in after.issues:
        budget.consume("work")
        current[_key(issue)].append(issue)
    for key in sorted(previous.keys() | current.keys(), key=repr):
        old, new = previous.get(key, []), current.get(key, [])
        for index in range(max(len(old), len(new))):
            budget.consume("work")
            left = old[index] if index < len(old) else None
            right = new[index] if index < len(new) else None
            status: Literal["introduced", "resolved", "persistent", "uncertain"] = (
                "persistent"
                if left is not None and right is not None
                else "introduced"
                if left is None and before.complete and before.checked
                else "resolved"
                if right is None and after.complete and after.checked
                else "uncertain"
            )
            budget.consume("states")
            changes.append(ContractChange(status, name, left, right))


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


def assess_upgrade(
    before: Vocabulary | Graph,
    after: Vocabulary | Graph,
    *,
    terms: Sequence[TermDeclaration] = (),
    models: Sequence[ModelDeclaration] = (),
    observed_usages: Sequence[ObservedUsage] = (),
    role_uses: Sequence[RoleUse] = (),
    query_usages: Sequence[QueryUsage] = (),
    fact_selection: FactSelection | None = None,
    contract_reports: Sequence[tuple[Report, Report]] = (),
    local_namespaces: Sequence[str] = (),
    replacement_predicates: Sequence[str] = (),
    scopes: Sequence[str] = ("local",),
    limits: Limits | None = None,
) -> UpgradeAssessment:
    """Recheck selected consumers and classify only their established evidence.

    Each existing checker receives the explicit limits; the enclosing executable
    supplies a wall-clock deadline for the complete operation. Additional context
    checks may be supplied as before/after pairs from the actual context checker.
    Missing or unsupported prior checks cannot prove a newly introduced failure.
    """
    if any(
        not isinstance(values, Sequence) or isinstance(values, (str, bytes))
        for values in (contract_reports, local_namespaces, replacement_predicates)
    ):
        raise InputError("Upgrade options require explicit sequences")
    selected = limits if limits is not None else Limits()
    budget = Budget(selected)
    issues = IssueCollector(budget)
    uses = index_uses(
        terms,
        models,
        observed_usages=observed_usages,
        role_uses=role_uses,
        query_usages=query_usages,
        budget=budget,
    )
    preparation_failed = False
    try:
        if fact_selection is not None or role_uses:
            before = (
                before
                if isinstance(before, Vocabulary)
                else load_vocabulary(before, budget=budget)
            )
            after = (
                after
                if isinstance(after, Vocabulary)
                else load_vocabulary(after, budget=budget)
            )
        differences = compare_vocabularies(
            before, after, scopes=scopes, fact_selection=fact_selection, budget=budget
        )
        qualified_changes = consumer_change_report(differences, budget)
    except LimitExceeded as error:
        preparation_failed = True
        failed = IssueCollector(budget)
        failed.exhausted(str(error), location="upgrade.sources")
        differences = VocabularyChanges(
            (),
            Report(
                "vocabulary.changes", issues=tuple(failed), requested=1, complete=False
            ),
        )
        qualified_changes = differences.report
    list.extend(issues, (*uses.report.issues, *qualified_changes.issues))
    pairs: list[tuple[str, Report, Report]] = []
    deltas: list[ContractChange] = []
    affected: list[AffectedUse] = []
    deprecated: list[DeprecatedUse] = []
    complete = not preparation_failed
    checked = 0
    fact_uses: list[FactAffectedUse] = []
    anonymous_impacts: list[AnonymousAffectedUse] = []
    views: tuple[VocabularyView, VocabularyView] | None = None
    try:
        references = tuple(
            TermDeclaration(
                use.iri,
                (str(RDF.Property),)
                if use.role == "predicate"
                else (str(RDFS.Class),)
                if use.role == "class"
                else (str(RDFS.Datatype),)
                if use.role == "datatype"
                else (),
                use.location,
            )
            for use in uses.entries
            if use.role in ("class", "predicate", "datatype")
        )
        if role_uses:
            views = (
                selected_view(before, scopes, selected, budget=budget),
                selected_view(after, scopes, selected, budget=budget),
            )
            pairs.append(
                (
                    "roles",
                    check_resource_roles(views[0], role_uses, budget=budget),
                    check_resource_roles(views[1], role_uses, budget=budget),
                )
            )
        for name, declarations in (("terms", tuple(terms)), ("uses", references)):
            if declarations:
                pairs.append(
                    (
                        name,
                        check_vocabulary(
                            before,
                            declarations,
                            local_namespaces=local_namespaces,
                            limits=selected,
                        ),
                        check_vocabulary(
                            after,
                            declarations,
                            local_namespaces=local_namespaces,
                            limits=selected,
                        ),
                    )
                )
        if models:
            pairs.append(
                (
                    "models",
                    check_models(models, before, limits=selected),
                    check_models(models, after, limits=selected),
                )
            )
        for index, pair in enumerate(contract_reports):
            budget.consume("work")
            if (
                not isinstance(pair, Sequence)
                or isinstance(pair, (str, bytes))
                or len(pair) != 2
                or any(not isinstance(report, Report) for report in pair)
            ):
                raise InputError("Expected paired before/after contract reports")
            pairs.append((f"additional[{index}]", *pair))
        for name, old, new in pairs:
            budget.consume("work")
            start = len(deltas)
            _contract_changes(name, old, new, budget, deltas)
            checked += 1
            if (
                not old.complete
                or not new.complete
                or any(delta.status == "uncertain" for delta in deltas[start:])
            ):
                issues.extend(
                    issue
                    for report in (old, new)
                    for issue in report.issues
                    if issue.kind in ("input_error", "exhausted")
                )
                issues.append(
                    Issue(
                        "upgrade.contract_unverified",
                        "One or both contract checks could not be completed",
                        name,
                        kind="unverified",
                    )
                )
        if views is None:
            views = (
                selected_view(
                    before,
                    scopes,
                    selected,
                    budget=budget if fact_selection is not None else None,
                ),
                selected_view(
                    after,
                    scopes,
                    selected,
                    budget=budget if fact_selection is not None else None,
                ),
            )
        dependency_children = _affected_uses(
            differences,
            uses,
            views,
            budget,
            issues,
            affected,
            role_aware=fact_selection is not None or bool(role_uses),
            fact_selection=fact_selection,
        )
        if differences.selected_facts is not None:
            selected_fact_uses(
                differences.selected_facts, uses, budget, issues, fact_uses
            )
        _used_deprecations(
            uses, *views, replacement_predicates, budget, issues, deprecated
        )
        if differences.anonymous is not None:
            anonymous_uses(
                differences.anonymous,
                uses,
                budget,
                issues,
                anonymous_impacts,
                dependency_children=dependency_children,
            )
        elif any(change.kind == "anonymous" for change in differences.changes):
            issues.append(
                Issue(
                    "upgrade.anonymous_impact_unverified",
                    "Anonymous structure changed; its application impact is unverified",
                    kind="unverified",
                )
            )
        if not any(old.checked and new.checked for _, old, new in pairs):
            issues.append(
                Issue(
                    "upgrade.no_contracts",
                    "No application contracts were supplied",
                    kind="unverified",
                )
            )
        for delta in deltas:
            if delta.status == "persistent" and delta.after is not None:
                issue = delta.after
                issues.append(
                    Issue(
                        "upgrade.contract_persistent",
                        "Pre-existing contract finding remains",
                        issue.location,
                        kind=issue.kind,
                        severity=issue.severity,
                        expected=issue.expected,
                        actual=issue.actual,
                        details=issue.details,
                    )
                )
            elif delta.status == "resolved" and delta.before is not None:
                issues.append(
                    Issue(
                        "upgrade.contract_resolved",
                        "Previous finding is absent after complete recheck",
                        delta.before.location,
                        severity="info",
                    )
                )
            elif delta.status == "introduced" and delta.after is not None:
                issue = delta.after
                issues.append(
                    Issue(
                        "upgrade.contract_introduced",
                        "Contract now produces a finding",
                        issue.location,
                        kind=issue.kind,
                        severity=issue.severity,
                        expected=issue.expected,
                        actual=issue.actual,
                        details=issue.details,
                    )
                )
    except LimitExceeded as error:
        issues.exhausted(str(error), location="upgrade")
        complete = False
    breaking = any(
        delta.status == "introduced"
        and delta.after is not None
        and delta.after.kind == "finding"
        and delta.after.severity == "error"
        for delta in deltas
    )
    unverified = (
        not complete or not uses.report.complete or not qualified_changes.complete
    )
    unverified |= any(issue.kind != "finding" for issue in issues)
    verdict: Literal[
        "demonstrated-break", "unverified", "informational", "agreement"
    ] = (
        "demonstrated-break"
        if breaking
        else "unverified"
        if unverified
        else "informational"
        if differences.changes
        or deltas
        or deprecated
        or differences.selected_facts is not None
        and differences.selected_facts.differences
        else "agreement"
    )
    return UpgradeAssessment(
        verdict,
        differences,
        uses,
        tuple(affected),
        tuple(deltas),
        tuple(deprecated),
        tuple(old for _, old, _ in pairs),
        tuple(new for _, _, new in pairs),
        Report(
            "upgrade",
            checked,
            tuple(issues),
            allow_empty=True,
            requested=checked if complete else checked + 1,
            complete=complete,
            families=("application.upgrade",),
            regime="finite-consumer-contracts",
            children=(
                uses.report,
                qualified_changes,
                *(report for _, old, new in pairs for report in (old, new)),
            ),
        ),
        fact_uses=tuple(fact_uses),
        anonymous_uses=tuple(anonymous_impacts),
        change_qualification=qualified_changes,
    )
