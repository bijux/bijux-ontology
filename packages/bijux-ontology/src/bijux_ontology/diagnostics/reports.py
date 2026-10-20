"""Truthful outcomes and acceptance policy for finite, explicitly scoped checks."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import KW_ONLY, dataclass, replace
from typing import Literal, cast

from bijux_ontology.diagnostics.errors import InputError, InputFailure, SourceSpan

IssueKind = Literal["finding", "unverified", "input_error", "exhausted"]
Severity = Literal["error", "warning", "info"]
Outcome = Literal[
    "agreement",
    "disagreement",
    "unverified",
    "input_error",
    "exhausted",
    "not_applicable",
]
_SEVERITY = {"info": 0, "warning": 1, "error": 2}


@dataclass(frozen=True)
class Location:
    """An established semantic location; coordinates are never fabricated."""

    source: str = ""
    path: tuple[str | int, ...] = ()
    term: str | None = None
    graph: str | None = None
    model: str | None = None
    branch: int | None = None
    failure: InputFailure | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or isinstance(
            cast(object, self.path), (str, bytes)
        ):
            raise InputError("A location requires a source string and path sequence")
        path = tuple(self.path)
        if any(
            type(part) not in (str, int) or isinstance(part, int) and part < 0
            for part in path
        ):
            raise InputError("Location paths contain keys or nonnegative indices")
        if self.branch is not None and (
            type(self.branch) is not int or self.branch < 0
        ):
            raise InputError("A branch index must be nonnegative")
        object.__setattr__(self, "path", path)
        if self.failure is not None and not isinstance(self.failure, InputFailure):
            raise InputError("Expected typed input failure details")

    @classmethod
    def from_error(cls, error: BaseException) -> Location | None:
        """Retain failing content separately from the check configuration path."""
        if isinstance(error, InputError) and error.failure is not None:
            return cls(
                source=error.failure.document_iri or error.failure.location,
                failure=error.failure,
            )
        return None

    def __str__(self) -> str:
        pointer = "".join(
            "/" + str(part).replace("~", "~0").replace("/", "~1") for part in self.path
        )
        return self.source + pointer or self.model or self.term or self.graph or ""

    def to_dict(self) -> dict[str, object]:
        """Return established location fields without assuming file coordinates."""
        data: dict[str, object] = {
            "source": self.source,
            "path": list(self.path),
            "term": self.term,
            "graph": self.graph,
            "model": self.model,
            "branch": self.branch,
        }
        if self.failure is not None:
            data["input_failure"] = self.failure.to_dict()
        return data


@dataclass(frozen=True)
class Issue:
    """A retained diagnostic, independent of whether process policy accepts it."""

    code: str
    message: str
    location: str = ""
    kind: IssueKind = "finding"
    severity: Severity = "error"
    expected: str | None = None
    actual: str | None = None
    _: KW_ONLY
    details: Location | None = None
    evidence_json: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code.strip():
            raise InputError("An issue needs a stable nonempty code")
        if not isinstance(self.message, str) or not self.message.strip():
            raise InputError("An issue needs a nonempty message")
        if self.kind not in ("finding", "unverified", "input_error", "exhausted"):
            raise InputError("Unknown diagnostic kind")
        if not isinstance(self.severity, str) or self.severity not in _SEVERITY:
            raise InputError("Unknown diagnostic severity")
        if not isinstance(self.location, str):
            raise InputError("Expected a readable location string")
        if any(
            value is not None and not isinstance(value, str)
            for value in (self.expected, self.actual)
        ):
            raise InputError("Diagnostic expected and actual values must be strings")
        if self.details is not None and not isinstance(self.details, Location):
            raise InputError("Expected a structured semantic location")
        if self.evidence_json is not None:
            if not isinstance(self.evidence_json, str):
                raise InputError("Expected JSON object evidence text")
            try:
                evidence = json.loads(self.evidence_json)
                if not isinstance(evidence, dict):
                    raise TypeError("Evidence must be an object")
                owned = json.dumps(
                    evidence, sort_keys=True, separators=(",", ":"), allow_nan=False
                )
            except (TypeError, ValueError, RecursionError) as error:
                raise InputError("Expected finite JSON object evidence") from error
            object.__setattr__(self, "evidence_json", owned)
        if self.details is not None and not self.location:
            object.__setattr__(self, "location", str(self.details))

    @property
    def evidence(self) -> dict[str, object] | None:
        """Return independent structured evidence without exposing mutable ownership."""
        return (
            cast(dict[str, object], json.loads(self.evidence_json))
            if self.evidence_json is not None
            else None
        )

    def to_dict(self) -> dict[str, object]:
        """Return original diagnostic values, including optional semantic location."""
        return {
            "code": self.code,
            "message": self.message,
            "location": self.location,
            "kind": self.kind,
            "severity": self.severity,
            "expected": self.expected,
            "actual": self.actual,
            "details": self.details.to_dict() if self.details else None,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class IssueSelection:
    """An exact code/location exception; neither unavailable inputs nor limits waive."""

    code: str
    location: str
    _: KW_ONLY
    issue: Issue | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.code, str)
            or not isinstance(self.location, str)
            or not self.code
            or not self.location
        ):
            raise InputError("An exception requires an exact code and location")
        if self.issue is not None and (
            not isinstance(self.issue, Issue)
            or (self.issue.code, self.issue.location) != (self.code, self.location)
        ):
            raise InputError("An exact exception must identify its original issue")

    @classmethod
    def for_issue(cls, issue: Issue) -> IssueSelection:
        """Bind an approval to all retained diagnostic fields, not a broad location."""
        return cls(issue.code, issue.location, issue=issue)

    def matches(self, issue: Issue) -> bool:
        """Select this diagnostic identity without wildcard or substring matching."""
        return (
            issue.code == self.code
            and issue.location == self.location
            and (self.issue is None or issue == self.issue)
        )

    def to_dict(self) -> dict[str, object]:
        """Preserve the exact approved evidence when an exception is serialized."""
        result: dict[str, object] = {"code": self.code, "location": self.location}
        if self.issue is not None:
            result["issue"] = self.issue.to_dict()
        return result


@dataclass(frozen=True)
class AcceptancePolicy:
    """Process acceptance changes no raw agreement, conformance or completeness."""

    minimum_severity: Severity = "error"
    strict: bool = False
    exceptions: tuple[IssueSelection, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.minimum_severity, str)
            or self.minimum_severity not in _SEVERITY
            or type(self.strict) is not bool
        ):
            raise InputError("Invalid acceptance policy")
        exceptions = tuple(self.exceptions)
        if any(not isinstance(item, IssueSelection) for item in exceptions):
            raise InputError("Expected exact issue selections")
        object.__setattr__(self, "exceptions", exceptions)

    def permits(self, issue: Issue) -> bool:
        """Return whether an exact exception permits a retained non-input issue."""
        return issue.kind in ("finding", "unverified") and any(
            item.matches(issue) for item in self.exceptions
        )


@dataclass(frozen=True)
class Report:
    """Requested scope, completed comparisons, retained diagnostics and raw outcome.

    Counts describe operation-defined comparisons, never inferred truth. An
    optional empty scope is not applicable; required empty work is incomplete.
    """

    scope: str
    checked: int = 0
    issues: tuple[Issue, ...] = ()
    allow_empty: bool = False
    _: KW_ONLY
    requested: int | None = None
    complete: bool = True
    regime: str = "asserted"
    families: tuple[str, ...] = ()
    children: tuple[Report, ...] = ()
    exclusions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.scope, str) or not self.scope.strip():
            raise InputError("A report needs a nonempty scope")
        if type(self.checked) is not int or self.checked < 0:
            raise InputError("checked must be a nonnegative integer")
        requested = self.checked if self.requested is None else self.requested
        if type(requested) is not int or requested < self.checked:
            raise InputError("requested must be an integer at least checked")
        if type(self.allow_empty) is not bool or type(self.complete) is not bool:
            raise InputError("Report completion and optional scope must be booleans")
        if not isinstance(self.regime, str) or not self.regime.strip():
            raise InputError("A report requires an explicit semantic regime")
        if isinstance(cast(object, self.families), (str, bytes)) or any(
            not isinstance(family, str) or not family.strip()
            for family in self.families
        ):
            raise InputError("Check families must be nonempty strings")
        if isinstance(cast(object, self.exclusions), (str, bytes)) or any(
            not isinstance(item, str) or not item.strip() for item in self.exclusions
        ):
            raise InputError("Excluded scopes must be nonempty strings")
        issues = tuple(self.issues)
        children = tuple(self.children)
        if any(not isinstance(issue, Issue) for issue in issues):
            raise InputError("A report requires typed issues")
        if any(not isinstance(child, Report) for child in children):
            raise InputError("A report requires typed child reports")
        # Aggregators already retain child diagnostics. Direct construction must
        # preserve the same truth without adding their comparison counts again.
        issues += tuple(
            issue for child in children for issue in child.issues if issue not in issues
        )
        if (
            not self.checked
            and not self.allow_empty
            and not any(
                issue.kind in ("input_error", "exhausted", "unverified")
                for issue in issues
            )
        ):
            issues += (
                Issue(
                    "input.empty_scope",
                    "No comparisons completed in a required scope",
                    self.scope,
                    kind="input_error",
                ),
            )
        complete = (
            self.complete
            and self.checked == requested
            and all(
                child.complete or child.allow_empty and not child.checked
                for child in children
            )
        )
        if not complete and not any(
            issue.kind in ("unverified", "input_error", "exhausted") for issue in issues
        ):
            issues += (
                Issue(
                    "execution.incomplete",
                    "Not all requested comparisons completed",
                    self.scope,
                    kind="unverified",
                ),
            )
        complete = complete and not any(
            issue.kind in ("unverified", "input_error", "exhausted") for issue in issues
        )
        object.__setattr__(self, "requested", requested)
        object.__setattr__(self, "issues", issues)
        object.__setattr__(self, "children", children)
        object.__setattr__(self, "exclusions", tuple(self.exclusions))
        object.__setattr__(self, "families", tuple(self.families) or (self.scope,))
        object.__setattr__(self, "complete", complete)

    @property
    def outcome(self) -> Outcome:
        """The raw checking outcome, unaffected by severity or exception policy."""
        for kind in ("input_error", "exhausted"):
            if any(issue.kind == kind for issue in self.issues):
                return "input_error" if kind == "input_error" else "exhausted"
        if any(issue.kind == "finding" for issue in self.issues):
            return "disagreement"
        if not self.complete:
            return "unverified"
        return "agreement" if self.checked else "not_applicable"

    def exit_code(
        self, *, strict: bool = False, policy: AcceptancePolicy | None = None
    ) -> int:
        """Return 2 for input/exhaustion, 1 for rejected findings, otherwise 0."""
        if self.outcome in ("input_error", "exhausted"):
            return 2
        selected = policy or AcceptancePolicy(strict=strict)
        for issue in self.issues:
            if selected.permits(issue):
                continue
            if (
                issue.kind == "finding"
                and _SEVERITY[issue.severity] >= _SEVERITY[selected.minimum_severity]
            ):
                return 1
            if issue.kind == "unverified" and (strict or selected.strict):
                return 1
        return 0

    @property
    def verified(self) -> bool:
        """Whether the supported requested scope completed in actual agreement."""
        return self.complete and self.outcome == "agreement"

    def __iter__(self) -> Iterator[Issue]:
        return iter(self.issues)

    def with_source(self, source: str) -> Report:
        """Attach a known input source without fabricating paths or file coordinates."""
        if not isinstance(source, str) or not source.strip():
            raise InputError("A diagnostic source must be a nonempty string")
        issues = []
        for issue in self.issues:
            if issue.details is not None and issue.details.source:
                issues.append(issue)
                continue
            details = (
                replace(issue.details, source=source)
                if issue.details is not None
                else Location(source=source)
            )
            issues.append(
                replace(
                    issue,
                    details=details,
                    location=source
                    + ("#" if issue.location.startswith("/") else ": ")
                    + issue.location
                    if issue.location
                    else source,
                )
            )
        return replace(
            self,
            issues=tuple(issues),
            children=tuple(child.with_source(source) for child in self.children),
        )

    def select_issues(
        self,
        *,
        code: str | None = None,
        kind: IssueKind | None = None,
        severity: Severity | None = None,
        location: str | None = None,
    ) -> tuple[Issue, ...]:
        """Filter presentation without changing the original report's outcome."""
        return tuple(
            issue
            for issue in self.issues
            if (code is None or issue.code == code)
            and (kind is None or issue.kind == kind)
            and (severity is None or issue.severity == severity)
            and (location is None or issue.location == location)
        )

    def to_dict(
        self, *, strict: bool = False, policy: AcceptancePolicy | None = None
    ) -> dict[str, object]:
        """Serialise schema-versioned meaning, counts and all original diagnostics."""
        issues = sorted(
            self.issues,
            key=lambda issue: (
                issue.location,
                issue.code,
                issue.kind,
                issue.expected or "",
                issue.actual or "",
                issue.message,
                issue.evidence_json or "",
            ),
        )
        selected = policy or AcceptancePolicy(strict=strict)
        return {
            "schema_version": 1,
            "exclusions": list(self.exclusions),
            "policy": {
                "minimum_severity": selected.minimum_severity,
                "strict": strict or selected.strict,
                "exceptions": [item.to_dict() for item in selected.exceptions],
                "permitted": [
                    {"code": issue.code, "location": issue.location}
                    for issue in issues
                    if selected.permits(issue)
                ],
            },
            "scope": self.scope,
            "regime": self.regime,
            "families": list(self.families),
            "checked": self.checked,
            "allow_empty": self.allow_empty,
            "requested": self.requested,
            "complete": self.complete,
            "outcome": self.outcome,
            "verified": self.verified,
            "exit_code": self.exit_code(strict=strict, policy=policy),
            "issues": [issue.to_dict() for issue in issues],
            "scopes": [
                child.to_dict(strict=strict, policy=policy) for child in self.children
            ],
        }

    def raise_for_findings(
        self, *, strict: bool = False, policy: AcceptancePolicy | None = None
    ) -> None:
        """Raise a focused exception retaining the complete, unfiltered report."""
        if self.exit_code(strict=strict, policy=policy):
            raise CheckError(self)


class CheckError(ValueError):
    """A failed requested check, retaining its complete structured report."""

    def __init__(self, report: Report) -> None:
        self.report = report
        detail = "; ".join(
            f"{issue.location}: {issue.message}" for issue in report.issues
        )
        super().__init__(detail or f"No comparisons completed in {report.scope}")


def combine_reports(scope: str, *reports: Report) -> Report:
    """Compose required scopes without losing optional emptiness or raw outcomes."""
    return Report(
        scope,
        sum(report.checked for report in reports),
        tuple(issue for report in reports for issue in report.issues),
        allow_empty=bool(reports) and all(report.allow_empty for report in reports),
        requested=sum(report.requested or 0 for report in reports),
        complete=all(
            report.complete or report.allow_empty and not report.checked
            for report in reports
        ),
        regime="composed",
        families=tuple(
            dict.fromkeys(family for report in reports for family in report.families)
        ),
        children=reports,
        exclusions=tuple(
            dict.fromkeys(item for report in reports for item in report.exclusions)
        ),
    )


def _report_object(value: object, *, location: str) -> dict[str, object]:
    """Require a decoded report object without importing input or budget layers."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise InputError("Expected an object", location=location)
    return cast(dict[str, object], value)


def _input_failure(value: object) -> InputFailure | None:
    """Reconstruct typed content evidence without interpreting parser messages."""
    if value is None:
        return None
    data = _report_object(value, location="worker input failure")
    chain = data.get("import_chain", [])
    if not isinstance(chain, list):
        raise InputError("Worker import chains require arrays")
    span = None
    if data.get("span") is not None:
        raw = _report_object(data["span"], location="worker parser span")
        span = SourceSpan(
            cast(int | None, raw.get("start_line")),
            cast(int | None, raw.get("start_column")),
            cast(int | None, raw.get("end_line")),
            cast(int | None, raw.get("end_column")),
            column_unit=cast(
                Literal["unicode_code_points", "utf16_code_units"] | None,
                raw.get("column_unit"),
            ),
        )
    return InputFailure(
        cast(str | None, data.get("document_iri")),
        cast(str, data.get("location", "")),
        cast(str, data.get("role", "input")),
        tuple(chain),
        span,
    )


def issue_from_data(value: object) -> Issue:
    """Reconstruct one typed issue without interpreting policy or parser messages."""
    item = _report_object(value, location="worker issue")
    details = item.get("details")
    location = None
    if details is not None:
        part = _report_object(details, location="worker location")
        location = Location(
            source=cast(str, part.get("source", "")),
            path=cast(tuple[str | int, ...], part.get("path", ())),
            term=cast(str | None, part.get("term")),
            graph=cast(str | None, part.get("graph")),
            model=cast(str | None, part.get("model")),
            branch=cast(int | None, part.get("branch")),
            failure=_input_failure(part.get("input_failure")),
        )
    return Issue(
        cast(str, item.get("code")),
        cast(str, item.get("message")),
        location=cast(str, item.get("location", "")),
        kind=cast(IssueKind, item.get("kind")),
        severity=cast(Severity, item.get("severity")),
        expected=cast(str | None, item.get("expected")),
        actual=cast(str | None, item.get("actual")),
        details=location,
        evidence_json=json.dumps(item["evidence"], allow_nan=False)
        if item.get("evidence") is not None
        else None,
    )


def _report_from_data(value: object, retained: dict[Issue, Issue]) -> Report:
    """Reconstruct validated truth; never trust transmitted policy or exit status."""
    data = _report_object(value, location="worker report")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise InputError("Unsupported worker report schema")
    if type(data.get("requested")) is not int:
        raise InputError(
            "Worker reports require an explicit requested comparison count"
        )
    raw_issues = data.get("issues")
    raw_children = data.get("scopes")
    if not isinstance(raw_issues, list) or not isinstance(raw_children, list):
        raise InputError("Worker report requires issues and scopes arrays")
    issues = [
        retained.setdefault(item, item)
        for raw in raw_issues
        for item in (issue_from_data(raw),)
    ]
    return Report(
        cast(str, data.get("scope")),
        cast(int, data.get("checked")),
        tuple(issues),
        allow_empty=cast(bool, data.get("allow_empty", False)),
        requested=cast(int, data.get("requested")),
        complete=cast(bool, data.get("complete")),
        regime=cast(str, data.get("regime")),
        families=cast(tuple[str, ...], data.get("families", ())),
        exclusions=cast(tuple[str, ...], data.get("exclusions", ())),
        children=tuple(_report_from_data(child, retained) for child in raw_children),
    )


def report_from_data(value: object) -> Report:
    """Reconstruct bounded decoded evidence with shared immutable issue ownership."""
    return _report_from_data(value, {})
