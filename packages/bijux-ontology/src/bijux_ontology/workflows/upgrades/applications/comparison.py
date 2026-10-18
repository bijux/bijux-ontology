"""Application upgrade composition and role-qualified ontology impacts."""

from collections.abc import Sequence
from dataclasses import replace

from rdflib import Graph

from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.roles.classification import bound_text
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.comparison import (
    VocabularyChanges,
    compare_vocabularies,
    selected_view,
)
from bijux_ontology.vocabulary.facts.types import FactSelection
from bijux_ontology.vocabulary.loading import load_vocabulary
from bijux_ontology.vocabulary.view import Vocabulary
from bijux_ontology.workflows.upgrades.anonymous import (
    AnonymousAffectedUse,
    anonymous_uses,
)
from bijux_ontology.workflows.upgrades.assessment import (
    AffectedUse,
    ContractChange,
    _affected_uses,
)
from bijux_ontology.workflows.upgrades.facts import FactAffectedUse, selected_fact_uses
from bijux_ontology.workflows.upgrades.qualification import consumer_change_report
from bijux_ontology.workflows.upgrades.uses import UseIndex

from .deltas import _contracts, _declarations, _sample
from .preparation import _check_version, _preflight, _stopped, _VersionCheck
from .types import (
    ApplicationDeclarationChange,
    ApplicationSampleComparison,
    ApplicationUpgrade,
    ApplicationVersion,
    _sequence,
)


def compare_applications(
    before_vocabulary: Vocabulary | Graph,
    after_vocabulary: Vocabulary | Graph,
    *,
    before: ApplicationVersion | Absent = ABSENT,
    after: ApplicationVersion,
    fact_selection: FactSelection | None = None,
    scopes: Sequence[str] = ("local",),
    limits: Limits | None = None,
) -> ApplicationUpgrade:
    """Compare finite independent releases without assuming unchanged consumers.

    Stable map keys and sample identifiers establish correspondence. Original
    reports and source witnesses stay intact. Changed declarations, expanded
    representations, selected mapping topology and exact RDF meaning are separate
    evidence; caller acceptance policy is outside this operation.
    """
    if (
        not isinstance(after, ApplicationVersion)
        or before is not ABSENT
        and not isinstance(before, ApplicationVersion)
    ):
        raise InputError("Expected explicit application versions or an absent baseline")
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("Expected explicit application Limits")
    if not isinstance(scopes, Sequence) or isinstance(scopes, str | bytes):
        raise InputError("Application scopes require a finite sequence")
    budget = Budget(limits)
    issues = IssueCollector(budget)
    left_version = None if before is ABSENT else before
    empty_uses = UseIndex((), Report("application.uses", allow_empty=True))
    pending = Report(
        "application.pending",
        issues=(
            Issue(
                "upgrade.application_unverified",
                "Required application comparison has not completed",
                kind="unverified",
            ),
        ),
        requested=1,
        complete=False,
    )
    left = _VersionCheck(left_version, pending, {}, {}, {}, empty_uses)
    right = _VersionCheck(after, pending, {}, {}, {}, empty_uses)
    changes: VocabularyChanges | None = None
    declarations: list[ApplicationDeclarationChange] = []
    samples: list[ApplicationSampleComparison] = []
    contracts: list[ContractChange] = []
    affected: tuple[list[AffectedUse], list[AffectedUse]] = ([], [])
    fact_uses: tuple[list[FactAffectedUse], list[FactAffectedUse]] = ([], [])
    anonymous_impacts: tuple[list[AnonymousAffectedUse], list[AnonymousAffectedUse]] = (
        [],
        [],
    )
    qualification: Report | None = None
    reports: list[Report] = []
    finished = True
    try:
        selected_scopes = _sequence(scopes, str, budget)
        for scope in selected_scopes:
            if not isinstance(scope, str):
                raise InputError("Application scopes require text names")
            bound_text(scope, budget)
        if left_version is not None:
            _preflight(left_version, budget)
        _preflight(after, budget)
        a = (
            before_vocabulary
            if isinstance(before_vocabulary, Vocabulary)
            else load_vocabulary(before_vocabulary, budget=budget)
        )
        b = (
            after_vocabulary
            if isinstance(after_vocabulary, Vocabulary)
            else load_vocabulary(after_vocabulary, budget=budget)
        )
        left = _check_version(left_version, a, "application.before", budget)
        reports.append(left.report)
        _stopped(left.report)
        right = _check_version(after, b, "application.after", budget)
        reports.append(right.report)
        _stopped(right.report)
        _declarations(left_version, after, budget, issues, declarations)
        for identifier in sorted(left.samples.keys() | right.samples.keys()):
            item = _sample(identifier, left, right, budget, issues)
            samples.append(item)
            reports.extend((item.mapping, item.dataset))
            _stopped(item.mapping)
            _stopped(item.dataset)
        names = left.parts.keys() | right.parts.keys()
        for name in sorted(names):
            old = left.parts.get(name)
            new = right.parts.get(name)
            if old is None or new is None:
                issues.append(
                    Issue(
                        "upgrade.application_unverified",
                        "No paired raw check exists for this stable consumer",
                        name,
                        kind="unverified",
                    )
                )
                continue
            start = len(contracts)
            _contracts(
                name,
                old,
                new,
                left_version,
                after,
                name[7:] if name.startswith("sample/") else None,
                budget,
                contracts,
            )
            if name.startswith("sample/"):
                comparison = next(
                    (item for item in samples if item.identifier == name[7:]), None
                )
                if (
                    comparison is None
                    or not comparison.mapping.complete
                    or any(
                        issue.kind != "finding" for issue in comparison.mapping.issues
                    )
                ):
                    contracts[start:] = [
                        replace(change, status="uncertain")
                        for change in contracts[start:]
                    ]
        if not samples:
            issues.append(
                Issue(
                    "upgrade.application_unverified",
                    "No representative actual output comparison was supplied",
                    kind="unverified",
                )
            )
        if not left_version or not after.fields:
            issues.append(
                Issue(
                    "upgrade.application_unverified",
                    "Explicit matched field coverage is required",
                    kind="unverified",
                )
            )
        for consumer in after.fields:
            budget.consume("work")
            exercised = any(
                key == consumer
                and previous is not None
                and current is not None
                and all(
                    u.occurrences and not (u.limitation or u.dropped or u.empty)
                    for u in (previous, current)
                )
                for item in samples
                for key, previous, current in item.observations
            )
            if not exercised:
                issues.append(
                    Issue(
                        "upgrade.application_unverified",
                        "Required field lacks an exercised matched sample",
                        consumer,
                        kind="unverified",
                    )
                )
        changes = compare_vocabularies(
            a, b, scopes=selected_scopes, fact_selection=fact_selection, budget=budget
        )
        qualification = consumer_change_report(changes, budget)
        reports.append(qualification)
        views = (
            selected_view(a, selected_scopes, budget.limits, budget=budget),
            selected_view(b, selected_scopes, budget.limits, budget=budget),
        )
        for index, version in enumerate((left, right)):
            dependency_children = _affected_uses(
                changes,
                version.uses,
                views,
                budget,
                issues,
                affected[index],
                role_aware=True,
                fact_selection=fact_selection,
            )
            if changes.anonymous is not None:
                anonymous_uses(
                    changes.anonymous,
                    version.uses,
                    budget,
                    issues,
                    anonymous_impacts[index],
                    dependency_children=dependency_children,
                )
            if changes.selected_facts is not None:
                selected_fact_uses(
                    changes.selected_facts,
                    version.uses,
                    budget,
                    issues,
                    fact_uses[index],
                )
        if any(c.status == "uncertain" for c in contracts):
            issues.append(
                Issue(
                    "upgrade.application_unverified",
                    "Some failures lack established stable before/after attribution",
                    kind="unverified",
                )
            )
    except LimitExceeded as error:
        finished = False
        issues.exhausted(str(error), location="application-upgrade")
    report = Report(
        "application-upgrade",
        len(samples),
        tuple(issues),
        requested=max(1, len(left.samples.keys() | right.samples.keys())),
        complete=finished,
        children=tuple(reports),
        families=("application-upgrade",),
    )
    return ApplicationUpgrade(
        left.report,
        right.report,
        tuple(declarations),
        tuple(samples),
        tuple(contracts),
        changes,
        left.uses,
        right.uses,
        tuple(affected[0]),
        tuple(affected[1]),
        tuple(fact_uses[0]),
        tuple(fact_uses[1]),
        report,
        before_anonymous_uses=tuple(anonymous_impacts[0]),
        after_anonymous_uses=tuple(anonymous_impacts[1]),
        change_qualification=qualification,
    )
