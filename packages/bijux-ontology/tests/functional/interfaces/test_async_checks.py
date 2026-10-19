"""Real async owned workers, bounded admission and cancellation cleanup."""

import asyncio
import os
import subprocess
import time
from collections.abc import Awaitable
from pathlib import Path

import pytest

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.interfaces.execution.async_checks import (
    AsyncChecks,
    PreparedWorkerCheck,
)
from bijux_ontology.interfaces.execution.executor import ExecutionResult, WorkerExecutor
from bijux_ontology.limits import Limits

VOCABULARY: dict[str, object] = {
    "source": "<urn:A> a <http://www.w3.org/2002/07/owl#Class>.",
    "format": "turtle",
    "terms": ["urn:A"],
}


def _live(root: Path) -> set[int]:
    """Inspect only the actual installed worker commands owned by this test."""
    output = subprocess.check_output(["ps", "-ww", "-axo", "pid=,command="], text=True)
    return {
        int(line.split(maxsplit=1)[0])
        for line in output.splitlines()
        if "bijux_ontology.interfaces.execution.worker" in line and str(root) in line
    }


def _project(directory: Path) -> PreparedWorkerCheck:
    """Run a real completed failure before expensive native RDF preparation."""
    directory.mkdir()
    (directory / "small.ttl").write_text("<urn:Present> <urn:p> <urn:o>.")
    (directory / "inventory.json").write_text('["urn:Missing"]')
    large = VOCABULARY["source"]
    assert isinstance(large, str)
    (directory / "large.ttl").write_text(
        large + "\n" + "".join(f"<urn:n{i}> <urn:p> <urn:o>.\n" for i in range(150000))
    )
    return PreparedWorkerCheck(
        "project",
        {
            "configuration": {
                "version": 1,
                "rdf": {"small": {"path": "small.ttl"}, "large": {"path": "large.ttl"}},
                "checks": [
                    {
                        "name": "first",
                        "kind": "vocabulary",
                        "source": "small",
                        "inventory": "inventory.json",
                    },
                    {
                        "name": "expensive",
                        "kind": "vocabulary",
                        "source": "large",
                        "inventory": "inventory.json",
                    },
                ],
            },
            "directory": str(directory),
        },
    )


async def _checkpoint(root: Path) -> None:
    """Wait for actual paid evidence, without a guessed sleep as cancellation proof."""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if any(
            "vocabulary.term_missing" in path.read_text()
            for path in root.glob("*/completed.jsonl")
        ):
            return
        await asyncio.sleep(0.005)
    raise AssertionError("The actual worker did not publish its completed first check")


async def _heartbeat(until: Awaitable[object]) -> tuple[object, int]:
    """Count event-loop progress while an actual request remains outstanding."""
    task = asyncio.ensure_future(until)
    beats = 0
    while not task.done():
        beats += 1
        await asyncio.sleep(0.005)
    return await task, beats


def test_owned_requests_batch_and_actual_synchronous_equivalence(
    tmp_path: Path,
) -> None:
    """Concurrent actual checks preserve direct truth and keep the loop responsive."""
    executor = WorkerExecutor(artifacts=tmp_path / "runs")
    direct = executor.run("vocabulary", VOCABULARY)
    assert direct.report.verified
    original = dict(VOCABULARY)
    prepared = PreparedWorkerCheck("vocabulary", original)
    original["source"] = "invalid"
    exported = prepared.parameters
    exported["source"] = "also invalid"
    bad = PreparedWorkerCheck("vocabulary", VOCABULARY | {"terms": ["urn:Missing"]})

    async def run() -> None:
        async with AsyncChecks(executor, max_running=2, max_queued=1) as checks:
            results, beats = await _heartbeat(
                asyncio.gather(checks.run(prepared), checks.run(prepared))
            )
            assert isinstance(results, list)
            assert all(item.report == direct.report for item in results)
            assert beats >= 2
            batch = await checks.batch((prepared, bad, prepared))
            assert batch.report.outcome == "disagreement"
            assert batch.report.requested == 3 and len(batch.results) == 3
            assert all(item is not None for item in batch.results)
            assert batch.results[0] is not None
            assert batch.results[0].report == direct.report
            assert batch.results[1] is not None
            assert any(
                issue.code == "vocabulary.term_missing"
                for issue in batch.results[1].report.issues
            )
        assert not list((tmp_path / "runs").iterdir())

    asyncio.run(run())


@pytest.mark.skipif(os.name != "posix", reason="POSIX process ownership evidence")
def test_active_cancel_reaps_one_worker_and_preserves_independent_request(
    tmp_path: Path,
) -> None:
    """Repeated task cancellation stops owned work and retains completed failures."""
    project = _project(tmp_path / "project")
    root = tmp_path / "runs"
    executor = WorkerExecutor(
        artifacts=root, timeout=30, limits=Limits(max_work=50_000_000)
    )
    (tmp_path / "project" / "valid.json").write_text('["urn:A"]')
    independent = PreparedWorkerCheck(
        "project",
        {
            "configuration": {
                "version": 1,
                "rdf": {"large": {"path": "large.ttl"}},
                "checks": [
                    {
                        "name": "independent",
                        "kind": "vocabulary",
                        "source": "large",
                        "inventory": "valid.json",
                    }
                ],
            },
            "directory": str(tmp_path / "project"),
        },
    )

    async def run() -> None:
        async with AsyncChecks(executor, max_running=2) as checks:
            first = asyncio.create_task(checks.run(project))
            second = asyncio.create_task(checks.run(independent))
            await _checkpoint(root)
            assert not second.done()
            deadline = time.monotonic() + 5
            while len(_live(root)) != 2 and time.monotonic() < deadline:
                await asyncio.sleep(0.005)
            assert len(_live(root)) == 2 and not second.done()
            first.cancel()
            first.cancel()
            result, beats = await _heartbeat(first)
            assert isinstance(result, ExecutionResult)
            assert result.report.outcome == "exhausted"
            assert {issue.code for issue in result.report.issues} >= {
                "execution.cancelled",
                "vocabulary.term_missing",
            }
            assert beats > 0 and first.cancelling() == 0
            other = await second
            assert other.report.verified
            assert not _live(root)
            assert (
                await checks.run(checks.prepare("vocabulary", VOCABULARY))
            ).report.verified
        assert not list(root.iterdir())

    asyncio.run(run())


@pytest.mark.skipif(os.name != "posix", reason="POSIX process ownership evidence")
def test_backpressure_queued_cancel_and_uninterruptible_session_close(
    tmp_path: Path,
) -> None:
    """Finite queued work never starts after close, even if close is cancelled."""
    project = _project(tmp_path / "project")
    root = tmp_path / "runs"
    executor = WorkerExecutor(
        artifacts=root, timeout=30, limits=Limits(max_work=50_000_000)
    )

    async def run() -> None:
        async with AsyncChecks(executor, max_running=1, max_queued=1) as checks:
            active = asyncio.create_task(checks.run(project))
            await _checkpoint(root)
            waiting = asyncio.create_task(
                checks.run(checks.prepare("vocabulary", VOCABULARY))
            )
            await asyncio.sleep(0)
            overflow = await checks.run(checks.prepare("vocabulary", VOCABULARY))
            assert overflow.report.outcome == "exhausted" and len(_live(root)) == 1
            waiting.cancel()
            queued = await waiting
            assert any(
                issue.code == "execution.cancelled" for issue in queued.report.issues
            )
            assert not active.done()
            again = asyncio.create_task(
                checks.run(checks.prepare("vocabulary", VOCABULARY))
            )
            await asyncio.sleep(0)
            closer = asyncio.create_task(checks.aclose())
            await asyncio.sleep(0)
            closer.cancel()
            closer.cancel()
            _, beats = await _heartbeat(closer)
            assert beats > 0
            assert (await active).report.outcome == "exhausted"
            assert (await again).report.outcome == "exhausted"
            assert not _live(root) and not list(root.iterdir())
            with pytest.raises(InputError):
                await checks.run(project)

    asyncio.run(run())


def test_batch_cancellation_retains_raw_prefix_and_required_unstarted_tail(
    tmp_path: Path,
) -> None:
    """A cancelled batch retains completed actual results and never hides its tail."""
    project = _project(tmp_path / "project")
    root = tmp_path / "runs"
    failed = PreparedWorkerCheck("vocabulary", VOCABULARY | {"terms": ["urn:Missing"]})
    good = PreparedWorkerCheck("vocabulary", VOCABULARY)
    executor = WorkerExecutor(
        artifacts=root, timeout=30, limits=Limits(max_work=50_000_000)
    )

    async def run() -> None:
        async with AsyncChecks(executor, max_running=1) as checks:
            task = asyncio.create_task(checks.batch((failed, project, good)))
            await _checkpoint(root)
            task.cancel()
            result = await task
            assert result.report.outcome == "exhausted"
            assert result.report.requested == 3 and result.results[2] is None
            assert result.results[0] is not None and result.results[1] is not None
            assert result.results[0].report.outcome == "disagreement"
            assert any(
                issue.code == "vocabulary.term_missing"
                for issue in result.report.issues
            )
            assert any(
                issue.code == "execution.cancelled" for issue in result.report.issues
            )
            assert not list(root.iterdir())
            assert (await checks.run(good)).report.verified

    asyncio.run(run())


def test_request_limits_lifecycle_and_batch_pending_coverage(tmp_path: Path) -> None:
    """Bounds reject before startup and keep captured required batch counts."""
    with pytest.raises(LimitExceeded):
        PreparedWorkerCheck(
            "vocabulary", {"source": "x" * 10000}, limits=Limits(max_bytes=1000)
        )
    with pytest.raises(InputError):
        AsyncChecks(WorkerExecutor(), max_running=0)

    class AlternateExecutor(WorkerExecutor):
        """A host subclass is not the owned process protocol boundary."""

    with pytest.raises(InputError):
        AsyncChecks(AlternateExecutor())
    request = PreparedWorkerCheck("vocabulary", VOCABULARY)
    executor = WorkerExecutor(artifacts=tmp_path / "runs", limits=Limits(max_states=3))

    async def run() -> None:
        checks = AsyncChecks(executor, max_running=1, max_queued=1)
        for attribute, value in (("max_running", 100), ("executor", WorkerExecutor())):
            with pytest.raises(AttributeError):
                setattr(checks, attribute, value)
        with pytest.raises(InputError):
            await checks.run(request)
        async with checks:
            batch = await checks.batch((request, request))
            assert batch.report.outcome == "exhausted" and batch.report.requested == 2
            assert not batch.results
        await checks.aclose()
        with pytest.raises(InputError):
            await checks.__aenter__()
        assert not (tmp_path / "runs").exists()

    asyncio.run(run())


def test_optional_jsonld_actual_output_matches_synchronous_worker(
    tmp_path: Path,
) -> None:
    """Actual native processor output is retained rather than a substituted fixture."""
    pytest.importorskip("pyld")
    source = {"@id": "urn:r", "label": "alpha", "@context": "urn:context"}
    resources = {"urn:context": {"@context": {"label": "urn:label"}}}
    executor = WorkerExecutor(artifacts=tmp_path / "runs")
    prepared = PreparedWorkerCheck(
        "flatten", {"document": source, "resources": resources}
    )
    direct = executor.run("flatten", prepared.parameters)
    resources["urn:context"]["@context"]["label"] = "urn:changed"
    source["label"] = "changed"
    assert direct.report.verified

    async def run() -> None:
        async with AsyncChecks(executor) as checks:
            actual = await checks.run(prepared)
            assert actual.report == direct.report and actual.data == direct.data
            batch = await checks.batch((prepared, prepared))
            assert batch.report.verified
            assert all(
                item is not None and item.data == direct.data for item in batch.results
            )

    asyncio.run(run())


def test_actual_deadline_native_input_failure_and_following_recovery(
    tmp_path: Path,
) -> None:
    """Worker interruption and native parser errors remain nonpositive async results."""
    request = PreparedWorkerCheck("vocabulary", VOCABULARY)
    invalid = PreparedWorkerCheck(
        "vocabulary", {"source": "not Turtle!", "format": "turtle", "terms": ["urn:A"]}
    )

    async def run() -> None:
        deadline_root = tmp_path / "deadline"
        async with AsyncChecks(
            WorkerExecutor(artifacts=deadline_root, timeout=0.005), max_running=1
        ) as checks:
            result = await checks.run(request)
            assert result.report.outcome == "exhausted"
            assert any(
                issue.code == "execution.deadline" for issue in result.report.issues
            )
        assert not list(deadline_root.iterdir())
        async with AsyncChecks(WorkerExecutor(artifacts=tmp_path / "normal")) as checks:
            failure = await checks.run(invalid)
            assert failure.report.outcome == "input_error"
            assert (await checks.run(request)).report.verified
        oversized = PreparedWorkerCheck("vocabulary", {"source": "x" * 2000})
        capped_root = tmp_path / "capped"
        async with AsyncChecks(
            WorkerExecutor(artifacts=capped_root, limits=Limits(max_bytes=1000))
        ) as checks:
            assert (await checks.run(oversized)).report.outcome == "exhausted"
        assert not capped_root.exists()

    asyncio.run(run())
