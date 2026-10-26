"""Paid Biolink diagnostic, interpretation and terminal wire accounting."""

import json
from collections.abc import Callable
from typing import cast

from bijux_ontology.diagnostics.errors import LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Location, Report
from bijux_ontology.execution.budgets import ControlBudget
from bijux_ontology.execution.limits import Budget, IssueCollector
from bijux_ontology.execution.protocol import json_bound

from .preparation import reserve


def association_diagnostics(
    location: str,
    index: int,
    budget: Budget,
    issues: IssueCollector,
    output_cap: int,
    response_size: list[int],
) -> Callable[..., None]:
    """Own the original live paid diagnostic closure within the response reserve."""

    def emit(
        code: str,
        message: str,
        path: tuple[str | int, ...] = (),
        *,
        unknown: bool = False,
    ) -> None:
        """Locate submitted association/qualifier ordinals without rewriting them."""
        # Parent and child reports both retain these actual coordinates. Bound
        # temporary encoding first, then reserve exact escaped wire capacity
        # before the issue becomes paid.
        issue = Issue(
            "biolink." + code,
            message,
            location,
            kind="unverified" if unknown else "finding",
            details=Location(source=location, path=(index, *path)),
        )
        metadata = issue.to_dict()
        maximum = json_bound(metadata, budget)
        budget.consume("bytes", 4 * maximum)
        budget.consume("work", maximum)
        amount = 512 + 2 * len(json.dumps(metadata, ensure_ascii=True))
        if response_size[0] + amount > output_cap:
            raise LimitExceeded("Biolink diagnostic output capacity exhausted")
        response_size[0] += amount
        issues.append(issue)

    return emit


def encode_constraints(constraints: dict[str, object], budget: Budget) -> str:
    """Produce the exact original constraint wire text after its original funding."""
    amount = json_bound(constraints, budget)
    budget.consume("work", amount)
    budget.consume("bytes", amount * 4)
    constraints_json = json.dumps(constraints, ensure_ascii=True, separators=(",", ":"))
    reserve(budget)
    return constraints_json


def admit_interpretation(
    interpretation: dict[str, object] | None,
    budget: Budget,
    output_cap: int,
    response_size: list[int],
) -> None:
    """Retain original wire-cap admission before publishing its association report."""
    encoded_size = json_bound(interpretation, budget)
    budget.consume("bytes", encoded_size * 2)
    observed = len(json.dumps(interpretation, ensure_ascii=True)) + 1024
    if response_size[0] + observed > output_cap:
        raise LimitExceeded("Biolink interpretation output capacity exhausted")
    response_size[0] += observed


def encode_response(
    native_identity: dict[str, object] | None,
    records: list[dict[str, object]],
    report: Report,
    budget: ControlBudget | None,
    request: dict[str, object] | None,
    cap: int,
) -> bytes:
    """Serialize only the qualified original terminal evidence and cumulative costs."""
    response = {
        "identity": native_identity,
        "interpretations": records,
        "report": report.to_dict(),
        "consumed": budget.consumed() if budget else None,
    }
    raw = json.dumps(response, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    maximum = request.get("output_cap", cap) if request is not None else cap
    return raw if len(raw) <= cast(int, maximum) else b"{}"
