"""Local SARIF preserves actual checking outcomes, locations and SHACL evidence."""

import json
from pathlib import Path
from typing import cast

import pytest
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import RDF, SH, XSD

from bijux_ontology.diagnostics.errors import (
    InputError,
    InputFailure,
    LimitExceeded,
    SourceSpan,
    attach_input_failure,
)
from bijux_ontology.diagnostics.reports import (
    AcceptancePolicy,
    Issue,
    IssueSelection,
    Location,
    Report,
)
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.interfaces.cli import main
from bijux_ontology.interfaces.exports.sarif import export_sarif, to_sarif
from bijux_ontology.rdf.value_comparison import compare_literal_values
from bijux_ontology.workflows.projects.checking import check_project
from bijux_ontology.workflows.projects.configuration import load_project

EX = Namespace("https://example.org/")


def _project(directory: Path) -> Path:
    """Use two real local competency queries against the same bad RDF record."""
    (directory / "data.ttl").write_text(
        '<urn:record> <urn:name> 7; <urn:age> "old" .', encoding="utf-8"
    )
    rules = []
    for name, datatype in (("name", "string"), ("age", "integer")):
        (directory / (name + ".rq")).write_text(
            "SELECT ?record ?value WHERE {?record <urn:"
            + name
            + "> ?value FILTER(datatype(?value) != <"
            + str(XSD)
            + datatype
            + ">)}",
            encoding="utf-8",
        )
        rules.append(
            {"name": name, "query_file": name + ".rq", "scope": {"kind": "default"}}
        )
    (directory / "rules.json").write_text(json.dumps(rules), encoding="utf-8")
    path = directory / "project.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "rdf": {"data": {"path": "data.ttl"}},
                "checks": [
                    {
                        "name": "constraints",
                        "kind": "query-rules",
                        "source": "data",
                        "rules": "rules.json",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _run(document: dict[str, object]) -> dict[str, object]:
    """Inspect the actual single native invocation rather than policy exit alone."""
    return cast(list[dict[str, object]], document["runs"])[0]


def _save(path: Path, document: dict[str, object]) -> None:
    """Retain public artifacts for separate official-schema qualification."""
    path.write_text(json.dumps(document), encoding="utf-8")


def test_actual_project_python_and_cli_preserve_two_findings_and_policy(
    tmp_path: Path,
) -> None:
    """A successful scan with two actual violations keeps native rejection semantics."""
    path = _project(tmp_path)
    report = check_project(load_project(path))
    assert report.complete and report.outcome == "disagreement"
    assert len(report.issues) == 2 and report.exit_code() == 1
    document = to_sarif(report, artifact_root=tmp_path)
    run = _run(document)
    results = cast(list[dict[str, object]], run["results"])
    assert len(results) == 2 and all(r["kind"] == "fail" for r in results)
    assert "name.rq" in json.dumps(results) and "age.rq" in json.dumps(results)
    assert (
        cast(list[dict[str, object]], run["invocations"])[0]["executionSuccessful"]
        is True
    )
    destination = tmp_path / "api.sarif"
    export_sarif(report, destination, artifact_root=tmp_path)
    assert json.loads(destination.read_text()) == document
    before = {
        p.name: p.read_bytes() for p in tmp_path.iterdir() if p.suffix != ".sarif"
    }
    output = tmp_path / "cli.sarif"
    assert (
        main(
            [
                "project",
                str(path),
                "--sarif",
                "--artifact-root",
                str(tmp_path),
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert json.loads(output.read_text()) == document
    assert {
        p.name: p.read_bytes() for p in tmp_path.iterdir() if p.name in before
    } == before
    assert main(["project", str(path), "--sarif", "--output", str(path)]) == 2
    assert path.read_bytes() == before[path.name]
    waived = AcceptancePolicy(
        exceptions=tuple(IssueSelection.for_issue(i) for i in report.issues)
    )
    permitted = to_sarif(report, policy=waived)
    invocation = cast(list[dict[str, object]], _run(permitted)["invocations"])[0]
    assert invocation["executionSuccessful"] is True and invocation["exitCode"] == 0
    _save(tmp_path / "waived.sarif", permitted)
    waived_output = tmp_path / "waived-cli.sarif"
    argv = ["project", str(path), "--sarif", "--output", str(waived_output)]
    for issue in report.issues:
        argv.extend(("--permit", issue.code, issue.location))
    assert main(argv) == 0
    waived_run = _run(json.loads(waived_output.read_text()))
    assert (
        cast(list[dict[str, object]], waived_run["invocations"])[0][
            "executionSuccessful"
        ]
        is True
    )
    assert len(cast(list[object], waived_run["results"])) == 2


def test_incomplete_comparison_input_and_empty_required_scope_never_become_pass(
    tmp_path: Path,
) -> None:
    """Open comparisons differ from failed invocations and empty required work."""
    comparison = compare_literal_values(
        Literal("a", datatype=EX.Custom),
        Literal("a", datatype=EX.Custom),
        regime="owl2-datatype-map",
    ).report
    assert not comparison.verified
    opened = to_sarif(comparison)
    run = _run(opened)
    assert any(
        r["kind"] == "open" and r["level"] == "none"
        for r in cast(list[dict[str, object]], run["results"])
    )
    assert (
        cast(list[dict[str, object]], run["invocations"])[0]["executionSuccessful"]
        is False
    )
    _save(tmp_path / "open.sarif", opened)
    path = _project(tmp_path)
    (tmp_path / "data.ttl").unlink()
    invalid = check_project(load_project(path))
    assert invalid.outcome == "input_error"
    for name, report in (("invalid", invalid), ("required-empty", Report("required"))):
        document = to_sarif(report)
        invocation = cast(list[dict[str, object]], _run(document)["invocations"])[0]
        assert invocation["executionSuccessful"] is False
        assert invocation["toolExecutionNotifications"]
        assert not any(
            r.get("kind") == "pass"
            for r in cast(list[dict[str, object]], _run(document)["results"])
        )
        _save(tmp_path / (name + ".sarif"), document)


def test_pointer_parser_region_and_iri_only_locations_are_not_conflated(
    tmp_path: Path,
) -> None:
    """Only parser evidence supplies coordinates; semantic IRIs stay typed."""
    failure = InputFailure(
        "https://example.org/document.json",
        span=SourceSpan(3, 7, 3, 9, column_unit="unicode_code_points"),
    )
    report = Report(
        "locations",
        3,
        (
            Issue(
                "source.value",
                "Bad {0} source [value](1)",
                details=Location("memory:document", ("a/b~", 2), term=str(EX.focus)),
            ),
            Issue(
                "input.invalid",
                "Bad JSON",
                kind="input_error",
                details=Location("configuration.json", failure=failure),
            ),
            Issue(
                "semantic.value",
                "Bad semantic value",
                details=Location(term=str(EX.focus), graph=str(EX.graph)),
            ),
        ),
    )
    document = to_sarif(report, artifact_root=Path("."))
    text = json.dumps(document)
    assert "/a~1b~0/2" in text and '"kind": "json-pointer"' in text
    assert '"startLine": 3' in text and '"startColumn": 7' in text
    results = cast(list[dict[str, object]], _run(document)["results"])
    assert (
        cast(dict[str, object], results[0]["message"])["text"]
        == r"Bad {{0}} source \[value\](1)"
    )
    assert (
        cast(
            dict[str, object],
            cast(dict[str, object], results[0]["properties"])["bijux"],
        )["message"]
        == "Bad {0} source [value](1)"
    )
    semantic = cast(list[dict[str, object]], results[1]["locations"])[0]
    assert "physicalLocation" not in semantic and "logicalLocations" in semantic
    _save(tmp_path / "locations.sarif", document)

    from bijux_ontology.json.values import decode_json
    from bijux_ontology.rdf.sources.loading import prepare_rdf
    from bijux_ontology.rdf.sources.types import RdfSource

    with pytest.raises(InputError) as malformed_json:
        decode_json('{"😀": }')
    json_error = attach_input_failure(malformed_json.value, document_iri="memory:json")
    failures = [("json", json_error)]
    turtle = '<urn:s> <urn:p> "😀" BAD .'
    with pytest.raises(InputError) as malformed_rdf:
        prepare_rdf(
            RdfSource(turtle.encode(), format="turtle", document_iri="memory:rdf")
        )
    failures.append(("turtle", malformed_rdf.value))
    xml = "<root>é😀<broken></root>"
    for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
        payload = xml.encode(encoding)
        if encoding != "utf-8":
            payload = (
                b"\xff\xfe" if encoding == "utf-16-le" else b"\xfe\xff"
            ) + payload
        with pytest.raises(InputError) as malformed_xml:
            prepare_rdf(RdfSource(payload, format="xml", document_iri="memory:xml"))
        failures.append((encoding, malformed_xml.value))
    for name, error in failures:
        assert error.failure is not None and error.failure.span is not None
        span = error.failure.span
        exported = to_sarif(
            Report(
                "parse",
                issues=(
                    Issue(
                        "input.invalid",
                        "Parser failure",
                        kind="input_error",
                        details=Location.from_error(error),
                    ),
                ),
            )
        )
        exported_run = _run(exported)
        assert exported_run["columnKind"] == "unicodeCodePoints"
        invocation = cast(list[dict[str, object]], exported_run["invocations"])[0]
        notification = cast(
            list[dict[str, object]], invocation["toolExecutionNotifications"]
        )[0]
        where = cast(list[dict[str, object]], notification["locations"])[0]
        region = cast(
            dict[str, object],
            cast(dict[str, object], where["physicalLocation"])["region"],
        )
        if name.startswith("utf-16"):
            assert (
                span.column_unit == "utf16_code_units" and "startColumn" not in region
            )
        else:
            assert (
                span.column_unit == "unicode_code_points"
                and region["startColumn"] == span.start_column
            )
        if name == "turtle":
            assert region["startColumn"] == turtle.index("BAD") + 1
        assert str(span.column_unit) in json.dumps(exported)
        _save(tmp_path / ("coordinates-" + name + ".sarif"), exported)


def test_actual_shacl_nested_results_and_deadline_keep_original_report_graph(
    tmp_path: Path,
) -> None:
    """Maintained execution retains nested RDF results and actual source pointers."""
    pytest.importorskip("pyshacl")
    pytest.importorskip("pyld")
    from bijux_ontology.jsonld.usages.checking import prepare_usages
    from bijux_ontology.shacl.checking import validate_shapes
    from bijux_ontology.shacl.findings import ShaclSource

    data = (
        Graph()
        .add((EX.record, EX.child, EX.childRecord))
        .add((EX.childRecord, EX.age, Literal("old")))
    )
    shapes = Graph()
    for triple in (
        (EX.Root, RDF.type, SH.NodeShape),
        (EX.Root, SH.targetNode, EX.record),
        (EX.Root, SH.property, EX.ChildField),
        (EX.ChildField, SH.path, EX.child),
        (EX.ChildField, SH.node, EX.Child),
        (EX.Child, SH.property, EX.AgeField),
        (EX.AgeField, SH.path, EX.age),
        (EX.AgeField, SH.datatype, XSD.integer),
    ):
        shapes.add(triple)
    document = {
        "@context": {
            "child": {"@id": str(EX.child), "@type": "@id"},
            "age": str(EX.age),
        },
        "@id": str(EX.record),
        "child": {"@id": str(EX.childRecord), "age": "old"},
    }
    prepared = prepare_usages(document)
    result = validate_shapes(
        data,
        shapes,
        source_usages=(ShaclSource(prepared, (("child", "age"),), "memory:record"),),
    )
    assert result.conforms is False
    before = frozenset(result.report_graph())
    exported = to_sarif(result)
    rows = cast(list[dict[str, object]], _run(exported)["results"])
    assert any(row.get("relatedLocations") for row in rows)
    assert "/child/age" in json.dumps(rows)
    assert '"datatype"' in json.dumps(rows) and "old" in json.dumps(rows)
    assert frozenset(result.report_graph()) == before
    assert (None, SH.conforms, Literal(False)) in result.report_graph()
    _save(tmp_path / "shacl.sarif", exported)
    result.report_graph().serialize(
        destination=tmp_path / "original-report.ttl", format="turtle"
    )
    timed = validate_shapes(data, shapes, timeout=0.000001)
    assert timed.report.outcome == "exhausted"
    exhausted = to_sarif(timed)
    invocation = cast(list[dict[str, object]], _run(exhausted)["invocations"])[0]
    assert (
        invocation["executionSuccessful"] is False
        and invocation["toolExecutionNotifications"]
    )
    _save(tmp_path / "timeout.sarif", exhausted)


def test_export_bounds_and_existing_files_fail_without_partial_artifacts(
    tmp_path: Path,
) -> None:
    """Conversion cannot overwrite inputs or publish a partial artifact."""
    report = Report("finding", 1, (Issue("bad", "x" * 1000),))
    path = tmp_path / "bounded.sarif"
    with pytest.raises(LimitExceeded):
        export_sarif(report, path, budget=Budget(Limits(max_bytes=100)))
    assert not path.exists()
    path = tmp_path / "existing.txt"
    path.write_text("original", encoding="utf-8")
    with pytest.raises(FileExistsError):
        export_sarif(Report("valid", 1), path)
    assert path.read_text() == "original"
    with pytest.raises(InputError):
        to_sarif(Report("valid", 1), artifact_root=cast(Path, "relative"))
    for args in (("--json", "--sarif"), ("--artifact-root", ".")):
        with pytest.raises(SystemExit) as error:
            main(["explain", "input.empty_scope", *args])
        assert error.value.code == 2
