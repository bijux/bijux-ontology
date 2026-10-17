"""The real console preserves pipeline, output and hard deadline contracts."""

import argparse
import io
import json
import os
import time
from pathlib import Path

import pytest

from bijux_ontology.interfaces.cli import main
from bijux_ontology.workflows.projects.configuration import ProjectConfig

EX = "https://example.org/"


def test_pipeline_coverage_and_output_alias_protection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ontology = tmp_path / "terms"
    original = f"<{EX}Record> a <http://www.w3.org/2002/07/owl#Class> ."
    ontology.write_text(original, encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps([EX + "Record"])))
    assert (
        main(
            [
                "vocabulary",
                str(ontology),
                "-",
                "--format",
                "turtle",
                "--coverage",
                "--namespace",
                EX,
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["verified"]
    inventory = tmp_path / "inventory.json"
    inventory.write_text("[]", encoding="utf-8")
    output = tmp_path / "output.json"
    output.hardlink_to(ontology)
    assert (
        main(
            [
                "vocabulary",
                str(ontology),
                str(inventory),
                "--format",
                "turtle",
                "--output",
                str(output),
                "--json",
            ]
        )
        == 2
    )
    assert ontology.read_text(encoding="utf-8") == original
    assert (
        json.loads(capsys.readouterr().out)["issues"][0]["code"] == "input.output_alias"
    )


def test_project_output_cannot_overwrite_a_configured_resource(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "terms.ttl"
    source.write_text("original", encoding="utf-8")
    config = tmp_path / "project.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "rdf": {"terms": {"path": "terms.ttl"}},
                "checks": [{"name": "terms", "kind": "ontology", "source": "terms"}],
            }
        ),
        encoding="utf-8",
    )
    assert main(["project", str(config), "--output", str(source), "--json"]) == 2
    assert source.read_text(encoding="utf-8") == "original"
    assert (
        json.loads(capsys.readouterr().out)["issues"][0]["code"] == "input.output_alias"
    )


@pytest.mark.skipif(
    not hasattr(os, "mkfifo"), reason="A real blocking FIFO requires POSIX"
)
def test_deadline_stops_real_blocked_input_and_retains_finished_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "terms.ttl").write_text(
        f"<{EX}Record> a <http://www.w3.org/2002/07/owl#Class> .", encoding="utf-8"
    )
    (tmp_path / "inventory.json").write_text(
        json.dumps([EX + "Missing"]), encoding="utf-8"
    )
    os.mkfifo(tmp_path / "blocked.json")
    config = {
        "version": 1,
        "rdf": {"terms": {"path": "terms.ttl"}},
        "checks": [
            {
                "name": "inventory",
                "kind": "vocabulary",
                "source": "terms",
                "inventory": "inventory.json",
            },
            {
                "name": "blocked",
                "kind": "context",
                "document": "blocked.json",
                "expectations": "absent.json",
            },
        ],
    }
    (tmp_path / "project.json").write_text(json.dumps(config), encoding="utf-8")
    started = time.monotonic()
    assert main(["project", "project.json", "--timeout", "2", "--json"]) == 2
    elapsed = time.monotonic() - started
    report = json.loads(capsys.readouterr().out)
    assert elapsed < 6
    assert report["outcome"] == "exhausted"
    assert not report["complete"] and not report["verified"]
    assert {issue["code"] for issue in report["issues"]} >= {
        "vocabulary.term_missing",
        "execution.deadline",
    }


def test_console_inspection_retains_typed_label_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "terms.ttl"
    source.write_text(
        f"<{EX}Record> a <http://www.w3.org/2002/07/owl#Class>; "
        '<http://www.w3.org/2000/01/rdf-schema#label> "Record"@en .',
        encoding="utf-8",
    )
    assert (
        main(
            [
                "inspect",
                str(source),
                "--search",
                "Record",
                "--language",
                "en",
                "--json",
                "--timeout",
                "5",
            ]
        )
        == 0
    )
    response = json.loads(capsys.readouterr().out)
    assert response["data"][0]["term"]["iri"] == EX + "Record"
    assert any(
        item["value"] == "Record" and item["language"] == "en"
        for item in response["data"]
    )
    assert response["complete"]


@pytest.mark.parametrize("timeout", [None, "5"])
def test_project_executes_the_configuration_snapshot_used_for_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    timeout: str | None,
) -> None:
    from bijux_ontology.interfaces.cli import application as cli
    from bijux_ontology.interfaces.cli.checking import project as prepare_project

    (tmp_path / "terms.ttl").write_text(
        f"<{EX}Record> a <http://www.w3.org/2002/07/owl#Class> .", encoding="utf-8"
    )
    (tmp_path / "inventory.json").write_text(
        json.dumps([EX + "Record"]), encoding="utf-8"
    )
    config = tmp_path / "project.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "rdf": {"terms": {"path": "terms.ttl"}},
                "checks": [
                    {
                        "name": "terms",
                        "kind": "vocabulary",
                        "source": "terms",
                        "inventory": "inventory.json",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def retain_and_replace(options: argparse.Namespace) -> ProjectConfig:
        prepared = prepare_project(options)
        config.write_text("not valid JSON", encoding="utf-8")
        return prepared

    monkeypatch.setattr(cli, "project", retain_and_replace)
    args = ["project", str(config), "--json"]
    if timeout is not None:
        args += ["--timeout", timeout]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["verified"]


def test_bounded_input_failure_never_overwrites_a_supplied_import(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "terms.ttl"
    source.write_text(
        "<https://example.org/ontology> a <http://www.w3.org/2002/07/owl#Ontology> .",
        encoding="utf-8",
    )
    imported = tmp_path / "import.ttl"
    imported.write_text("original", encoding="utf-8")
    resources = tmp_path / "sources.json"
    resources.write_text(json.dumps({EX + "import": "import.ttl"}), encoding="utf-8")
    assert (
        main(
            [
                "ontology",
                str(source),
                "--sources",
                str(resources),
                "--output",
                str(imported),
                "--timeout",
                "5",
                "--json",
            ]
        )
        == 2
    )
    assert imported.read_text(encoding="utf-8") == "original"
    assert json.loads(capsys.readouterr().out)["outcome"] == "input_error"


def test_policy_permissions_are_visible_exact_and_do_not_change_truth(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "terms.ttl"
    source.write_text(
        f"<{EX}Record> a <http://www.w3.org/2002/07/owl#Class> .", encoding="utf-8"
    )
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps([EX + "Missing"]), encoding="utf-8")
    args = ["vocabulary", str(source), str(inventory)]
    assert main(args + ["--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    issue = result["issues"][0]
    permitted = ["--permit", issue["code"], issue["location"]]
    assert main(args + permitted) == 0
    human = capsys.readouterr().out
    assert "accepted=True" in human and "verified=False" in human
    assert "permitted_diagnostics=1" in human and issue["code"] in human
    inventory.write_text(json.dumps([EX + "Missing", EX + "Other"]), encoding="utf-8")
    assert main(args + permitted + ["--display-limit", "0"]) == 1
    assert "2 additional diagnostics" in capsys.readouterr().out
