"""Complete semantic totals and bounded presentation for actual prepared records."""

from dataclasses import dataclass
from typing import Protocol, TypeAlias

from bijux_ontology.reports import Issue, Outcome, Report


@dataclass(frozen=True)
class FindingStreamLimits:
    """Separate run-wide delivery/sample limits from each engine's max_issues."""

    max_records: int = 1000
    max_emitted_findings: int = 10000
    max_retained: int = 20
    max_output_bytes: int = 10_000_000


@dataclass(frozen=True)
class CheckCoverage:
    """One actual report node, with its original parent path and completion."""

    path: tuple[int, ...]
    scope: str
    checked: int
    requested: int
    complete: bool
    outcome: Outcome
    regime: str


@dataclass(frozen=True)
class RecordResultEvent:
    """Actual per-record report scope; no finding samples determine this truth."""

    run_id: str
    record_index: int
    checks: tuple[CheckCoverage, ...]


@dataclass(frozen=True)
class FindingEvent:
    """A stable run/record/finding identity and actual enclosing report paths."""

    run_id: str
    record_index: int
    finding_index: int
    issue: Issue
    check_paths: tuple[tuple[int, ...], ...]


@dataclass(frozen=True)
class FindingStreamSummary:
    """Raw observed totals and two independent completion contracts.

    The footer confirms finished prior frames, without claiming its own future
    flush. Its callback remains pending; only the returned summary confirms
    whole-export completion after the final finish. A legacy Report is returned
    only when all reports and findings fit; samples never replace full truth.
    """

    run_id: str
    records_seen: int
    records_evaluated: int
    records_complete: int
    failing_records: int
    checked: int
    requested: int
    finding_count: int
    finding_counts: tuple[tuple[str, int], ...]
    record_outcomes: tuple[tuple[str, int], ...]
    emitted_findings: int
    output_bytes: int
    samples: tuple[FindingEvent, ...]
    semantic_complete: bool
    export_complete: bool
    raw_outcome: Outcome
    failure: Issue | None = None
    report: Report | None = None

    @property
    def finding_counts_complete(self) -> bool:
        """Qualify kind totals when a post-engine budget stop interrupts adoption."""
        return sum(count for _, count in self.finding_counts) == self.finding_count

    @property
    def verified(self) -> bool:
        """Require full semantic agreement and successful full selected export."""
        return (
            self.semantic_complete
            and self.export_complete
            and self.raw_outcome == "agreement"
        )


StreamEvent: TypeAlias = RecordResultEvent | FindingEvent | FindingStreamSummary


class FindingSink(Protocol):
    """Synchronous bounded handoff; caller code retains its ordinary lifetime."""

    def write(self, event: StreamEvent, payload: bytes) -> None:
        """Accept the whole prequalified JSONL frame or raise an observable failure."""
        ...

    def finish(self) -> None:
        """Flush accepted frames before and after the summary; do not close output."""
        ...
