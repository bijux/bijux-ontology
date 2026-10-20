"""SARIF 2.1.0 plus Errata 01 projection of native checking truth."""

import json
from pathlib import Path
from typing import cast

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import AcceptancePolicy, Issue, Report
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    bound_text,
    retain_report,
)
from bijux_ontology.json.values import decode_json, own_json
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.shacl.evidence.findings import ShaclFinding, term_data
from bijux_ontology.shacl.evidence.results import ShaclResult

from .locations import artifact_root as qualify_root
from .locations import location_data, root_uri

SCHEMA_URI = (
    "https://docs.oasis-open.org/sarif/sarif/v2.1.0/errata01/os/"
    "schemas/sarif-schema-2.1.0.json"
)


def _message(text: str, budget: Budget) -> dict[str, object]:
    """Keep native text literal rather than introducing SARIF placeholders or links."""
    bound_text(text, budget)
    budget.consume("work", len(text) * 5 + 1)
    extra = sum(text.count(character) for character in "\\[]{}")
    if not extra:
        return {"text": text}
    size = len(text) + extra
    if size > budget.limits.max_bytes:
        raise LimitExceeded("SARIF message escaping exceeds max_bytes")
    budget.consume("bytes", 320 + 20 * size)
    literal = (
        text.replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("{", "{{")
        .replace("}", "}}")
    )
    bound_text(literal, budget)
    return {"text": literal}


def _exit_code(
    report: Report,
    policy: AcceptancePolicy | None,
    native_work: int,
    budget: Budget,
) -> int:
    """Bound exact approval comparisons while delegating all policy semantics."""
    start = budget.remaining("work")
    if policy is not None:
        budget.consume("states", len(policy.exceptions))
        for selection in policy.exceptions:
            bound_text(selection.code, budget)
            bound_text(selection.location, budget)
            if selection.issue is not None:
                retain_report(
                    Report("sarif.acceptance", 1, (selection.issue,)),
                    budget,
                    IssueCollector(budget),
                )
        policy_work = start - budget.remaining("work")
        budget.consume(
            "work",
            native_work * len(policy.exceptions) + policy_work * len(report.issues),
        )
    budget.consume("work", len(report.issues) + 1)
    return report.exit_code(policy=policy)


def _scopes(report: Report, budget: Budget) -> list[dict[str, object]]:
    """Retain required child coverage without duplicating aggregated diagnostics."""
    records: list[dict[str, object]] = []
    pending: list[tuple[Report, int | None]] = [(report, None)]
    while pending:
        current, parent = pending.pop()
        budget.consume("states")
        budget.consume("work", len(current.issues) * 3 + 1)
        index = len(records)
        records.append(
            {
                "parent": parent,
                "scope": current.scope,
                "checked": current.checked,
                "requested": current.requested,
                "complete": current.complete,
                "allow_empty": current.allow_empty,
                "outcome": current.outcome,
                "verified": current.verified,
                "regime": current.regime,
                "families": list(current.families),
                "exclusions": list(current.exclusions),
            }
        )
        budget.consume("states", len(current.children))
        pending.extend((child, index) for child in reversed(current.children))
    return records


def _issue_data(issue: Issue, budget: Budget) -> dict[str, object]:
    """Decode bounded immutable evidence through the ordinary strict JSON boundary."""
    data: dict[str, object] = {
        "code": issue.code,
        "message": issue.message,
        "location": issue.location,
        "kind": issue.kind,
        "severity": issue.severity,
        "expected": issue.expected,
        "actual": issue.actual,
        "details": issue.details.to_dict() if issue.details is not None else None,
        "evidence": None,
    }
    if issue.evidence_json is not None:
        data["evidence"] = decode_json(
            issue.evidence_json,
            max_bytes=budget.limits.max_bytes,
            max_depth=budget.limits.max_depth,
        )
    return data


def _related(
    finding: ShaclFinding, root: Path | None, budget: Budget
) -> list[dict[str, object]]:
    """Follow actual report-local detail links once, preserving cycles as references."""
    result: list[dict[str, object]] = []
    if type(finding.details) is not tuple:
        raise InputError("SARIF requires finite SHACL detail tuples")
    budget.consume("states", len(finding.details))
    pending = list(finding.details)
    visited = {id(finding)}
    while pending:
        item = pending.pop()
        budget.consume("work")
        if (
            not isinstance(item, ShaclFinding)
            or type(item.details) is not tuple
            or type(item.messages) is not tuple
            or type(item.locations) is not tuple
        ):
            raise InputError("SARIF requires finite typed SHACL detail records")
        if id(item) in visited:
            continue
        budget.consume("states")
        visited.add(id(item))
        for node in (
            item.focus_node,
            item.path,
            item.result_node,
            item.value,
            item.severity,
            item.source_shape,
            item.constraint_component,
        ):
            if node is not None:
                bound_node(node, budget)
        budget.consume("states", len(item.messages))
        for message in item.messages:
            bound_node(message, budget)
        locations = item.locations or (None,)
        budget.consume("states", len(locations) + len(item.details))
        for details in locations:
            related = (
                location_data(details, str(item.focus_node or ""), root, budget) or {}
            )
            related["id"] = len(result) + 1
            length = sum(len(message) + 2 for message in item.messages)
            if length > budget.limits.max_bytes:
                raise LimitExceeded("SARIF related message exceeds max_bytes")
            budget.consume("work", length + 1)
            joined = "; ".join(map(str, item.messages)) or "Related SHACL result"
            bound_text(joined, budget)
            related["message"] = _message(joined, budget)
            related["properties"] = {
                "bijux": {
                    "result": term_data(item.result_node),
                    "focus_node": term_data(item.focus_node),
                    "path": term_data(item.path),
                    "value": term_data(item.value),
                    "details_complete": item.details_complete,
                }
            }
            result.append(related)
        pending.extend(item.details)
    return result


def to_sarif(
    value: Report | ShaclResult,
    *,
    artifact_root: Path | None = None,
    policy: AcceptancePolicy | None = None,
    budget: Budget | None = None,
) -> dict[str, object]:
    """Return a fresh local SARIF document without changing native acceptance.

    Findings are failed results; unresolved obligations are open results. Input
    errors and exhaustion are invocation notifications. Execution success means
    complete checking, including completed checks that found violations, rather
    than acceptance or a zero process exit. No individual pass is synthesized.
    """
    if not isinstance(value, (Report, ShaclResult)):
        raise InputError("SARIF requires a native Report or ShaclResult")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("SARIF budget requires a Budget")
    if policy is not None and not isinstance(policy, AcceptancePolicy):
        raise InputError("SARIF policy requires an AcceptancePolicy")
    if artifact_root is not None and not isinstance(artifact_root, Path):
        raise InputError("SARIF artifact_root requires a Path")
    operation = budget if budget is not None else Budget()
    report = value.report if isinstance(value, ShaclResult) else value
    if not isinstance(report, Report):
        raise InputError("SARIF requires a typed native report")
    start = operation.remaining("work")
    retain_report(report, operation, IssueCollector(operation))
    native_work = start - operation.remaining("work")
    exit_code = _exit_code(report, policy, native_work, operation)
    root = qualify_root(artifact_root, operation)
    scopes = _scopes(report, operation)
    rules: list[dict[str, object]] = []
    indices: dict[str, int] = {}
    results: list[dict[str, object]] = []
    notifications: list[dict[str, object]] = []
    findings: dict[tuple[str, str], ShaclFinding] = {}
    if isinstance(value, ShaclResult):
        if (
            type(value.findings) is not tuple
            or type(value.findings_complete) is not bool
        ):
            raise InputError("SARIF requires finite typed SHACL result metadata")
        if value.conforms is not None and type(value.conforms) is not bool:
            raise InputError("SHACL conformance must be a raw boolean or unavailable")
        bound_text(value.backend, operation)
        operation.consume("states", len(value.findings))
        for finding in value.findings:
            operation.consume("work")
            if not isinstance(finding, ShaclFinding):
                raise InputError("SARIF requires typed SHACL findings")
            if finding.result_node is not None:
                bound_node(finding.result_node, operation)
                node = term_data(finding.result_node)
                assert node is not None
                findings[(str(node["kind"]), str(node["value"]))] = finding
    for issue in report.issues:
        operation.consume("work")
        operation.consume("states")
        code = str(issue.code)
        if code not in indices:
            indices[code] = len(rules)
            rules.append({"id": code})
        where = location_data(issue.details, issue.location, root, operation)
        native = _issue_data(issue, operation)
        properties = {"bijux": native}
        if issue.kind in ("input_error", "exhausted"):
            notification: dict[str, object] = {
                "descriptor": {"id": code},
                "level": "error",
                "message": _message(issue.message, operation),
                "properties": properties,
            }
            if where is not None:
                notification["locations"] = [where]
            notifications.append(notification)
            continue
        result: dict[str, object] = {
            "ruleId": code,
            "ruleIndex": indices[code],
            "kind": "open" if issue.kind == "unverified" else "fail",
            "level": "none"
            if issue.kind == "unverified"
            else {"error": "error", "warning": "warning", "info": "note"}[
                issue.severity
            ],
            "message": _message(issue.message, operation),
            "properties": properties,
        }
        if where is not None:
            result["locations"] = [where]
        evidence = native.get("evidence")
        result_node = evidence.get("result") if isinstance(evidence, dict) else None
        matched = (
            findings.get((result_node["kind"], result_node["value"]))
            if isinstance(result_node, dict)
            and isinstance(result_node.get("kind"), str)
            and isinstance(result_node.get("value"), str)
            else None
        )
        if matched is not None:
            if matched.locations:
                operation.consume("states", len(matched.locations))
                result["locations"] = [
                    location_data(details, "", root, operation)
                    for details in matched.locations
                ]
            related = _related(matched, root, operation)
            if related:
                result["relatedLocations"] = related
        results.append(result)
    invocation: dict[str, object] = {
        "executionSuccessful": report.complete
        and (not isinstance(value, ShaclResult) or value.findings_complete),
        "exitCode": exit_code,
        "toolExecutionNotifications": notifications,
    }
    run: dict[str, object] = {
        "tool": {"driver": {"name": "bijux-ontology", "rules": rules}},
        "columnKind": "unicodeCodePoints",
        "invocations": [invocation],
        "results": results,
        "properties": {
            "bijux": {
                "scopes": scopes,
                "accepted": exit_code == 0,
                "complete": report.complete,
                "verified": report.verified,
                "outcome": report.outcome,
            }
        },
    }
    if isinstance(value, ShaclResult):
        cast(dict[str, object], run["properties"])["bijuxShacl"] = {
            "conforms": value.conforms,
            "backend": value.backend,
            "findings_complete": value.findings_complete,
        }
    if root is not None:
        uri = root_uri(root, operation)
        run["originalUriBaseIds"] = {"ARTIFACT_ROOT": {"uri": uri}}
    document = cast(
        dict[str, object],
        own_json(
            {"$schema": SCHEMA_URI, "version": "2.1.0", "runs": [run]},
            max_bytes=operation.limits.max_bytes,
            max_depth=operation.limits.max_depth,
        ),
    )
    encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    operation.consume("work", len(encoded))
    operation.consume("bytes", len(encoded.encode("utf-8")))
    return document


def export_sarif(
    value: Report | ShaclResult,
    destination: Path,
    *,
    artifact_root: Path | None = None,
    policy: AcceptancePolicy | None = None,
    budget: Budget | None = None,
) -> Path:
    """Write a complete local artifact exclusively; never overwrite an existing file."""
    if not isinstance(destination, Path):
        raise InputError("SARIF destination requires a Path")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("SARIF budget requires a Budget")
    operation = budget if budget is not None else Budget()
    bound_text(str(destination), operation)
    data = to_sarif(value, artifact_root=artifact_root, policy=policy, budget=operation)
    content = json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n"
    size = len(content.encode("utf-8"))
    if size > operation.limits.max_bytes:
        raise LimitExceeded("SARIF output exceeds max_bytes")
    operation.consume("work", len(content))
    operation.consume("bytes", size)
    with destination.open("x", encoding="utf-8") as stream:
        stream.write(content)
    return destination
