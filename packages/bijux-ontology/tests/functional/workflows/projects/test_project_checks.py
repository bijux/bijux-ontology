"""Real project inputs retain cwd independence, reuse and partial failures."""

import json
from pathlib import Path
from typing import cast

import pytest

from bijux_ontology.errors import InputError
from bijux_ontology.workflows.projects.checking import PreparedChecks, check_project
from bijux_ontology.workflows.projects.configuration import ProjectConfig, load_project

EX = "https://example.org/"


def test_local_project_reuses_owned_sources_and_retains_independent_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "vocabulary.ttl").write_text(
        f"<{EX}Record> a <http://www.w3.org/2002/07/owl#Class> .", encoding="utf-8"
    )
    (tmp_path / "inventory.json").write_text(
        json.dumps([EX + "Missing"]), encoding="utf-8"
    )
    (tmp_path / "document.json").write_text(
        json.dumps({"@context": EX + "context", "title": "Example"}), encoding="utf-8"
    )
    (tmp_path / "context.json").write_text(
        json.dumps({"@context": {"title": EX + "title"}}), encoding="utf-8"
    )
    (tmp_path / "expectations.json").write_text(
        json.dumps([{"path": ["title"], "predicate": EX + "title"}]), encoding="utf-8"
    )
    data = {
        "version": 1,
        "rdf": {"terms": {"path": "vocabulary.ttl"}},
        "contexts": {EX + "context": "context.json"},
        "checks": [
            {
                "name": "inventory",
                "kind": "vocabulary",
                "source": "terms",
                "inventory": "inventory.json",
            },
            {
                "name": "mapping",
                "kind": "context",
                "document": "document.json",
                "expectations": "expectations.json",
            },
            {
                "name": "missing",
                "kind": "models",
                "source": "terms",
                "declarations": "absent.json",
            },
        ],
    }
    path = tmp_path / "project.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.chdir(tmp_path.parent)
    config = load_project(path)
    prepared = PreparedChecks(config)
    first = prepared.vocabulary("terms")
    (tmp_path / "vocabulary.ttl").write_text("invalid RDF", encoding="utf-8")
    assert prepared.vocabulary("terms") is first
    report = check_project(prepared)
    assert report.exit_code() == 2
    assert [child.scope for child in report.children] == [
        "inventory",
        "mapping",
        "missing",
    ]
    assert report.children[1].verified
    assert any(issue.code == "vocabulary.term_missing" for issue in report.issues)
    assert any(
        issue.kind == "input_error" and "/checks/2" in issue.location
        for issue in report.issues
    )
    explained = config.explain()
    assert str(tmp_path / "vocabulary.ttl") in str(explained)
    cast(list[object], data["checks"]).clear()
    assert len(config.checks()) == 3
    with pytest.raises(InputError, match="must be unique"):
        ProjectConfig(
            {
                "version": 1,
                "rdf": {"a": {"path": "a", "iri": EX}, "b": {"path": "b", "iri": EX}},
                "checks": [{"name": "one", "kind": "context"}],
            },
            directory=tmp_path,
        )


def test_project_never_ignores_unknown_policy_or_configuration(
    tmp_path: Path,
) -> None:
    with pytest.raises(InputError, match="Unknown keys"):
        ProjectConfig({"version": 1, "unknown": []}, directory=tmp_path)
    config = ProjectConfig(
        {"version": 1, "checks": [{"name": "one", "kind": "context", "typo": True}]},
        directory=tmp_path,
    )
    report = check_project(config)
    assert report.exit_code() == 2
    assert "typo" in report.issues[0].message
    with pytest.raises(InputError, match="positive integer"):
        ProjectConfig(
            {
                "version": 1,
                "limits": {"max_work": True},
                "checks": [{"name": "one", "kind": "context"}],
            },
            directory=tmp_path,
        )


def test_project_composes_data_shapes_and_context_batch_without_conflating_scopes(
    tmp_path: Path,
) -> None:
    (tmp_path / "terms.ttl").write_text(
        "@prefix ex: <https://example.org/> . "
        "@prefix owl: <http://www.w3.org/2002/07/owl#> . "
        "ex:Record a owl:Class . ex:title a owl:DatatypeProperty .",
        encoding="utf-8",
    )
    (tmp_path / "data.ttl").write_text(
        f'<{EX}record> a <{EX}Record>; <{EX}title> "Example" .', encoding="utf-8"
    )
    (tmp_path / "shapes.ttl").write_text(
        "@prefix sh: <http://www.w3.org/ns/shacl#> . "
        "@prefix ex: <https://example.org/> . "
        "ex:Shape a sh:NodeShape; sh:targetClass ex:Record; "
        "sh:property [sh:path ex:title; sh:minCount 1] .",
        encoding="utf-8",
    )
    (tmp_path / "document.json").write_text(
        json.dumps({"@context": {"title": EX + "title"}, "title": "Example"}),
        encoding="utf-8",
    )
    (tmp_path / "expectations.json").write_text(
        json.dumps([{"path": ["title"], "predicate": EX + "title"}]), encoding="utf-8"
    )
    config = ProjectConfig(
        {
            "version": 1,
            "rdf": {
                name: {"path": name + ".ttl"} for name in ("terms", "data", "shapes")
            },
            "checks": [
                {
                    "name": "usage",
                    "kind": "data",
                    "source": "terms",
                    "document": "data",
                },
                {
                    "name": "shapes",
                    "kind": "shacl",
                    "document": "data",
                    "shapes": "shapes",
                },
                {
                    "name": "documents",
                    "kind": "context",
                    "documents": [
                        {
                            "document": "document.json",
                            "expectations": "expectations.json",
                        },
                        {
                            "document": "absent.json",
                            "expectations": "expectations.json",
                        },
                    ],
                },
            ],
        },
        directory=tmp_path,
    )
    result = check_project(config)
    assert [child.verified for child in result.children] == [True, True, False]
    assert result.children[2].children[0].verified
    assert result.children[2].children[1].outcome == "input_error"
    assert result.exit_code() == 2
    assert "shacl-core" in result.families and "vocabulary-usage" in result.families


def test_project_preparation_and_result_limits_preserve_prior_findings(
    tmp_path: Path,
) -> None:
    (tmp_path / "terms.ttl").write_text(
        f"<{EX}Record> a <http://www.w3.org/2002/07/owl#Class> .", encoding="utf-8"
    )
    (tmp_path / "other.ttl").write_text(
        f"<{EX}Other> a <http://www.w3.org/2002/07/owl#Class> .", encoding="utf-8"
    )
    (tmp_path / "inventory.json").write_text(
        json.dumps([EX + "Missing"]), encoding="utf-8"
    )
    data = {
        "version": 1,
        "limits": {"max_resources": 1},
        "rdf": {name: {"path": name + ".ttl"} for name in ("terms", "other")},
        "checks": [
            {
                "name": "first",
                "kind": "vocabulary",
                "source": "terms",
                "inventory": "inventory.json",
            },
            {
                "name": "second",
                "kind": "vocabulary",
                "source": "other",
                "inventory": "inventory.json",
            },
            {
                "name": "third",
                "kind": "vocabulary",
                "source": "terms",
                "inventory": "inventory.json",
            },
        ],
    }
    result = check_project(ProjectConfig(data, directory=tmp_path))
    assert result.outcome == "exhausted" and not result.verified
    assert any(issue.code == "vocabulary.term_missing" for issue in result.issues)
    assert any("max_resources" in issue.message for issue in result.issues)
    assert result.issues[-1].expected == "third"
    assert result.requested is not None and result.requested > result.checked
    data["limits"] = {"max_issues": 1}
    result = check_project(ProjectConfig(data, directory=tmp_path))
    assert result.outcome == "exhausted"
    assert len(result.issues) == 2
    assert result.issues[0].code == "vocabulary.term_missing"


def test_project_graph_selections_keep_views_and_job_failures_independent(
    tmp_path: Path,
) -> None:
    (tmp_path / "terms.trig").write_text(
        "@prefix ex: <https://example.org/> . "
        "@prefix owl: <http://www.w3.org/2002/07/owl#> . "
        "ex:first { ex:Record a owl:Class . } "
        "ex:second { ex:Other a owl:Class . }",
        encoding="utf-8",
    )
    for name, terms in (
        ("first", [EX + "Record"]),
        ("second", [EX + "Other"]),
        ("all", [EX + "Record", EX + "Other"]),
    ):
        (tmp_path / f"{name}.json").write_text(json.dumps(terms), encoding="utf-8")
    common = {
        "kind": "vocabulary",
        "source": "terms",
        "coverage": True,
        "namespaces": [EX],
    }
    config = ProjectConfig(
        {
            "version": 1,
            "rdf": {"terms": {"path": "terms.trig"}},
            "checks": [
                common
                | {
                    "name": "first",
                    "graph_name": EX + "first",
                    "inventory": "first.json",
                },
                common | {"name": "invalid", "union": "false", "inventory": "all.json"},
                common
                | {
                    "name": "second",
                    "graph_name": EX + "second",
                    "inventory": "second.json",
                },
                common | {"name": "union", "union": True, "inventory": "all.json"},
            ],
        },
        directory=tmp_path,
    )
    result = check_project(config)
    assert [child.scope for child in result.children] == [
        "first",
        "invalid",
        "second",
        "union",
    ]
    assert [child.verified for child in result.children] == [True, False, True, True]
    assert result.children[1].outcome == "input_error"
    assert "Expected a boolean" in result.children[1].issues[0].message
    assert result.exit_code() == 2


def test_project_resource_selections_apply_to_imports_and_lookup_snapshots(
    tmp_path: Path,
) -> None:
    (tmp_path / "root.ttl").write_text(
        f"<{EX}Root> <http://www.w3.org/2002/07/owl#imports> <{EX}Document>."
    )
    (tmp_path / "import.trig").write_text(
        "@prefix ex:<https://example.org/>. "
        "@prefix owl:<http://www.w3.org/2002/07/owl#>. "
        "ex:ontology { ex:Imported a owl:Class. } "
        "ex:application { ex:Poison a owl:Class; owl:imports ex:Unsupplied. }"
    )
    (tmp_path / "lookup.trig").write_text(
        "@prefix ex:<https://example.org/>. "
        "@prefix owl:<http://www.w3.org/2002/07/owl#>. "
        "ex:Lookup a owl:Class. ex:application { ex:Hidden a owl:Class. }"
    )
    (tmp_path / "inventory.json").write_text(
        json.dumps([EX + "Imported", EX + "Lookup"])
    )
    config = ProjectConfig(
        {
            "version": 1,
            "rdf": {
                "root": {"path": "root.ttl"},
                "import": {
                    "path": "import.trig",
                    "iri": EX + "Document",
                    "graph_name": EX + "ontology",
                },
                "lookup": {"path": "lookup.trig", "graph_name": "@default"},
            },
            "checks": [
                {
                    "name": "selected",
                    "kind": "vocabulary",
                    "source": "root",
                    "lookup": ["lookup"],
                    "inventory": "inventory.json",
                }
            ],
        },
        directory=tmp_path,
    )
    assert config.explain()["rdf"] == {
        name: {
            "path": str(item.content),
            "format": None,
            "iri": item.document_iri,
            "effective_url": None,
            "graph_name": item.graph_name,
            "union": False,
        }
        for name, item in config.rdf.items()
    }
    assert check_project(config).verified
    (tmp_path / "inventory.json").write_text(json.dumps([EX + "Poison", EX + "Hidden"]))
    report = check_project(config)
    assert {issue.code for issue in report.issues} == {"vocabulary.term_missing"}
    assert len(report.issues) == 2
