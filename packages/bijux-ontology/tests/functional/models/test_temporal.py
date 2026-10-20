"""Actual emitted intervals retain calendar uncertainty and an explicit clock."""

import threading
from dataclasses import replace
from decimal import Inexact, getcontext, localcontext

import pytest
from rdflib import Literal
from rdflib.namespace import XSD

from bijux_ontology.limits import Budget, Limits
from bijux_ontology.models.temporal import (
    RelativeTemporal,
    TemporalField,
    TemporalInterval,
    TemporalOrder,
    check_temporal,
)

START = TemporalField(("start",), XSD.dateTime)
END = TemporalField(("end",), XSD.dateTime)
INTERVAL = TemporalInterval(START, END)
DOCUMENT = {"start": "2024-01-31T12:00:00Z", "end": "2024-02-29T12:00:00Z"}


def test_intervals_related_record_chronology_and_owned_source() -> None:
    pytest.importorskip("elementpath")
    document = {"records": [dict(DOCUMENT), {"start": "2024-02-29T13:00:00+01:00"}]}
    predecessor = TemporalField(("records", 0, "end"), XSD.dateTime)
    successor = TemporalField(("records", 1, "start"), XSD.dateTime)
    relation = TemporalOrder(predecessor, successor)
    result = check_temporal(document, chronology=[relation])
    assert result.report.verified
    assert not check_temporal(
        document, chronology=[replace(relation, strict=True)]
    ).report.verified
    document["records"][1]["start"] = "2024-01-01T00:00:00Z"
    assert (
        check_temporal(document, chronology=[relation]).report.outcome == "disagreement"
    )
    assert result.document != document
    copy = result.document
    assert isinstance(copy, dict)
    copy.clear()
    assert result.document
    assert check_temporal(DOCUMENT, intervals=[INTERVAL]).report.verified
    crossing = {
        "start": "2023-12-31T23:00:00-14:00",
        "end": "2024-01-01T00:00:00+14:00",
    }
    assert (
        check_temporal(crossing, intervals=[INTERVAL]).report.outcome == "disagreement"
    )
    assert check_temporal(
        {"start": crossing["end"], "end": crossing["start"]}, intervals=[INTERVAL]
    ).report.verified
    reversed_record = {"start": DOCUMENT["end"], "end": DOCUMENT["start"]}
    assert (
        check_temporal(reversed_record, intervals=[INTERVAL]).report.outcome
        == "disagreement"
    )


def test_partial_precision_and_explicit_open_end_policies() -> None:
    pytest.importorskip("elementpath")
    for value, datatype in (
        ("2024-01-31T12:00:00", XSD.dateTime),
        ("2024-01-31T12:00:00.1234567Z", XSD.dateTime),
        ("2024-01", XSD.gYearMonth),
        ("12:00:00Z", XSD.time),
    ):
        start = replace(START, datatype=datatype)
        result = check_temporal(
            {**DOCUMENT, "start": value}, intervals=[replace(INTERVAL, start=start)]
        )
        assert result.report.outcome == "unverified"
        assert not any(
            issue.code == "temporal.order_violation" for issue in result.report
        )
    assert check_temporal(
        {"end": DOCUMENT["end"]}, intervals=[replace(INTERVAL, open_start=True)]
    ).report.verified
    assert (
        check_temporal(
            {}, intervals=[replace(INTERVAL, open_start=True, open_end=True)]
        ).report.outcome
        == "unverified"
    )
    assert check_temporal({}, intervals=[INTERVAL]).report.outcome == "disagreement"
    date = TemporalField(("date",), XSD.date)
    mixed = check_temporal(
        {**DOCUMENT, "date": "2024-01-31Z"}, chronology=[TemporalOrder(date, END)]
    )
    assert mixed.report.outcome == "unverified"


def test_native_calendar_months_and_days_are_not_interchangeable() -> None:
    pytest.importorskip("elementpath")
    month = Literal("P1M", datatype=XSD.yearMonthDuration)
    days = Literal("P30D", datatype=XSD.dayTimeDuration)
    bound = replace(INTERVAL, minimum_duration=month, maximum_duration=month)
    assert check_temporal(DOCUMENT, intervals=[bound]).report.verified
    assert (
        check_temporal(
            DOCUMENT, intervals=[replace(INTERVAL, minimum_duration=days)]
        ).report.outcome
        == "disagreement"
    )
    for duration in (
        Literal("P1M", datatype=XSD.duration),
        Literal("-P1D", datatype=XSD.dayTimeDuration),
        Literal("PT0.0000001S", datatype=XSD.dayTimeDuration, normalize=False),
    ):
        assert (
            check_temporal(
                DOCUMENT, intervals=[replace(INTERVAL, maximum_duration=duration)]
            ).report.outcome
            == "unverified"
        )
    dates = TemporalInterval(
        replace(START, datatype=XSD.date),
        replace(END, datatype=XSD.date),
        minimum_duration=month,
        maximum_duration=month,
    )
    assert check_temporal(
        {"start": "2024-01-31Z", "end": "2024-02-29Z"}, intervals=[dates]
    ).report.verified
    with localcontext() as ambient:
        ambient.prec = 2
        ambient.Emax = 2
        ambient.traps[Inexact] = True
        before = ambient.copy()
        assert check_temporal(DOCUMENT, intervals=[bound]).report.verified
        assert getcontext().prec == before.prec and getcontext().Emax == before.Emax
        assert getcontext().flags == before.flags


def test_clock_is_supplied_only_for_explicit_relative_rules() -> None:
    pytest.importorskip("elementpath")
    rule = RelativeTemporal(END, "before")
    clock = Literal("2024-03-01T00:00:00Z", datatype=XSD.dateTime)
    result = check_temporal(DOCUMENT, relative=[rule], clock=clock)
    assert result.report.verified and str(result.clock) == str(clock)
    assert check_temporal(DOCUMENT, relative=[rule]).report.outcome == "unverified"
    assert (
        check_temporal(DOCUMENT, intervals=[INTERVAL], clock=clock).report.outcome
        == "input_error"
    )
    assert (
        check_temporal(
            DOCUMENT, relative=[rule], clock=Literal("now", lang="en")
        ).report.outcome
        == "unverified"
    )
    assert (
        check_temporal(
            DOCUMENT, relative=[replace(rule, relation="after")], clock=clock
        ).report.outcome
        == "disagreement"
    )


def test_budget_cancellation_paid_findings_and_optional_absence() -> None:
    cancel = threading.Event()
    cancel.set()
    assert (
        check_temporal(DOCUMENT, intervals=[INTERVAL], cancel=cancel).report.outcome
        == "exhausted"
    )
    assert (
        check_temporal(
            DOCUMENT, intervals=[INTERVAL], limits=Limits(max_work=1)
        ).report.outcome
        == "exhausted"
    )
    dense: dict[str, object] = {str(index): None for index in range(1000)}
    dense.update(DOCUMENT)
    bounded = Budget(Limits(max_total_bytes=4096))
    copied = check_temporal(dense, intervals=[INTERVAL], budget=bounded)
    assert copied.report.outcome == "exhausted" and copied.document is None
    assert bounded._counts.get("bytes", 0) <= 4096
    pytest.importorskip("elementpath")
    recovered = check_temporal(dense, intervals=[INTERVAL])
    assert recovered.report.verified and recovered.document == dense
    dense["0"] = "changed"
    assert recovered.document != dense
    assert check_temporal(DOCUMENT, intervals=[INTERVAL]).report.verified
    backwards = TemporalOrder(END, START)
    budget = Budget(Limits(max_issues=1))
    stopped = check_temporal(DOCUMENT, chronology=[backwards, backwards], budget=budget)
    assert stopped.report.outcome == "exhausted"
    paid = [
        issue
        for issue in budget._retained_issues.values()
        if issue.code == "temporal.order_violation"
    ]
    assert paid and all(
        any(issue is retained for retained in stopped.report) for issue in paid
    )
    repeated = Budget(Limits(max_resources=2))
    assert check_temporal(
        DOCUMENT, intervals=[INTERVAL], budget=repeated
    ).report.verified
    assert (
        check_temporal(DOCUMENT, intervals=[INTERVAL], budget=repeated).report.outcome
        == "exhausted"
    )
