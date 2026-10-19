"""Actual one-term extension and protected-redefinition scenarios."""

from dataclasses import dataclass

from bijux_ontology.jsonld.checking import check_context
from bijux_ontology.jsonld.composition.checking import _Case, _document
from bijux_ontology.jsonld.contexts.processing import _data, inspect_context
from bijux_ontology.jsonld.contexts.types import ContextInspection, TermDefinition
from bijux_ontology.jsonld.resources import ContextResources
from bijux_ontology.limits import Budget, IssueCollector, retain_report
from bijux_ontology.reports import Issue, Report


@dataclass
class _Probe:
    """Prepaid output slots keep actual raw evidence through later exhaustion."""

    name: str
    term: str
    definition: object
    expected: str
    extension_cases: tuple[_Case, ...]
    cases: list[Report | None]
    replacement: ContextInspection | None = None
    inspection: ContextInspection | None = None
    report: Report | None = None


def _term(
    inspection: ContextInspection, name: str, budget: Budget
) -> TermDefinition | None:
    """Look up a selected definition with each comparison charged."""
    if inspection.view is not None:
        for term in inspection.view.terms:
            budget.consume("work", len(term.term) + len(name) + 1)
            if term.term == name:
                return term
    return None


def _append(
    context: object, replacement: dict[str, object], budget: Budget
) -> list[object]:
    """Append one ordinary context entry without merging any term dictionaries."""
    count = len(context) if isinstance(context, list) else 1
    budget.consume("states", count + 1)
    budget.consume("bytes", 64 + 8 * (count + 1))
    budget.consume("work", count + 1)
    return (
        [*context, replacement] if isinstance(context, list) else [context, replacement]
    )


def run_probe(
    probe: _Probe,
    context: object,
    original: TermDefinition,
    retained_cases: tuple[_Case, ...],
    resources: ContextResources,
    base: str | None,
    budget: Budget,
) -> Report:
    """Keep raw native errors distinct from explicit expected-guard policy truth."""
    issues = IssueCollector(budget)
    raw = IssueCollector(budget)
    children: list[Report] = []
    replacement = {probe.term: probe.definition}
    probe.replacement = inspect_context(
        replacement, base=base, document_loader=resources, budget=budget
    )
    retain_report(probe.replacement.report, budget, raw)
    children.append(probe.replacement.report)
    candidate = _term(probe.replacement, probe.term, budget)
    if not probe.replacement.report.verified or candidate is None:
        issues.append(
            Issue(
                "context.publication_unverified",
                "The replacement has no independently qualified definition",
                kind="unverified",
            )
        )
        return Report(
            "jsonld.publication.override",
            0,
            tuple(issues),
            requested=1,
            complete=False,
            children=tuple(children),
        )
    if candidate.scoped_context_json is not None:
        issues.append(
            Issue(
                "context.publication_unverified",
                "Scoped replacement coverage is outside the root-term profile",
                kind="unverified",
            )
        )
        return Report(
            "jsonld.publication.override",
            0,
            tuple(issues),
            requested=1,
            complete=False,
            children=tuple(children),
        )
    probe.inspection = inspect_context(
        replacement,
        inherited_context=context,
        base=base,
        document_loader=resources,
        budget=budget,
    )
    retain_report(probe.inspection.report, budget, raw)
    if probe.expected == "blocked":
        precise_guard = probe.inspection.report.outcome == "input_error" and any(
            issue.kind == "input_error"
            and issue.actual == "protected term redefinition"
            for issue in probe.inspection.report.issues
        )
        if not precise_guard:
            children.append(probe.inspection.report)
            issues.append(
                Issue(
                    "context.publication_mismatch",
                    "The requested protected override was not precisely blocked",
                    kind="finding"
                    if probe.inspection.report.verified
                    else "unverified",
                )
            )
        return Report(
            "jsonld.publication.override",
            1,
            tuple(issues),
            requested=1,
            complete=precise_guard or probe.inspection.report.verified,
            regime="expected-protected-redefinition",
            children=tuple(children),
        )
    children.append(probe.inspection.report)
    if not probe.inspection.report.verified:
        issues.append(
            Issue(
                "context.publication_mismatch",
                "The declared extension override was not permitted",
                kind="unverified",
            )
        )
        return Report(
            "jsonld.publication.override",
            1,
            tuple(issues),
            requested=1,
            complete=False,
            children=tuple(children),
        )
    actual = _term(probe.inspection, probe.term, budget)
    _data(actual, budget)
    _data(original, budget)
    if actual == original:
        issues.append(
            Issue(
                "context.publication_unverified",
                "The extension probe did not change its effective definition",
                kind="unverified",
            )
        )
        return Report(
            "jsonld.publication.override",
            0,
            tuple(issues),
            requested=1,
            complete=False,
            children=tuple(children),
        )
    composite = _append(context, replacement, budget)
    for index, case in enumerate((*retained_cases, *probe.extension_cases)):
        result = check_context(
            _document(case, composite, budget),
            case.expectations,
            base=base,
            document_loader=resources,
            budget=budget,
        )
        probe.cases[index] = result
        children.append(result)
        retain_report(result, budget, issues)
    return Report(
        "jsonld.publication.override",
        len(probe.cases),
        tuple(issues),
        requested=len(probe.cases),
        complete=all(r.complete for r in children),
        children=tuple(children),
        regime="permitted-root-extension",
    )
