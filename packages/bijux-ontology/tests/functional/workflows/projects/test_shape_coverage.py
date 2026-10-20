"""Portable projects require actual completed root targets, not selected filters."""

import json
from pathlib import Path

from bijux_ontology.workflows.projects.bundles import load_bundle
from bijux_ontology.workflows.projects.checking import check_project
from bijux_ontology.workflows.projects.configuration import ProjectConfig
from bijux_ontology.workflows.projects.parallel.checking import check_project_parallel


def test_required_targets_survive_serial_parallel_and_bundle_routes(
    tmp_path: Path,
) -> None:
    (tmp_path / "data.ttl").write_text('<urn:record> <urn:title> "Title".')
    shapes = tmp_path / "shapes.ttl"
    shapes.write_text(
        "@prefix sh:<http://www.w3.org/ns/shacl#>. <urn:Shape> a sh:NodeShape;"
        "sh:targetNode <urn:record>; sh:property [sh:path <urn:title>;sh:minCount 1]."
    )
    job: dict[str, object] = {
        "name": "records",
        "kind": "shacl",
        "document": "data",
        "shapes": "shapes",
        "coverage": {
            "minimum_focus": 1,
            "focus_nodes": ["urn:absent"],
            "pairs": [["urn:Shape", "urn:absent"]],
        },
    }
    data: dict[str, object] = {
        "version": 2,
        "rdf": {
            "data": {"path": "data.ttl", "iri": "urn:data"},
            "shapes": {"path": "shapes.ttl", "iri": "urn:shapes"},
        },
        "checks": [job],
    }
    (tmp_path / "project.json").write_text(json.dumps(data))
    config = ProjectConfig(data, directory=tmp_path)
    ordinary = check_project(config)
    assert not ordinary.verified
    codes = {issue.code for issue in ordinary.issues}
    assert {"shacl.coverage_missing", "shacl.coverage_pair_missing"} <= codes
    parallel = check_project_parallel(config, artifacts=tmp_path / "parallel")
    relocated = load_bundle(tmp_path).materialize(tmp_path / "portable")
    for report in (parallel.report, check_project(relocated)):
        assert not report.verified and {issue.code for issue in report.issues} == codes
    job["coverage"] = {"minimum_focus": 1, "pairs": [["urn:Shape", "urn:record"]]}
    assert check_project(ProjectConfig(data, directory=tmp_path)).verified
    # Successful meta-SHACL preflight cannot discharge an absent data target.
    job["preflight"] = True
    job["coverage"] = {"pairs": [["urn:Shape", "urn:absent"]]}
    assert not check_project(ProjectConfig(data, directory=tmp_path)).verified
    job.pop("preflight")
    job["coverage"] = {"pairs": [["urn:Shape", "urn:record"]]}
    shapes.write_text(shapes.read_text() + " <urn:Shape> sh:deactivated true.")
    assert not check_project(ProjectConfig(data, directory=tmp_path)).verified
    job["coverage"] = {}
    assert check_project(ProjectConfig(data, directory=tmp_path)).verified
    job["coverage"] = {"pairs": [["urn:Shape"]]}
    assert (
        check_project(ProjectConfig(data, directory=tmp_path)).outcome == "input_error"
    )
    job["coverage"] = {"minimum_focus": True}
    assert (
        check_project(ProjectConfig(data, directory=tmp_path)).outcome == "input_error"
    )
    job["coverage"] = {"ignored": 1}
    assert (
        check_project(ProjectConfig(data, directory=tmp_path)).outcome == "input_error"
    )
