"""Actual parser failures retain content identity independently of project locations."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from rdflib import Graph

from bijux_ontology.diagnostics.errors import (
    InputError,
    InputFailure,
    LimitExceeded,
    ResourceError,
)
from bijux_ontology.diagnostics.reports import Issue, Location, Report
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    retain_report,
)
from bijux_ontology.interfaces.execution import WorkerExecutor
from bijux_ontology.jsonld.preparation import prepare_jsonld
from bijux_ontology.jsonld.resources import OfflineDocumentLoader
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.types import RdfSource
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.workflows.projects.checking import check_project
from bijux_ontology.workflows.projects.configuration import ProjectConfig

BAD = '@prefix ex: <https://example.org/> .\nex:s ex:p "unterminated'
GOOD = "<urn:record> a <http://www.w3.org/2002/07/owl#Class> ."


def test_native_parser_coordinates_and_explicit_content_location() -> None:
    assert len(prepare_rdf(RdfSource(GOOD, format="turtle")).graph()) == 1
    descriptor = RdfSource(
        BAD,
        format="turtle",
        document_iri="urn:broken",
        effective_url="https://example.org/retrieved",
        location="vendor/input.ttl",
        role="data",
    )
    for load in (prepare_rdf, prepare_source, load_vocabulary):
        with pytest.raises(ResourceError) as caught:
            load(descriptor)
        failure = caught.value.failure
        assert caught.value.location == "urn:broken" and failure is not None
        assert failure.document_iri == "urn:broken"
        assert failure.location == "vendor/input.ttl" and failure.role == "data"
        assert failure.span is not None
        assert failure.span.column_unit == "unicode_code_points"
        assert (failure.span.start_line, failure.span.start_column) == (2, 11)
        assert (failure.span.end_line, failure.span.end_column) == (2, 24)
        assert isinstance(caught.value.__cause__, SyntaxError)


def test_nested_import_failure_retains_actual_source_and_known_parent_chain() -> None:
    root = RdfSource(
        "<urn:root> <http://www.w3.org/2002/07/owl#imports> <urn:first> .",
        format="turtle",
        document_iri="urn:root",
    )
    first = RdfSource(
        "<urn:first> <http://www.w3.org/2002/07/owl#imports> <urn:second> .",
        format="turtle",
        document_iri="urn:actual-first",
    )
    second = RdfSource(
        BAD,
        format="turtle",
        document_iri="urn:actual-second",
        location="supplied/second.ttl",
    )
    with pytest.raises(ResourceError) as caught:
        load_vocabulary(root, imports={"urn:first": first, "urn:second": second})
    failure = caught.value.failure
    assert failure is not None and failure.document_iri == "urn:actual-second"
    assert failure.import_chain == ("urn:root", "urn:actual-first")
    assert failure.role == "import" and failure.location == "supplied/second.ttl"
    assert failure.span is not None and failure.span.start_column == 11


def test_json_decoder_span_and_spanless_native_guard_stay_distinct() -> None:
    with pytest.raises(InputError) as caught:
        prepare_source(
            RdfSource(
                '{"value":\n]',
                format="json-ld",
                document_iri="urn:bad-json",
                role="data",
            )
        )
    failure = caught.value.failure
    assert failure is not None and failure.span is not None
    assert (failure.span.start_line, failure.span.start_column) == (2, 1)
    assert failure.span.end_line is None and failure.span.end_column is None
    assert failure.span.column_unit == "unicode_code_points"
    with pytest.raises(InputError) as unsupported:
        prepare_source(
            RdfSource(
                "<!DOCTYPE rdf><rdf/>",
                format="xml",
                document_iri="urn:xml",
                role="shape",
            )
        )
    failure = unsupported.value.failure
    assert failure is not None and failure.document_iri == "urn:xml"
    assert failure.role == "shape" and failure.span is None
    assert isinstance(prepare_source(Graph()).graph(), Graph)


def test_project_and_cli_keep_content_failure_and_successful_sibling(
    tmp_path: Path,
) -> None:
    (tmp_path / "good.ttl").write_text(GOOD)
    (tmp_path / "bad.ttl").write_text(BAD)
    (tmp_path / "inventory.json").write_text(json.dumps(["urn:record"]))
    config = {
        "version": 1,
        "rdf": {
            "good": {"path": "good.ttl", "iri": "urn:good"},
            "bad": {"path": "bad.ttl", "iri": "urn:broken"},
        },
        "checks": [
            {
                "name": "bad",
                "kind": "vocabulary",
                "source": "bad",
                "inventory": "inventory.json",
            },
            {
                "name": "good",
                "kind": "vocabulary",
                "source": "good",
                "inventory": "inventory.json",
            },
        ],
    }
    report = check_project(ProjectConfig(config, directory=tmp_path))
    assert len(report.children) == 2 and report.children[1].verified
    issue = report.children[0].issues[0]
    assert issue.location != "urn:broken" and issue.details is not None
    assert issue.details.source == "urn:broken" and issue.details.failure is not None
    assert issue.details.failure.span is not None
    assert issue.details.failure.span.start_column == 11
    path = tmp_path / "project.json"
    path.write_text(json.dumps(config))
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-m",
            "bijux_ontology",
            "project",
            str(path),
            "--json",
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2, result.stderr
    output = json.loads(result.stdout)
    failures = [
        item
        for item in output["issues"]
        if item.get("details", {}).get("input_failure")
    ]
    assert failures[0]["details"]["input_failure"]["span"]["start_column"] == 11
    isolated = WorkerExecutor(artifacts=tmp_path / "workers").run(
        "project", {"configuration": config, "directory": str(tmp_path)}
    )
    assert isolated.report == report
    standalone = WorkerExecutor(artifacts=tmp_path / "standalone").run(
        "vocabulary", {"source": BAD, "format": "turtle", "base": "urn:standalone"}
    )
    assert standalone.report.outcome == "input_error"
    details = standalone.report.issues[0].details
    assert details is not None and details.failure is not None
    assert details.failure.document_iri == "urn:standalone"
    assert details.failure.span is not None and details.failure.span.start_column == 11
    assert details.failure.span.column_unit == "unicode_code_points"


def test_adopting_large_failure_metadata_retains_prior_findings_before_exhaustion() -> (
    None
):
    prior = Issue("test.known_failure", "Known disagreement")
    failure = InputFailure("urn:input", location="😀" * 1000)
    report = Report(
        "input",
        1,
        (
            prior,
            Issue(
                "input.invalid",
                "Malformed content",
                details=Location(source="urn:input", failure=failure),
            ),
        ),
    )
    operation = Budget(Limits(max_bytes=64))
    issues = IssueCollector(operation)
    with pytest.raises(LimitExceeded):
        retain_report(report, operation, issues)
    assert issues == [prior]
    issues.exhausted("Input metadata exceeds the operation allowance")
    assert Report("adopted", 1, tuple(issues)).outcome == "exhausted"


def test_jsonld_metadata_bounds_and_actual_loader_document_identity(
    tmp_path: Path,
) -> None:
    large = prepare_jsonld(
        {"@id": []},
        document_iri="urn:" + "x" * 100000,
        limits=Limits(max_bytes=1000, max_total_bytes=10000),
    )
    assert large.source is None and large.report.outcome == "exhausted"
    assert all(
        issue.details is None and not issue.location for issue in large.report.issues
    )
    if importlib.util.find_spec("pyld") is None:
        return
    bad = tmp_path / "context.json"
    bad.write_text('{"@context":\n]')
    loader = OfflineDocumentLoader(
        {"urn:broken-context": bad, "urn:broken-document": bad}
    )
    for document, expected in (
        ({"@context": "urn:broken-context", "@id": "urn:record"}, "urn:broken-context"),
        ("urn:broken-document", "urn:broken-document"),
    ):
        failed = prepare_jsonld(
            document, document_iri="urn:parent", document_loader=loader
        )
        assert failed.source is None and failed.report.outcome == "input_error"
        details = failed.report.issues[0].details
        assert details is not None and details.source == expected
        assert details.failure is not None and details.failure.document_iri == expected
        assert details.failure.location == str(bad) and details.failure.role == "input"
        assert details.failure.span is not None
        assert (details.failure.span.start_line, details.failure.span.start_column) == (
            2,
            1,
        )
