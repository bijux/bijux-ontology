"""Immutable ordered outcomes and explicit accounting for parallel project jobs."""

from dataclasses import dataclass
from typing import Literal

from bijux_ontology.limits import BudgetKind
from bijux_ontology.reports import Report

JobState = Literal["completed", "pending", "unsupported", "interrupted"]


@dataclass(frozen=True)
class ParallelJobResult:
    """One declaration-ordered job, actual native report and controlled counters.

    Interrupted workers forfeit their grant because their completed work cannot
    be measured after termination. Native opaque engine CPU is deadline bounded;
    counters describe the explicitly instrumented preparation and projection.
    """

    name: str
    state: JobState
    report: Report
    consumed: tuple[tuple[BudgetKind, int], ...] = ()
    granted: tuple[tuple[BudgetKind, int], ...] = ()
    started_ns: int | None = None
    finished_ns: int | None = None
    reason: str = ""


@dataclass(frozen=True)
class ParallelProjectResult:
    """Retain every selected job, including unstarted required dependencies."""

    jobs: tuple[ParallelJobResult, ...]
    report: Report
