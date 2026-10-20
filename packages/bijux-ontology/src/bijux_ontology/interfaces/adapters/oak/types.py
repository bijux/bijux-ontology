"""Optional native OAK ownership and required operation failure evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from bijux_ontology.diagnostics.reports import Report

Adapter = TypeVar("Adapter")


class OakOperationError(RuntimeError):
    """An OAK scalar interface cannot represent incomplete Bijux evidence."""

    def __init__(self, report: Report) -> None:
        """Keep the actual required report rather than turn it into empty output."""
        self.report = report
        super().__init__("OAK operation is not qualified: " + report.outcome)


@dataclass(frozen=True)
class OakPreparation(Generic[Adapter]):
    """A native concrete adapter exists only after bounded optional preparation."""

    adapter: Adapter | None
    report: Report
