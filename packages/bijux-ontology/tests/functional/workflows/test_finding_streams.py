"""Actual prepared-record finding export retains raw totals and incomplete delivery."""

import io
import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from threading import Event

import pytest
from rdflib import Graph, Namespace
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.declarations import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.models.emission.types import ModelUsage
from bijux_ontology.models.preparation import PreparedModels
from bijux_ontology.reports import Report, combine_reports
from bijux_ontology.workflows.finding_streams import (
    CallbackFindingSink,
    FindingEvent,
    FindingStreamLimits,
    FindingStreamSummary,
    JsonlFindingSink,
    RecordResultEvent,
    StreamEvent,
    stream_model_findings,
)

EX = Namespace("urn:stream:")
USAGES = (ModelUsage("node"),)


def prepared(*, max_issues: int = 3) -> PreparedModels:
    """Use real ordinary check_models preparation and recursive child declarations."""
    model = ModelDeclaration(
        "node",
        (str(EX.Node),),
        (
            FieldDeclaration(
                "label",
                str(EX.label),
                (ValueDeclaration("literal", datatype=str(XSD.string)),),
                emission="guaranteed",
            ),
            FieldDeclaration(
                "child",
                str(EX.child),
                (ValueDeclaration("object", model="node"),),
                emission="optional",
            ),
        ),
    )
    graph = Graph().add((EX.Node, RDF.type, OWL.Class))
    for predicate, kind, range_ in (
        (EX.label, OWL.DatatypeProperty, XSD.string),
        (EX.child, OWL.ObjectProperty, EX.Node),
    ):
        graph.add((predicate, RDF.type, kind)).add(
            (predicate, RDFS.domain, EX.Node)
        ).add((predicate, RDFS.range, range_))
    result = PreparedModels((model,), graph, limits=Limits(max_issues=max_issues))
    assert result.declaration_report.verified
    return result


def document(index: int, *, invalid: bool = True) -> dict[str, object]:
    """Keep each document's native child source paths and RDF identities distinct."""
    return {
        "@context": {"@vocab": str(EX)},
        "@id": str(EX[f"root{index}"]),
        "@type": "Node",
        "label": "ok",
        "child": {
            "@id": str(EX[f"child{index}"]),
            "@type": "Node",
            "label": 7 if invalid else "child",
        },
    }


def test_large_real_failure_run_has_full_jsonl_and_late_source_location(
    tmp_path: Path,
) -> None:
    """Hundreds of actual failures finish while only two samples remain in memory."""
    plan = prepared()
    path = tmp_path / "findings.jsonl"
    with path.open("xb") as output:
        result = stream_model_findings(
            plan,
            (document(i) for i in range(300)),
            USAGES,
            sink=JsonlFindingSink(output),
            stream_limits=FindingStreamLimits(max_retained=2),
        )
    assert result.semantic_complete and result.export_complete
    assert result.raw_outcome == "disagreement" and not result.verified
    assert (
        result.records_evaluated
        == result.records_complete
        == result.failing_records
        == 300
    )
    assert result.finding_count == result.emitted_findings == 300
    assert dict(result.finding_counts) == {"finding": 300}
    assert result.finding_counts_complete
    assert len(result.samples) == 2 and result.report is None
    frames = [json.loads(line) for line in path.read_text().splitlines()]
    late = [
        frame
        for frame in frames
        if frame["type"] == "finding" and frame["record_index"] == 299
    ]
    assert len(late) == 1 and late[0]["issue"]["location"] == "/child/label[0]"
    assert late[0]["check_paths"] == [[], [1]]
    assert frames[-1]["type"] == "summary" and frames[-1]["finding_count"] == 300
    assert frames[-1]["selected_frames_finished"] is True
    assert "export_complete" not in frames[-1]
    assert result.output_bytes == path.stat().st_size
    legacy = list(plan.check_many((document(i) for i in range(20)), USAGES))
    assert any(report.outcome == "exhausted" for report in legacy)


def test_small_report_agreement_and_sample_filters_do_not_change_truth() -> None:
    """Actual small Reports and their original scope tree remain available intact."""
    plan = prepared()
    docs = [
        document(i, invalid=invalid) for i, invalid in enumerate((False, True, False))
    ]
    expected = combine_reports("expected", *(plan.check(item, USAGES) for item in docs))
    events: list[StreamEvent] = []
    result = stream_model_findings(
        plan, docs, USAGES, sink=CallbackFindingSink(events.append)
    )
    assert result.report is not None
    assert result.report.outcome == expected.outcome == result.raw_outcome
    assert (result.checked, result.requested) == (expected.checked, expected.requested)
    assert (
        result.report.complete and result.semantic_complete and result.export_complete
    )
    pending = [item for item in events if isinstance(item, FindingStreamSummary)]
    assert len(pending) == 1 and not pending[0].export_complete
    records = [item for item in events if isinstance(item, RecordResultEvent)]
    assert len(records) == 3 and {item.path for item in records[0].checks} == {
        (),
        (0,),
        (1,),
    }
    selected = FindingStreamLimits(max_records=1)
    binding = ModelUsage("node")

    def mutate(event: StreamEvent) -> None:
        """Caller changes to its original objects cannot alter the admitted plan."""
        object.__setattr__(selected, "max_records", 1000)
        object.__setattr__(binding, "model", "unknown")

    owned = stream_model_findings(
        plan, docs, (binding,), sink=CallbackFindingSink(mutate), stream_limits=selected
    )
    assert owned.records_evaluated == 1 and not owned.export_complete
    assert owned.raw_outcome == "unverified"
    for retained, codes in ((0, ()), (20, ("not.displayed",))):
        filtered = stream_model_findings(
            plan,
            docs,
            USAGES,
            sink=JsonlFindingSink(io.BytesIO()),
            stream_limits=FindingStreamLimits(max_retained=retained),
            sample_codes=codes,
        )
        assert not filtered.samples
        assert (
            filtered.finding_count == filtered.emitted_findings == result.finding_count
        )
        assert filtered.raw_outcome == result.raw_outcome and filtered.semantic_complete

    missing = document(0)
    del missing["label"]
    selected_code = "emission.value_mismatch"
    selected_events: list[StreamEvent] = []
    sampled = stream_model_findings(
        plan,
        (missing,),
        USAGES,
        sink=CallbackFindingSink(selected_events.append),
        stream_limits=FindingStreamLimits(max_retained=1),
        sample_codes=(selected_code,),
    )
    assert sampled.semantic_complete and sampled.export_complete
    assert sampled.finding_count == sampled.emitted_findings == 2
    assert len(sampled.samples) == 1
    assert sampled.samples[0].issue.code == selected_code
    assert sampled.samples[0].issue.location == "/child/label[0]"
    assert sampled.samples[0].finding_index == 1
    exported = next(
        event
        for event in selected_events
        if isinstance(event, FindingEvent) and event.issue.code == selected_code
    )
    assert sampled.samples[0] is exported
    assert sampled.samples[0].check_paths == ((), (1,))


def test_broken_sink_and_callback_cancel_preserve_known_negative() -> None:
    """Output consumers never supply truth, and no next record is prefetched."""
    plan = prepared()
    pulled: list[int] = []

    def documents() -> Iterator[object]:
        """Record each actual admitted producer pull."""
        for index in range(10):
            pulled.append(index)
            yield document(index)

    def broken(event: StreamEvent) -> None:
        """Fail after two acknowledged real finding frames."""
        if isinstance(event, FindingEvent) and event.record_index == 2:
            raise OSError("caller sink failed")

    failed = stream_model_findings(
        plan, documents(), USAGES, sink=CallbackFindingSink(broken)
    )
    assert pulled == [0, 1, 2]
    assert failed.failing_records == failed.finding_count == 3
    assert failed.emitted_findings == 2 and not failed.export_complete
    assert failed.raw_outcome == "disagreement" and failed.failure is not None
    assert not failed.verified and failed.report is None
    cancel = Event()
    captured: list[FindingEvent] = []

    def cancelled(event: StreamEvent) -> None:
        """Cancel only after the first real finding was delivered."""
        if isinstance(event, FindingEvent):
            captured.append(event)
            cancel.set()

    stopped = stream_model_findings(
        plan,
        (document(i) for i in range(10)),
        USAGES,
        sink=CallbackFindingSink(cancelled),
        cancel=cancel,
    )
    assert stopped.raw_outcome == "disagreement" and not stopped.export_complete
    assert stopped.emitted_findings == stopped.finding_count == 1
    assert stopped.samples[0].issue is captured[0].issue
    assert stopped.failure and stopped.failure.kind == "exhausted"


def test_output_caps_short_write_and_per_record_limits_are_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary per-record exhaustion and incomplete output never become success."""
    plan = prepared()
    for selected in (
        FindingStreamLimits(max_output_bytes=100),
        FindingStreamLimits(max_emitted_findings=1),
        FindingStreamLimits(max_records=1),
    ):
        result = stream_model_findings(
            plan,
            (document(i) for i in range(3)),
            USAGES,
            sink=JsonlFindingSink(io.BytesIO()),
            stream_limits=selected,
        )
        assert not result.export_complete and not result.verified
        assert result.finding_count >= 1 and result.raw_outcome == "disagreement"
        assert result.failure and result.failure.kind == "exhausted"

    class ShortOutput:
        """An actual binary sink acknowledges only a partial frame."""

        def __init__(self) -> None:
            """Own an ordinary binary buffer for the actual partial write."""
            self.output = io.BytesIO()

        def write(self, payload: bytes, /) -> int:
            """Persist an actual prefix and return its real short count."""
            return self.output.write(payload[:-1])

        def flush(self) -> None:
            """Use the ordinary binary buffer's flush."""
            self.output.flush()

    short = ShortOutput()
    failed = stream_model_findings(
        plan, (document(0),), USAGES, sink=JsonlFindingSink(short)
    )
    assert (
        short.output.getvalue() and not failed.export_complete and not failed.verified
    )

    class FlushOutput(io.BytesIO):
        """A binary consumer can fail while finishing acknowledged frames."""

        def flush(self) -> None:
            """Expose an actual flush failure to the local final summary."""
            raise OSError("caller flush failed")

    flush_output = FlushOutput()
    flushed = stream_model_findings(
        plan,
        (document(0, invalid=False),),
        USAGES,
        sink=JsonlFindingSink(flush_output),
    )
    assert flushed.semantic_complete and flushed.raw_outcome == "agreement"
    assert not flushed.export_complete and not flushed.verified
    assert all(
        json.loads(line)["type"] != "summary"
        for line in flush_output.getvalue().splitlines()
    )

    class FooterFlushOutput(io.BytesIO):
        """Fail only while finishing the footer after selected frames succeeded."""

        def __init__(self) -> None:
            """Count actual flush calls on this binary output."""
            super().__init__()
            self.finishes = 0

        def flush(self) -> None:
            """The selected frames finish, then the summary flush fails."""
            self.finishes += 1
            if self.finishes == 2:
                raise OSError("summary flush failed")
            super().flush()

    footer_output = FooterFlushOutput()
    footer_failure = stream_model_findings(
        plan,
        (document(0, invalid=False),),
        USAGES,
        sink=JsonlFindingSink(footer_output),
    )
    footer = json.loads(footer_output.getvalue().splitlines()[-1])
    assert footer["selected_frames_finished"] is True
    assert "export_complete" not in footer
    assert not footer_failure.export_complete and not footer_failure.verified
    limited = prepared(max_issues=1)
    bad = document(0)
    bad["label"] = 7
    result = stream_model_findings(
        limited, (bad,), USAGES, sink=JsonlFindingSink(io.BytesIO())
    )
    assert not result.semantic_complete and not result.export_complete
    assert result.raw_outcome == "exhausted" and result.failing_records == 1
    assert result.samples and any(
        item.issue.kind == "finding" for item in result.samples
    )
    enclosing = stream_model_findings(
        plan,
        (bad,),
        USAGES,
        sink=JsonlFindingSink(io.BytesIO()),
        limits=Limits(max_issues=1),
    )
    assert enclosing.raw_outcome == "exhausted" and not enclosing.semantic_complete
    interrupted = Budget()
    check = PreparedModels.check

    def stop_after_record(
        self: PreparedModels,
        supplied: object,
        usages: Sequence[ModelUsage],
        *,
        budget: Budget | None = None,
    ) -> Report:
        report = check(self, supplied, usages, budget=budget)
        interrupted.consume("work", interrupted.remaining("work"))
        return report

    with monkeypatch.context() as selected:
        selected.setattr(PreparedModels, "check", stop_after_record)
        partial = stream_model_findings(
            plan,
            (document(0),),
            USAGES,
            sink=JsonlFindingSink(io.BytesIO()),
            budget=interrupted,
        )
    assert partial.finding_count > 0 and not partial.finding_counts_complete
    assert not partial.semantic_complete and not partial.verified
    budget = Budget(Limits(max_work=1))
    result = stream_model_findings(
        plan, (document(0),), USAGES, sink=JsonlFindingSink(io.BytesIO()), budget=budget
    )
    assert not result.verified and budget.remaining("work") >= 0
    cancel = Event()
    cancel.set()
    budget = Budget()
    result = stream_model_findings(
        plan,
        (document(0),),
        USAGES,
        sink=JsonlFindingSink(io.BytesIO()),
        budget=budget,
        cancel=cancel,
    )
    assert result.failure and not result.verified and budget._counts == {}
