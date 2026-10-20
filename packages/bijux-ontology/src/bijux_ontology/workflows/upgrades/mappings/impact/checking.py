"""Consumer mapping impact reuses actual SSSOM and application-policy evidence."""

import threading
from collections.abc import Sequence
from typing import Literal

from bijux_ontology._json import decode_json, object_value
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture
from bijux_ontology.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.inspection import VocabularyView
from bijux_ontology.vocabulary.mappings.checking import check_mappings
from bijux_ontology.vocabulary.mappings.composition.checking import _input
from bijux_ontology.vocabulary.mappings.composition.types import MappingOccurrence
from bijux_ontology.vocabulary.mappings.types import (
    MappingPolicy,
    MappingRead,
    MappingSet,
)

from .comparison import (
    differences,
    index,
    interpretation_fields,
    interpretation_required,
    names,
)
from .comparison import qualifications as capture_qualifications
from .types import (
    MappingChange,
    MappingCoverage,
    MappingImpact,
    MappingObligation,
    MappingQualifications,
    MappingUse,
)

Requirements = dict[tuple[str, str], tuple[int, int, Issue]]
Conflicts = dict[tuple[str, str], dict[tuple[str, ...], Issue]]


def _evidence(issue: Issue, budget: Budget) -> dict[str, object]:
    """Adopt native structured requirement fields before decoding their JSON."""
    if issue.evidence_json is None:
        raise InputError("Native mapping requirement evidence is unavailable")
    bound_text(issue.evidence_json, budget)
    size = len(issue.evidence_json)
    budget.consume("states", size)
    budget.consume("work", size)
    budget.consume("bytes", 4 * size)
    return object_value(
        decode_json(issue.evidence_json), location="native mapping requirement evidence"
    )


def _requirements(report: Report, budget: Budget) -> Requirements:
    """Read native structured cardinality evidence without parsing diagnostic prose."""
    result: Requirements = {}
    for issue in report:
        budget.consume("work")
        if issue.code not in {"mapping.maximum_exceeded", "mapping.minimum_missing"}:
            continue
        data = _evidence(issue, budget)
        requirement, endpoint = data.get("requirement"), data.get("endpoint")
        limit, observed = data.get("limit"), data.get("observed")
        if (
            requirement not in {"minimum_targets", "maximum_targets", "maximum_sources"}
            or not isinstance(endpoint, str)
            or type(limit) is not int
            or type(observed) is not int
            or limit < 0
            or observed < 0
        ):
            raise InputError("Native mapping cardinality evidence is malformed")
        key = str(requirement), endpoint
        previous = result.get(key)
        if previous is None or (
            observed < previous[1]
            if requirement == "minimum_targets"
            else observed > previous[1]
        ):
            budget.consume("states")
            result[key] = limit, observed, issue
    return result


def _conflicts(report: Report, budget: Budget) -> Conflicts:
    """Retain actual incompatible relation pairs at exact authored endpoints."""
    result: Conflicts = {}
    for issue in report:
        budget.consume("work")
        if issue.code != "mapping.relation_conflict":
            continue
        data = _evidence(issue, budget)
        subject, target = data.get("subject"), data.get("object")
        relations = data.get("relations")
        if (
            data.get("requirement") != "relation_conflict"
            or not isinstance(subject, str)
            or not isinstance(target, str)
            or not isinstance(relations, list)
            or len(relations) != 2
            or any(not isinstance(value, str) for value in relations)
        ):
            raise InputError("Native mapping conflict evidence is malformed")
        budget.consume("states", 3)
        result.setdefault((subject, target), {})[tuple(sorted(relations))] = issue
    return result


def _introduced_conflicts(
    use: MappingUse,
    before: Conflicts,
    after: Conflicts,
    budget: Budget,
) -> tuple[Issue, ...]:
    """Require the used directional pair and predicate in a newly failing policy."""
    row = use.occurrence.row
    result = []
    pair = row.subject, row.object
    previous = before.get(pair, {})
    for relations, issue in after.get(pair, {}).items():
        budget.consume("work")
        if relations not in previous and row.predicate in relations:
            budget.consume("states")
            result.append(issue)
    return tuple(result)


def _uses(
    values: Sequence[MappingUse],
    before: MappingSet,
    coverage: MappingCoverage,
    budget: Budget,
) -> tuple[MappingUse, ...]:
    """Capture actual original row witnesses; equal-looking foreign rows do not bind."""
    result = []
    budget.consume("states", len(coverage.records))
    records = set(coverage.records)
    for value in capture(values, budget):
        if not isinstance(value, MappingUse):
            raise InputError("Mapping consumers require typed directional uses")
        for text in (value.record, value.location):
            if type(text) is not str or not text:
                raise InputError("Mapping consumers need actual record/location names")
            bound_text(text, budget)
        occurrence = value.occurrence
        if (
            value.record not in records
            or value.direction not in ("forward", "reverse")
            or not isinstance(occurrence, MappingOccurrence)
            or occurrence.mapping_set is not before
            or type(occurrence.index) is not int
            or not 0 <= occurrence.index < len(before.rows)
        ):
            raise InputError(
                "Mapping use is outside its exact supplied source/record scope"
            )
        budget.consume("states")
        result.append(
            MappingUse(value.record, value.location, occurrence, value.direction)
        )
    return tuple(result)


def _introduced(
    use: MappingUse,
    before: Requirements,
    after: Requirements,
    budget: Budget,
) -> tuple[Issue, ...]:
    """Attribute only new or worsened native constraints at the used row's endpoints."""
    row = use.occurrence.row
    result = []
    for requirement, endpoint in (
        ("minimum_targets", row.subject),
        ("maximum_targets", row.subject),
        ("maximum_sources", row.object),
    ):
        budget.consume("work")
        current = after.get((requirement, endpoint))
        if current is None:
            continue
        previous = before.get((requirement, endpoint))
        if previous is not None and (
            current[1] >= previous[1]
            if requirement == "minimum_targets"
            else current[1] <= previous[1]
        ):
            continue
        budget.consume("states")
        result.append(current[2])
    return tuple(result)


def assess_mapping_impact(
    before: MappingSet | MappingRead,
    after: MappingSet | MappingRead,
    *,
    uses: Sequence[MappingUse],
    coverage: MappingCoverage,
    policy: MappingPolicy,
    qualifications: MappingQualifications,
    subject_vocabulary: VocabularyView,
    object_vocabulary: VocabularyView,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: threading.Event | None = None,
) -> MappingImpact:
    """Assess only supplied directional mapping obligations and their exact scope.

    Rows are compared by expanded endpoints, exact predicates, declared roles and
    selected qualifications. No inverse, symmetric, transitive or equivalence
    substitution repairs a removed authored obligation. Original duplicate row
    occurrences remain evidence while native cardinalities count distinct IRIs.
    Unselected metadata changes and unused edits do not demonstrate a data break.
    Both actual application checks are retained; persistent preexisting policy
    failures alone do not establish a newly introduced consumer failure.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children: list[Report] = []
    left = right = None
    owned_coverage = None
    selected = None
    before_check = after_check = None
    changes: tuple[MappingChange, ...] = ()
    obligations: list[MappingObligation] = []
    checked = 0
    requested = 1
    verdict: Literal[
        "demonstrated-break", "unverified", "informational", "agreement"
    ] = "unverified"
    complete = False
    try:
        if not isinstance(policy, MappingPolicy) or not isinstance(
            coverage, MappingCoverage
        ):
            raise InputError(
                "Mapping impact needs typed policy and finite consumer coverage"
            )
        if type(coverage.complete) is not bool:
            raise InputError(
                "Mapping coverage requires an explicit boolean completeness claim"
            )
        selected = capture_qualifications(qualifications, operation)
        owned_coverage = MappingCoverage(
            names(coverage.records, operation), coverage.complete
        )
        left, _ = _input(before, operation, issues, children)
        right, _ = _input(after, operation, issues, children)
        if left is not None and right is not None:
            selected_uses = _uses(uses, left, owned_coverage, operation)
            requested = max(1, len(selected_uses))
            operation.consume(
                "states", len(selected.cells) + len(left.columns) + len(right.columns)
            )
            if set(selected.cells) - set(left.columns) - set(right.columns):
                raise InputError(
                    "Selected row qualifications are absent from both releases"
                )
            previous, keys, previous_metadata = index(left, selected, operation)
            current, _, current_metadata = index(right, selected, operation)
            context_fields = interpretation_fields(left, right, selected, operation)
            contexts_known = True
            changes = differences(previous, current, operation)
            for value in (left, right):
                result = check_mappings(
                    value,
                    subject_vocabulary=subject_vocabulary,
                    object_vocabulary=object_vocabulary,
                    policy=policy,
                    budget=operation,
                ).report
                retain_report(result, operation, issues)
                children.append(result)
                if before_check is None:
                    before_check = result
                else:
                    after_check = result
            assert before_check is not None and after_check is not None
            old_requirements = _requirements(before_check, operation)
            new_requirements = _requirements(after_check, operation)
            old_conflicts = _conflicts(before_check, operation)
            new_conflicts = _conflicts(after_check, operation)
            known = before_check.complete and after_check.complete
            for use in selected_uses:
                operation.consume("work")
                surviving = current.get(keys[use.occurrence.index], ())
                failures = []
                if not surviving:
                    finding = Issue(
                        "upgrade.mapping_obligation_changed",
                        "The supplied directional mapping obligation "
                        "no longer has an exact supported row",
                        use.location,
                        actual=use.record,
                    )
                    issues.append(finding)
                    failures.append(finding)
                introduced = (
                    _introduced(use, old_requirements, new_requirements, operation)
                    if before_check.complete
                    else ()
                )
                for native in introduced:
                    finding = Issue(
                        "upgrade.mapping_cardinality_changed",
                        "A new or worsened mapping cardinality "
                        "requirement affects the supplied consumer",
                        use.location,
                        actual=use.record,
                        evidence_json=native.evidence_json,
                    )
                    issues.append(finding)
                    failures.append(finding)
                introduced_conflicts = (
                    _introduced_conflicts(use, old_conflicts, new_conflicts, operation)
                    if before_check.complete
                    else ()
                )
                for native in introduced_conflicts:
                    finding = Issue(
                        "upgrade.mapping_relation_changed",
                        "A newly incompatible relation pair affects "
                        "the supplied directional mapping obligation",
                        use.location,
                        actual=use.record,
                        evidence_json=native.evidence_json,
                    )
                    issues.append(finding)
                    failures.append(finding)
                context_required = interpretation_required(
                    use.occurrence.row,
                    selected,
                    context_fields,
                    previous_metadata,
                    current_metadata,
                    operation,
                )
                if context_required:
                    contexts_known = False
                    issues.append(
                        Issue(
                            "upgrade.mapping_impact_unverified",
                            "Selected identifier qualifications have changed "
                            "prefix context without a qualified resolved comparison",
                            use.location,
                            kind="unverified",
                            actual=use.record,
                        )
                    )
                operation.consume("states", len(surviving) + len(failures) + 1)
                status: Literal["preserved", "broken", "unverified"] = (
                    "broken"
                    if failures
                    else "preserved"
                    if known and not context_required
                    else "unverified"
                )
                obligations.append(
                    MappingObligation(use, status, surviving, tuple(failures))
                )
                checked += 1
            operation.consume(
                "states", len(selected_uses) + len(owned_coverage.records)
            )
            seen_records = {use.record for use in selected_uses}
            scoped_complete = (
                owned_coverage.complete
                and bool(selected_uses)
                and seen_records == set(owned_coverage.records)
            )
            if not scoped_complete:
                issues.append(
                    Issue(
                        "upgrade.mapping_impact_unverified",
                        "Consumer mapping uses do not establish complete "
                        "coverage of the declared finite record inventory",
                        kind="unverified",
                    )
                )
            complete = known and scoped_complete and contexts_known
            verdict = (
                "demonstrated-break"
                if any(item.status == "broken" for item in obligations)
                else "unverified"
                if not complete
                else "informational"
                if changes or any(issue.kind == "finding" for issue in issues)
                else "agreement"
            )
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(
                Issue(
                    "upgrade.mapping_impact_input_error", str(error), kind="input_error"
                )
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining mapping impact input failure")
    report = Report(
        "upgrade.mapping_impact",
        checked,
        tuple(issues),
        requested=requested,
        complete=complete,
        children=tuple(children),
        regime="explicit-consumer-mapping-obligations",
    )
    if any(
        issue.code
        in {
            "upgrade.mapping_obligation_changed",
            "upgrade.mapping_cardinality_changed",
            "upgrade.mapping_relation_changed",
        }
        for issue in issues
    ):
        verdict = "demonstrated-break"
    elif not report.complete:
        verdict = "unverified"
    return MappingImpact(
        verdict,
        left,
        right,
        owned_coverage,
        selected,
        changes,
        tuple(obligations),
        before_check,
        after_check,
        report,
    )
