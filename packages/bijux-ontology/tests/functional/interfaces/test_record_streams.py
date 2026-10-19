"""Actual record preparation with asynchronous backpressure and joined cleanup."""

import asyncio
import json
import threading
import time
from collections.abc import Iterator

import pytest
from rdflib import Literal

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget
from bijux_ontology.interfaces.execution.record_streams import AsyncJSONLDRecords
from bijux_ontology.jsonld.streams import JSONLDRecord, check_jsonld_records
from bijux_ontology.rdf.literals import check_literal
from bijux_ontology.rdf.sources.snapshots import PreparedSource


def _record(value: str = "01") -> JSONLDRecord:
    """Supply one real typed literal through maintained JSON-LD preparation."""
    return JSONLDRecord(
        json.dumps(
            {
                "@id": "urn:item",
                "urn:value": {
                    "@value": value,
                    "@type": "http://www.w3.org/2001/XMLSchema#integer",
                },
            }
        )
    )


def _check(source: PreparedSource, budget: Budget) -> Report:
    """Check the actual prepared lexical term without replacing native evidence."""
    value = next(source.graph().objects())
    assert isinstance(value, Literal)
    return check_literal(value, budget=budget)


async def _entered(event: threading.Event) -> None:
    """Keep the event loop active while the actual caller callback is admitted."""
    deadline = time.monotonic() + 5
    while not event.is_set():
        assert time.monotonic() < deadline
        await asyncio.sleep(0.002)


def test_async_records_keep_sync_truth_and_pull_only_on_demand() -> None:
    pulled: list[int] = []

    def frames() -> Iterator[JSONLDRecord]:
        """Expose tail access separately from actual first-result preparation."""
        for index, value in enumerate(("01", "invalid")):
            pulled.append(index)
            yield _record(value)

    expected = tuple(
        check_jsonld_records((_record(), _record("invalid")), check=_check)
    )

    async def run() -> None:
        stream = check_jsonld_records(frames(), check=_check)
        async with AsyncJSONLDRecords(stream) as records:
            assert pulled == []
            first = await anext(records)
            assert pulled == [0] and first.report.verified
            second = await anext(records)
            assert pulled == [0, 1] and not second.report.verified
            assert [first.report.to_dict(), second.report.to_dict()] == [
                item.report.to_dict() for item in expected
            ]
            with pytest.raises(StopAsyncIteration):
                await anext(records)
            assert stream.finished and not records.report.verified

    asyncio.run(run())


def test_repeated_async_cancel_waits_for_the_admitted_callback_and_retains_result() -> (
    None
):
    entered, release = threading.Event(), threading.Event()
    prior = {thread.ident for thread in threading.enumerate()}

    def check(source: PreparedSource, budget: Budget) -> Report:
        """Hold a real check so cancellation and cleanup races are observable."""
        entered.set()
        assert release.wait(5)
        return _check(source, budget)

    async def run() -> None:
        stream = check_jsonld_records((_record(), _record()), check=check)
        async with AsyncJSONLDRecords(stream) as records:
            pending = asyncio.create_task(anext(records))
            await _entered(entered)
            with pytest.raises(InputError, match="only one pull"):
                await anext(records)
            pending.cancel()
            await asyncio.sleep(0.01)
            pending.cancel()
            await asyncio.sleep(0.01)
            assert not pending.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert records.last_result is not None
            assert records.last_result.report.verified
            assert stream.processed == 1 and not records.report.complete
            assert not records.report.verified
        assert not [
            t
            for t in threading.enumerate()
            if t.ident not in prior and t.name.startswith("bijux-record")
        ]
        async with AsyncJSONLDRecords(
            check_jsonld_records((_record(),), check=_check)
        ) as fresh:
            assert (await anext(fresh)).report.verified
            with pytest.raises(StopAsyncIteration):
                await anext(fresh)
            assert fresh.report.verified

    try:
        asyncio.run(run())
    finally:
        release.set()


def test_async_close_drains_active_call_and_callback_failure_cannot_resume() -> None:
    entered, release = threading.Event(), threading.Event()

    def check(source: PreparedSource, budget: Budget) -> Report:
        """Propagate the real caller failure after cleanup has been requested."""
        entered.set()
        assert release.wait(5)
        raise LookupError("consumer failed")

    async def run() -> None:
        stream = check_jsonld_records((_record(), _record()), check=check)
        records = AsyncJSONLDRecords(stream)
        async with records:
            pending = asyncio.create_task(anext(records))
            await _entered(entered)
            closing = asyncio.create_task(records.aclose())
            await asyncio.sleep(0.01)
            closing.cancel()
            await asyncio.sleep(0.01)
            assert not pending.done() and not closing.done()
            release.set()
            with pytest.raises(LookupError, match="consumer failed"):
                await pending
            with pytest.raises(asyncio.CancelledError):
                await closing
            with pytest.raises(StopAsyncIteration):
                await anext(records)
            assert not stream.finished and not records.report.complete

    try:
        asyncio.run(run())
    finally:
        release.set()


@pytest.mark.parametrize("boundary", ["producer", "check"])
def test_caller_cancelled_error_finishes_its_owned_future_without_retry(
    boundary: str,
) -> None:
    def frames() -> Iterator[JSONLDRecord]:
        """An ordinary producer cancellation remains a required unchecked tail."""
        if boundary == "producer":
            raise asyncio.CancelledError
        yield _record()

    def check(source: PreparedSource, budget: Budget) -> Report:
        """A caller may signal cancellation from its ordinary check function."""
        raise asyncio.CancelledError

    async def run() -> None:
        stream = check_jsonld_records(frames(), check=check)
        async with AsyncJSONLDRecords(stream) as records:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(anext(records), 2)
            assert not records.report.complete and not stream.finished

    asyncio.run(run())
