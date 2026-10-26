"""Compose explicit consumer checks and retain established upgrade evidence."""

from collections.abc import Sequence
from typing import Literal

from rdflib import Graph
from rdflib.namespace import RDF, RDFS

from bijux_ontology.data.query_usage.types import QueryUsage
from bijux_ontology.declarations.types import ModelDeclaration, TermDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import operation
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.jsonld.evidence.occurrences import ObservedUsage
from bijux_ontology.models.contracts.checking import check_models
from bijux_ontology.models.resource_roles.checking import check_resource_roles
from bijux_ontology.models.resource_roles.types import RoleUse
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.checking import check_vocabulary
from bijux_ontology.vocabulary.comparison import (
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
    selected_fact_uses,
)
from bijux_ontology.workflows.upgrades.qualification import consumer_change_report
from bijux_ontology.workflows.upgrades.uses import index_uses

from .contracts import _contract_changes
from .impacts import _affected_uses, _used_deprecations
from .types import AffectedUse, ContractChange, DeprecatedUse, UpgradeAssessment


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
    budget: Budget | None = None,
) -> UpgradeAssessment:
    """Recheck selected consumers and classify only their established evidence.

    By default existing checkers retain their independent limits. An explicit
    shared Budget instead covers preparation, both versions and comparison; an
    enclosing Operation also supplies its original deadline and cancellation.
    The enclosing executable bounds ordinary standalone calls. Additional context
    checks may be supplied as before/after pairs from the actual context checker.
    Missing or unsupported prior checks cannot prove a newly introduced failure.
    """
    if any(
        not isinstance(values, Sequence) or isinstance(values, (str, bytes))
        for values in (contract_reports, local_namespaces, replacement_predicates)
    ):
        raise InputError("Upgrade options require explicit sequences")
    shared_budget = budget
    budget = operation(limits, budget)
    selected = budget.limits
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
    pending_before: Report | None = None
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
            pending_before = check_resource_roles(views[0], role_uses, budget=budget)
            current = check_resource_roles(views[1], role_uses, budget=budget)
            pairs.append(("roles", pending_before, current))
            pending_before = None
        for name, declarations in (("terms", tuple(terms)), ("uses", references)):
            if declarations:
                pending_before = check_vocabulary(
                    before,
                    declarations,
                    local_namespaces=local_namespaces,
                    limits=selected,
                    budget=shared_budget,
                )
                current = check_vocabulary(
                    after,
                    declarations,
                    local_namespaces=local_namespaces,
                    limits=selected,
                    budget=shared_budget,
                )
                pairs.append((name, pending_before, current))
                pending_before = None
        if models:
            pending_before = check_models(
                models, before, limits=selected, budget=shared_budget
            )
            current = check_models(models, after, limits=selected, budget=shared_budget)
            pairs.append(("models", pending_before, current))
            pending_before = None
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
                    budget=budget
                    if fact_selection is not None or shared_budget is not None
                    else None,
                ),
                selected_view(
                    after,
                    scopes,
                    selected,
                    budget=budget
                    if fact_selection is not None or shared_budget is not None
                    else None,
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
        tuple(old for _, old, _ in pairs)
        + ((pending_before,) if pending_before is not None else ()),
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
                *((pending_before,) if pending_before is not None else ()),
            ),
        ),
        fact_uses=tuple(fact_uses),
        anonymous_uses=tuple(anonymous_impacts),
        change_qualification=qualified_changes,
    )
