"""Validate JSON reports exchanged only with the owned execution worker."""

import json
from typing import cast

from bijux_ontology._json import object_value
from bijux_ontology.errors import InputError
from bijux_ontology.reports import Issue, IssueKind, Location, Report, Severity


def report_from_data(value: object) -> Report:
    """Reconstruct validated truth; never trust transmitted policy or exit status."""
    data = object_value(value, location="worker report")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise InputError("Unsupported worker report schema")
    raw_issues = data.get("issues")
    raw_children = data.get("scopes")
    if not isinstance(raw_issues, list) or not isinstance(raw_children, list):
        raise InputError("Worker report requires issues and scopes arrays")
    issues = []
    for raw in raw_issues:
        item = object_value(raw, location="worker issue")
        details = item.get("details")
        location = None
        if details is not None:
            part = object_value(details, location="worker location")
            location = Location(
                source=cast(str, part.get("source", "")),
                path=cast(tuple[str | int, ...], part.get("path", ())),
                term=cast(str | None, part.get("term")),
                graph=cast(str | None, part.get("graph")),
                model=cast(str | None, part.get("model")),
                branch=cast(int | None, part.get("branch")),
            )
        issues.append(
            Issue(
                cast(str, item.get("code")),
                cast(str, item.get("message")),
                location=cast(str, item.get("location", "")),
                kind=cast(IssueKind, item.get("kind")),
                severity=cast(Severity, item.get("severity")),
                expected=cast(str | None, item.get("expected")),
                actual=cast(str | None, item.get("actual")),
                details=location,
                evidence_json=(
                    json.dumps(item["evidence"], allow_nan=False)
                    if item.get("evidence") is not None
                    else None
                ),
            )
        )
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
        children=tuple(report_from_data(child) for child in raw_children),
    )
