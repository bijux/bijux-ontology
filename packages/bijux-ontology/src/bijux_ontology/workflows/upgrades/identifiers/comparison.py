"""Actual finite release behavior and separately authorized advisory updates."""

from collections.abc import Sequence

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.vocabulary.accessions.checking import _where, check_accessions
from bijux_ontology.vocabulary.accessions.types import (
    AccessionCheck,
    AccessionObservation,
    AccessionValue,
    _text,
)

from .types import (
    Change,
    IdentifierEquivalence,
    IdentifierImpact,
    IdentifierRelease,
    IdentifierReleaseImpact,
    IdentifierUpdate,
    Transition,
    Verdict,
)


def _joined(left: str, right: str, budget: Budget) -> str:
    """Reserve complete exact identity text before concatenating its parts."""
    size = len(left.encode("utf-8")) + len(right.encode("utf-8"))
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Identifier expansion exceeds max_bytes")
    budget.consume("bytes", size)
    budget.consume("work", len(left) + len(right) + 1)
    return left + right


def _record(observation: AccessionObservation | None, budget: Budget) -> str | None:
    """Recognized namespace/local is representation evidence, not IRI equality."""
    if (
        observation is None
        or observation.namespace is None
        or observation.local is None
    ):
        return None
    return _joined(observation.namespace, observation.local, budget)


def _transition(
    left: AccessionObservation | None, right: AccessionObservation | None
) -> Transition:
    """Classify actual validity only; unknown baseline cannot prove a new success."""
    if (
        left is None
        or right is None
        or left.status not in ("valid", "invalid")
        or right.status not in ("valid", "invalid")
    ):
        return "uncertain"
    if left.status == "valid":
        return "unchanged" if right.status == "valid" else "introduced"
    return "resolved" if right.status == "valid" else "persistent"


def _impact(
    value: AccessionValue,
    left: AccessionObservation | None,
    right: AccessionObservation | None,
    budget: Budget,
) -> IdentifierImpact:
    """Compare independent identity, recognition and spelling dimensions."""
    previous, current = _record(left, budget), _record(right, budget)
    expansion = (
        (previous, current)
        if value.mode == "curie"
        else (
            str(value.value) if previous is not None else None,
            str(value.value) if current is not None else None,
        )
    )
    changes: list[Change] = []
    if previous is not None and current is not None:
        if previous != current:
            changes.append(
                "expansion_changed" if value.mode == "curie" else "recognition_changed"
            )
        elif (
            left is not None
            and right is not None
            and left.canonical_curie is not None
            and right.canonical_curie is not None
            and left.canonical_curie != right.canonical_curie
        ):
            changes.append("canonical_spelling_changed")
    if (
        left is not None
        and right is not None
        and left.ambiguous is False
        and right.ambiguous is True
    ):
        changes.append("recognition_became_ambiguous")
    return IdentifierImpact(
        value, left, right, *expansion, tuple(changes), _transition(left, right)
    )


def _candidate(
    impact: IdentifierImpact,
    pair: IdentifierEquivalence,
    release: IdentifierRelease,
    budget: Budget,
) -> AccessionValue | None:
    """Use existing exact registry operations, requiring a round-trip to caller IRI."""
    target = str(pair.after)
    if impact.original.mode == "curie":
        largest = len(target.encode("utf-8"))
        namespace_size = 0
        count = len(release.registry.canonical_prefixes)
        for namespace, prefix in release.registry.canonical_prefixes:
            bound_text(namespace, budget)
            bound_text(prefix, budget)
            largest = max(
                largest, len(target.encode("utf-8")) + len(prefix.encode("utf-8")) + 1
            )
            namespace_size = max(namespace_size, len(namespace.encode("utf-8")))
        if largest > budget.limits.max_bytes:
            raise LimitExceeded("Proposed identifier exceeds max_bytes")
        budget.consume(
            "work", 4 * (count + 1) * (count.bit_length() + 1) * (largest + 1)
        )
        budget.consume("bytes", 4 * largest)
        # Existing expansion builds its binding map and joined text. Reserve
        # those actual API allocations without implementing a second resolver.
        count_bindings = len(release.registry.bindings)
        budget.consume("states", count_bindings)
        budget.consume("bytes", 64 * count_bindings)
        for prefix, namespace in release.registry.bindings:
            bound_text(prefix, budget)
            bound_text(namespace, budget)
        if namespace_size + largest > budget.limits.max_bytes:
            raise LimitExceeded("Proposed expansion may exceed max_bytes")
        budget.consume("bytes", namespace_size + largest)
        budget.consume("work", namespace_size + largest + 1)
        spelling = release.registry.compact_iri(target)
        try:
            expanded = release.registry.expand_curie(spelling)
        except InputError:
            return None
        if expanded != target:
            return None
    else:
        bound_text(target, budget)
        spelling = pair.after
    return AccessionValue(spelling, impact.original.mode, impact.original.location)


def compare_identifier_releases(
    before: IdentifierRelease,
    after: IdentifierRelease,
    values: Sequence[AccessionValue],
    *,
    equivalences: Sequence[IdentifierEquivalence] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> IdentifierReleaseImpact:
    """Compare only supplied actual occurrences under two explicit policies.

    Each policy is checked once under the same Budget. Raw failures and unknown
    baselines remain intact. Expansion changes, spelling-only changes, validation
    transitions and actual ambiguity are independent evidence. Unused additions
    have no impact unless they alter matching for an actual used value.

    Named equivalence pairs authorize advisory proposals only, with actual after
    checking; no namespace equality, input rewriting or ontology existence follows.
    The finite verdict is not migration acceptance or universal compatibility.
    """
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("Identifier release limits conflict with the shared Budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    left: AccessionCheck | None = None
    right: AccessionCheck | None = None
    uses: list[IdentifierImpact] = []
    updates: list[IdentifierUpdate] = []
    checks: list[Report] = []
    requested = 1
    finished = False
    try:
        for release in (before, after):
            if not isinstance(release, IdentifierRelease):
                raise InputError("Expected two explicit identifier releases")
            _text(release.label, operation)
        captured = capture_items(values, AccessionValue, operation)
        requested = len(captured)
        for value in captured:
            _where(value, operation)
        pairs = capture_items(equivalences, IdentifierEquivalence, operation)
        proposed: dict[str, IdentifierEquivalence] = {}
        for declared in pairs:
            bound_node(declared.before, operation)
            bound_node(declared.after, operation)
            key = str(declared.before)
            if key in proposed:
                raise InputError("Equivalence sources must have one explicit target")
            proposed[key] = declared
        # Both raw result slots and their references are owned before callbacks.
        operation.consume("states", 2)
        operation.consume("bytes", 128)
        left = check_accessions(
            captured, before.registry, rules=before.rules, budget=operation
        )
        retain_report(left.report, operation, issues)
        right = check_accessions(
            captured, after.registry, rules=after.rules, budget=operation
        )
        retain_report(right.report, operation, issues)
        for index, value in enumerate(captured):
            operation.consume("work")
            operation.consume("states")
            operation.consume("bytes", 192)
            previous = (
                left.observations[index] if index < len(left.observations) else None
            )
            current = (
                right.observations[index] if index < len(right.observations) else None
            )
            use = _impact(value, previous, current, operation)
            uses.append(use)
            where = _where(value, operation)
            for change in use.changes:
                issues.append(
                    Issue(
                        "upgrade.identifier_changed",
                        "Actual identifier release behavior changed: " + change,
                        where,
                        severity="info"
                        if change == "canonical_spelling_changed"
                        else "error",
                        actual=value.value,
                        details=value.location,
                    )
                )
            if use.validation == "uncertain":
                issues.append(
                    Issue(
                        "upgrade.identifier_unverified",
                        "Paired identifier validity is not established "
                        "for this occurrence",
                        where,
                        kind="unverified",
                        details=value.location,
                    )
                )
            if (
                previous is None
                or previous.status != "valid"
                or use.before_expansion is None
            ):
                continue
            pair = proposed.get(use.before_expansion)
            if pair is None:
                continue
            operation.consume("states")
            operation.consume("bytes", 128)
            candidate = _candidate(use, pair, after, operation)
            checked = None
            if candidate is not None:
                checked = check_accessions(
                    (candidate,), after.registry, rules=after.rules, budget=operation
                )
            update = IdentifierUpdate(
                value,
                pair,
                candidate,
                checked,
                None
                if checked is None
                or not checked.observations
                or checked.observations[0].status == "unknown"
                else checked.report.verified,
            )
            updates.append(update)
            if checked is not None:
                checks.append(checked.report)
                retain_report(checked.report, operation, issues)
            if candidate is None:
                issues.append(
                    Issue(
                        "upgrade.identifier_unverified",
                        "Caller target has no qualified after-policy "
                        "CURIE representation",
                        where,
                        kind="unverified",
                        details=value.location,
                    )
                )
        finished = len(uses) == requested and requested > 0
    except LimitExceeded:
        issues.exhausted("Stopped at the cumulative identifier release bound")
    except (InputError, IndexError, KeyError):
        try:
            issues.append(
                Issue(
                    "upgrade.identifier_input_error",
                    "Release policy, occurrence or explicit equivalence "
                    "is invalid or conflicting",
                    kind="input_error",
                )
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining identifier release input failure")
    if left is not None:
        issues.retain_completed(left.report)
    if right is not None:
        issues.retain_completed(right.report)
    children = tuple(
        check.report for check in (left, right) if check is not None
    ) + tuple(checks)
    report = Report(
        "upgrade.identifiers",
        len(uses),
        tuple(issues),
        requested=max(requested, len(uses)),
        complete=finished,
        children=children,
        regime="actual-finite-identifier-release-behavior",
    )
    important = {
        "expansion_changed",
        "recognition_changed",
        "recognition_became_ambiguous",
    }
    verdict: Verdict = "agreement"
    if any(
        use.before is not None
        and use.before.status == "valid"
        and (use.validation == "introduced" or important.intersection(use.changes))
        for use in uses
    ):
        verdict = "demonstrated-break"
    elif not report.complete:
        verdict = "unverified"
    elif any(
        use.changes or use.validation in ("persistent", "resolved") for use in uses
    ):
        verdict = "informational"
    return IdentifierReleaseImpact(
        left, right, tuple(uses), tuple(updates), verdict, report
    )
