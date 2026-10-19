"""Apply explicit application rules to actual selected JSON-LD key roles."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from bijux_ontology._jsonpaths import SourcePath, at_path, bound_path, bounded_location
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    bound_text,
    retain_report,
)
from bijux_ontology.jsonld.checking import PreparedUsages
from bijux_ontology.jsonld.contexts.usages import (
    SourceTerm,
    UsageContextUnavailable,
    source_terms,
)
from bijux_ontology.jsonld.resources import ContextInputError
from bijux_ontology.jsonld.selectors.checking import resolve_selection
from bijux_ontology.rdf.identifiers import absolute_iri

from .types import (
    TermUsage,
    TermUsageRegion,
    TermUsageResult,
    TermUsageVocabulary,
    UsageRole,
)


@dataclass(frozen=True)
class _Region:
    """One owned declaration and its precharged exact ignored-term membership."""

    declaration: TermUsageRegion
    ignored: frozenset[str]
    comparison_work: int


def _capture(regions: Sequence[TermUsageRegion], budget: Budget) -> tuple[_Region, ...]:
    """Capture finite declarations once, rejecting changing or untyped inventories."""
    if not isinstance(regions, Sequence) or isinstance(
        cast(object, regions), str | bytes
    ):
        raise InputError("Term usage regions require a finite typed sequence")
    count = len(regions)
    budget.consume("states", count)
    budget.consume("bytes", 64 + 16 * count)
    result = []
    for index in range(count):
        budget.consume("work")
        try:
            region = regions[index]
        except IndexError as error:
            raise InputError("Term usage inventory changed during capture") from error
        if not isinstance(region, TermUsageRegion):
            raise InputError("Expected typed TermUsageRegion declarations")
        budget.consume("states", len(region.ignored_terms))
        budget.consume("bytes", 64 + 64 * len(region.ignored_terms))
        comparison_work = 1
        for term in region.ignored_terms:
            bound_text(term, budget)
            budget.consume("work", len(term) + 1)
            comparison_work += len(term) + 1
        result.append(_Region(region, frozenset(region.ignored_terms), comparison_work))
    return tuple(result)


def _role(term: SourceTerm, budget: Budget) -> UsageRole:
    """Classify maintained mappings; implicit vocabulary expansion remains legal."""
    if term.iri is not None and term.iri.startswith("@"):
        return "keyword"
    if term.iri is not None:
        budget.consume("work", len(term.iri) + 1)
        try:
            absolute_iri(term.iri)
        except InputError:
            return "unresolved"
    if term.explicit:
        return "nullified" if term.iri is None else "explicit"
    if term.compact:
        return "compact-iri"
    if ":" in term.term and term.iri is not None:
        return "full-iri"
    if term.iri is not None and term.vocabulary is not None:
        return "implicit-vocabulary"
    return "unmapped"


def _opaque(prepared: PreparedUsages, path: SourcePath, budget: Budget) -> bool:
    """Use existing original usage evidence to recognize selected JSON payloads."""
    for size in range(len(path), 0, -1):
        budget.consume("work")
        prefix = path[:size]
        try:
            usage = prepared.observe(prefix)
        except LimitExceeded:
            raise
        except InputError:
            continue
        if usage.limitation:
            continue
        node = at_path(prepared._document, prefix)
        for occurrence in usage.occurrences:
            budget.consume("work", len(occurrence.value_json) + 1)
            value = occurrence.value
            if (
                isinstance(value, dict)
                and value.get("@type") == "@json"
                and value.get("@value") == node
            ):
                return True
    return False


def check_term_usages(
    prepared: PreparedUsages,
    regions: Sequence[TermUsageRegion],
    *,
    vocabulary: TermUsageVocabulary | None = None,
    budget: Budget | None = None,
) -> TermUsageResult:
    """Check direct source keys under actual scoped contexts and explicit policy.

    This opt-in check never declares legal @vocab expansion invalid JSON-LD.
    Regions refer to concrete source occurrences, with explicit extension and
    ignored-term permissions. Keyword/context metadata and opaque JSON contents
    are excluded from application-property membership. Unsupported associations
    remain required unknown. The originating prepared budget/resource owner is
    reused; independently prepared documents and blank labels are never joined.
    Supplied membership lookup is trusted read-only Python code, not sandboxed
    or authorized to fetch external resources by this operation.
    """
    if not isinstance(prepared, PreparedUsages):
        raise InputError("Term usage requires PreparedUsages")
    if budget is not None and (
        not isinstance(budget, Budget) or budget is not prepared._budget
    ):
        raise InputError("Term usage requires the originating prepared Budget")
    if vocabulary is not None and not isinstance(vocabulary, TermUsageVocabulary):
        raise InputError("Membership requires a trusted read-only vocabulary lookup")
    operation = prepared._budget
    issues = IssueCollector(operation)
    children: list[Report] = []
    usages: list[TermUsage] = []
    exclusions: list[str] = []
    checked = 0
    requested = 1
    optional_empty = False
    try:
        declarations = _capture(regions, operation)
        requested = max(1, len(declarations))
        optional_empty = bool(declarations) and all(
            captured.declaration.selector.minimum == 0 for captured in declarations
        )
        if not declarations:
            issues.append(
                Issue(
                    "context.term_usage_unverified",
                    "No required term usage regions were selected",
                    kind="unverified",
                )
            )
        selected: dict[SourcePath, _Region] = {}
        for captured in declarations:
            region = captured.declaration
            if region.membership is not None and vocabulary is None:
                raise InputError(
                    "Selected term membership requires an owned vocabulary"
                )
            selection = resolve_selection(
                region.selector,
                prepared._document,
                prepared.entities,
                prepared.observe,
                operation,
                require_usage=False,
            )
            retain_report(selection.report, operation, issues)
            children.append(selection.report)
            for path in selection.paths:
                operation.consume("work")
                bound_path(path, operation)
                node = at_path(prepared._document, path)
                if not isinstance(node, dict):
                    issues.append(
                        Issue(
                            "context.term_usage_unverified",
                            "Selected term usage scope is not an object",
                            bounded_location(path, operation),
                            kind="unverified",
                        )
                    )
                    continue
                if path in selected:
                    prior = selected[path]
                    previous = prior.declaration
                    operation.consume(
                        "work", prior.comparison_work + captured.comparison_work
                    )
                    if (
                        previous.implicit,
                        previous.allow_compact_iris,
                        previous.allow_full_iris,
                        prior.ignored,
                        previous.membership,
                    ) != (
                        region.implicit,
                        region.allow_compact_iris,
                        region.allow_full_iris,
                        captured.ignored,
                        region.membership,
                    ):
                        raise InputError(
                            "Overlapping source objects have conflicting term policies"
                        )
                    continue
                operation.consume("states")
                selected[path] = captured
        retain_report(
            Report(
                "prepared omissions", issues=prepared._omission_issues, allow_empty=True
            ),
            operation,
            issues,
        )
        exclusions.extend(
            bounded_location(path, operation) for path in prepared.allowed_omissions
        )
        requested = max(1, len(selected))
        operation.consume("resources")
        observed = (
            source_terms(
                prepared._document,
                tuple(selected),
                base=prepared._base,
                resources=prepared._loader,
                budget=operation,
            )
            if selected
            else {}
        )
        for path, captured in selected.items():
            region = captured.declaration
            operation.consume("work")
            keys = observed.get(path)
            if keys is None:
                where = bounded_location(path, operation)
                if _opaque(prepared, path, operation):
                    operation.consume("states")
                    usages.append(TermUsage(path, "", None, "opaque", True))
                    exclusions.append(where)
                    checked += 1
                else:
                    issues.append(
                        Issue(
                            "context.term_usage_unverified",
                            "No exact processed source-object context was established",
                            where,
                            kind="unverified",
                        )
                    )
                continue
            if not keys:
                issues.append(
                    Issue(
                        "context.term_usage_unverified",
                        "Selected object contains no actual key uses",
                        bounded_location(path, operation),
                        kind="unverified",
                    )
                )
            for term in keys:
                operation.consume("work", len(term.term) + 1)
                key_path = (*path, term.term)
                where = bounded_location(key_path, operation)
                role = _role(term, operation)
                ignored = term.term in captured.ignored
                accepted = (
                    role in ("explicit", "keyword")
                    or role == "implicit-vocabulary"
                    and region.implicit == "allow"
                    or role == "compact-iri"
                    and region.allow_compact_iris
                    or role == "full-iri"
                    and region.allow_full_iris
                )
                if role == "keyword" or ignored:
                    accepted = True
                    exclusions.append(where)
                elif not accepted:
                    issues.append(
                        Issue(
                            "context.term_usage_unverified"
                            if role == "unresolved"
                            else "context.term_usage_disallowed",
                            "Key role violates the selected application term policy",
                            where,
                            expected="explicit or permitted term use",
                            actual=term.iri or "null",
                            kind="unverified" if role == "unresolved" else "finding",
                        )
                    )
                if (
                    accepted
                    and role != "keyword"
                    and not ignored
                    and region.membership is not None
                ):
                    assert vocabulary is not None and term.iri is not None
                    bound_text(term.iri, operation)
                    operation.consume("work")
                    status = getattr(vocabulary.lookup(term.iri), "status", None)
                    if not isinstance(status, str):
                        raise InputError(
                            "Vocabulary lookup returned no finite term status"
                        )
                    bound_text(status, operation)
                    if status not in ("declared", "referenced", "absent"):
                        raise InputError(
                            "Vocabulary lookup returned an unsupported term status"
                        )
                    accepted = (
                        status == "declared"
                        if region.membership == "declared"
                        else status != "absent"
                    )
                    if not accepted:
                        issues.append(
                            Issue(
                                "context.term_usage_membership",
                                "Expanded key lacks required vocabulary membership",
                                where,
                                expected=region.membership,
                                actual=term.iri,
                            )
                        )
                operation.consume("states")
                usages.append(TermUsage(key_path, term.term, term.iri, role, accepted))
            checked += 1
    except LimitExceeded:
        issues.exhausted("Term usage exceeded the shared operation limits")
    except (ResourceError, UsageContextUnavailable):
        try:
            issues.append(
                Issue(
                    "context.term_usage_unverified",
                    "Maintained context processing is unavailable",
                    kind="unverified",
                )
            )
        except LimitExceeded as error:
            issues.exhausted(str(error))
    except ContextInputError:
        try:
            issues.append(
                Issue(
                    "context.term_usage_input_error",
                    "Maintained processing rejected the selected source input",
                    kind="input_error",
                )
            )
        except LimitExceeded as error:
            issues.exhausted(str(error))
    report = Report(
        "jsonld.term_usage",
        checked,
        tuple(issues),
        requested=max(checked, 0 if optional_empty and not checked else requested),
        allow_empty=optional_empty,
        children=tuple(children),
        regime="explicit-source-term-policy",
        exclusions=tuple(exclusions),
    )
    return TermUsageResult(tuple(usages), report)
