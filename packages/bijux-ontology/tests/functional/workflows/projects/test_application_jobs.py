"""Ordinary project files compose real emission, corpus and application checks."""

import json
from pathlib import Path
from typing import cast

import pytest
from rdflib import URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.interfaces.cli import main
from bijux_ontology.limits import Limits
from bijux_ontology.workflows.projects.building import (
    ApplicationInput,
    CorpusDocumentInput,
    ProjectBuilder,
)
from bijux_ontology.workflows.projects.bundles import load_bundle
from bijux_ontology.workflows.projects.checking import PreparedChecks, check_project
from bijux_ontology.workflows.projects.configuration import ProjectConfig, load_project
from bijux_ontology.workflows.upgrades.corpus import CorpusBranch, CorpusWitness

EX = "https://example.invalid/project/"


def _write(path: Path, value: object) -> None:
    """Write actual consumer JSON, including explicitly supplied null values."""
    path.write_text(json.dumps(value), encoding="utf-8")


def _project(directory: Path) -> ProjectConfig:
    """Author independent declarations and actual samples with ordinary files."""
    vocabulary = f"""@prefix owl:<http://www.w3.org/2002/07/owl#>.
      @prefix rdfs:<http://www.w3.org/2000/01/rdf-schema#>.
      <{EX}Record> a owl:Class.
      <{EX}label> a owl:DatatypeProperty;rdfs:domain <{EX}Record>;
        rdfs:range <http://www.w3.org/2001/XMLSchema#string>."""
    for name in ("old.ttl", "new.ttl"):
        (directory / name).write_text(vocabulary, encoding="utf-8")
    model = {
        "identifier": "Record",
        "types": [EX + "Record"],
        "fields": [
            {
                "name": "label",
                "predicate": EX + "label",
                "values": [
                    {
                        "kind": "literal",
                        "datatype": "http://www.w3.org/2001/XMLSchema#string",
                    }
                ],
                "output": {"require_property": True},
            }
        ],
    }
    document = {
        "@context": EX + "context",
        "@id": EX + "record",
        "@type": EX + "Record",
        "label": "Actual",
    }
    context = {"@context": {"label": EX + "label"}}
    usages = [{"model": "Record", "path": []}]
    _write(directory / "terms.json", [EX + "Record"])
    _write(directory / "models.json", [model])
    _write(directory / "usages.json", usages)
    _write(directory / "output.json", document)
    _write(directory / "old-output.json", document)
    _write(
        directory / "expectations.json",
        [{"path": ["label"], "predicate": EX + "label"}],
    )
    for name in ("old-context.json", "new-context.json"):
        _write(directory / name, context)
    application = {
        "version": 1,
        "terms": {
            "record": {
                "iri": EX + "Record",
                "kinds": ["http://www.w3.org/2002/07/owl#Class"],
            }
        },
        "models": {"record": model},
        "fields": {
            "label": {
                "model": "Record",
                "field": "label",
                "samples": {"one": ["label"]},
            }
        },
        "outputs": {"one": {"document": document, "usages": usages}},
        "resources": {EX + "context": context},
    }
    for name in ("before.json", "after.json"):
        _write(directory / name, application)
    builder = ProjectBuilder(directory=directory, strict=True)
    builder.rdf("old", "old.ttl", format="turtle", iri=EX + "old")
    builder.rdf("new", "new.ttl", format="turtle", iri=EX + "new")
    builder.context(EX + "context", "new-context.json")
    builder.vocabulary("terms", source="new", inventory="terms.json")
    builder.models(
        "models", source="new", declarations="models.json", depends_on=("terms",)
    )
    builder.emission(
        "output",
        source="new",
        declarations="models.json",
        document="output.json",
        usages="usages.json",
        depends_on=("models",),
    )
    builder.context_corpus(
        "contexts",
        documents=(
            CorpusDocumentInput(
                "record", "old-output.json", "expectations.json", "output.json"
            ),
        ),
        before_contexts={EX + "context": "old-context.json"},
        after_contexts={EX + "context": "new-context.json"},
        branches=(CorpusBranch("label", (CorpusWitness("record", ("label",)),)),),
    )
    builder.upgrade(
        "release",
        before=ApplicationInput("old", "before.json"),
        after=ApplicationInput("new", "after.json"),
        depends_on=("output",),
    )
    configured = builder.build(source=str(directory / "project.json"))
    _write(directory / "project.json", configured.to_dict())
    return configured


def test_typed_builder_and_loaded_project_run_actual_checks_with_same_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One validated configuration runs the complete real file-based workflow."""
    built = _project(tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    loaded = load_project(tmp_path / "project.json")
    assert built.to_dict() == loaded.to_dict()
    report = check_project(built)
    assert report.verified, report.to_dict()
    assert report.to_dict() == check_project(loaded).to_dict()
    assert [child.scope for child in report.children] == [
        "terms",
        "models",
        "output",
        "contexts",
        "release",
    ]
    selected = {path.name for path in loaded.input_paths()}
    assert {
        "before.json",
        "after.json",
        "old-context.json",
        "usages.json",
        "old-output.json",
    } <= selected
    assert str(tmp_path / "before.json") in str(loaded.explain())


def test_missing_input_blocks_required_dependents_but_independent_findings_finish(
    tmp_path: Path,
) -> None:
    """Neither failed IO nor policy permission can turn prerequisite truth green."""
    original = _project(tmp_path).to_dict()
    jobs = cast(list[dict[str, object]], original["checks"])
    jobs[2]["document"] = "absent.json"
    _write(tmp_path / "missing-terms.json", [EX + "Missing"])
    jobs.append(
        {
            "name": "independent",
            "kind": "vocabulary",
            "source": "new",
            "inventory": "missing-terms.json",
        }
    )
    report = check_project(ProjectConfig(original, directory=tmp_path))
    assert report.children[2].outcome == "input_error"
    assert report.children[3].verified
    assert report.children[4].outcome == "unverified"
    assert report.children[4].issues[0].code == "project.dependency_unverified"
    assert report.children[5].outcome == "disagreement"
    assert any(i.code == "vocabulary.term_missing" for i in report.issues)
    # A real checker finding also blocks dependent emission, independently of IO.
    jobs[0]["inventory"] = "missing-terms.json"
    report = check_project(ProjectConfig(original, directory=tmp_path))
    assert report.children[0].outcome == "disagreement"
    assert all(report.children[index].outcome == "unverified" for index in (1, 2, 4))
    assert report.children[3].verified and report.children[5].outcome == "disagreement"
    finding = report.children[0].issues[0]
    original["policy"] = {
        "exceptions": [{"code": finding.code, "location": finding.location}]
    }
    permitted = check_project(ProjectConfig(original, directory=tmp_path))
    assert permitted.children[1].outcome == "unverified"
    original["limits"] = {"max_issues": 1}
    bounded = check_project(ProjectConfig(original, directory=tmp_path))
    assert bounded.outcome == "exhausted"
    assert any(i.code == "vocabulary.term_missing" for i in bounded.issues)
    oversized = json.loads(json.dumps(original))
    oversized["limits"] = {"max_bytes": 1000}
    oversized["checks"][0]["name"] = "x" * 100_000
    oversized["checks"] = [oversized["checks"][0]]
    metadata = check_project(ProjectConfig(oversized, directory=tmp_path))
    assert metadata.outcome == "exhausted" and not metadata.verified
    assert len(metadata.children[0].scope) < 100
    oversized["checks"].append({**oversized["checks"][0], "name": "y" * 100_000})
    metadata = check_project(ProjectConfig(oversized, directory=tmp_path))
    assert metadata.outcome == "exhausted" and metadata.requested == 2
    assert all(issue.expected is None for issue in metadata.issues)


def test_version_dependency_and_builder_validation_share_one_schema(
    tmp_path: Path,
) -> None:
    """New syntax is explicit and invalid dependencies never become ignored options."""
    data = _project(tmp_path).to_dict()
    with pytest.raises(InputError, match="version 2"):
        ProjectConfig({**data, "version": 1}, directory=tmp_path)
    jobs = cast(list[dict[str, object]], data["checks"])
    for dependencies in (["release"], ["terms", "terms"], ["absent"]):
        modified = json.loads(json.dumps(data))
        modified["checks"][0]["depends_on"] = dependencies
        with pytest.raises(InputError):
            ProjectConfig(modified, directory=tmp_path)
    with pytest.raises(InputError, match="project execution"):
        PreparedChecks(ProjectConfig(data, directory=tmp_path)).check(jobs[1])
    jobs[2]["unused_option"] = True
    with pytest.raises(InputError, match="Unknown keys"):
        ProjectConfig(data, directory=tmp_path)
    builder = ProjectBuilder(directory=tmp_path)
    required = ["first"]
    builder.vocabulary("first", source="terms", inventory="missing.json")
    builder.models(
        "second", source="terms", declarations="missing.json", depends_on=required
    )
    required.clear()
    assert builder.build().checks()[1]["depends_on"] == ["first"]
    # Path discovery is metadata-only even when every selected file is missing.
    assert tmp_path / "missing.json" in builder.build().input_paths()
    aliases = ProjectBuilder(directory=tmp_path)
    aliases.rdf(URIRef("urn:vocabulary"), "new.ttl", format="turtle")
    aliases.vocabulary("selected", source="urn:vocabulary", inventory="terms.json")
    normalized = aliases.build()
    assert check_project(normalized).verified
    assert type(next(iter(cast(dict[str, object], normalized.to_dict()["rdf"])))) is str
    with pytest.raises(InputError, match="finite mapping"):
        ProjectBuilder(directory=tmp_path).context_corpus(
            "contexts",
            documents=(),
            before_contexts=cast(dict[str, str], None),
            after_contexts={},
        )
    with pytest.raises(LimitExceeded):
        ProjectBuilder(directory=tmp_path, limits=Limits(max_states=1)).context_corpus(
            "contexts",
            documents=(),
            before_contexts={EX + "a": "a", EX + "b": "b"},
            after_contexts={},
        )


def test_context_and_application_mutations_retain_their_concrete_operation_truth(
    tmp_path: Path,
) -> None:
    """Actual context topology and output changes retain their concrete check truth."""
    config = _project(tmp_path)
    _write(
        tmp_path / "new-context.json",
        {"@context": {"label": {"@reverse": EX + "label"}}},
    )
    report = check_project(config)
    assert not report.children[2].verified and not report.children[3].verified
    assert report.children[4].issues[0].code == "project.dependency_unverified"
    # Bypass only the project prerequisite to inspect the existing upgrade truth.
    data = config.to_dict()
    jobs = cast(list[dict[str, object]], data["checks"])
    jobs[-1].pop("depends_on")
    application = json.loads((tmp_path / "after.json").read_text())
    application["outputs"]["one"]["document"].pop("label")
    _write(tmp_path / "after.json", application)
    upgraded = check_project(ProjectConfig(data, directory=tmp_path)).children[-1]
    assert not upgraded.verified and any(i.kind == "finding" for i in upgraded.issues)


def test_bundle_preserves_new_direct_job_inputs_and_versioned_execution(
    tmp_path: Path,
) -> None:
    """Portable packaging owns new declared files without a competing job runner."""
    config = _project(tmp_path)
    bundle = load_bundle(tmp_path)
    assert (
        "before.json" in bundle.resource_paths
        and "usages.json" in bundle.resource_paths
    )
    relocated = bundle.materialize(tmp_path / "portable")
    assert relocated.to_dict() == config.to_dict()
    assert check_project(relocated).verified


def test_console_matches_python_and_protects_declared_application_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The actual project command uses the same schema, results and input ownership."""
    config = _project(tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    assert main(["project", str(tmp_path / "project.json"), "--json"]) == 0
    observed = json.loads(capsys.readouterr().out)
    assert observed == check_project(config).to_dict(policy=config.policy)
    source = tmp_path / "before.json"
    original = source.read_bytes()
    assert (
        main(
            [
                "project",
                str(tmp_path / "project.json"),
                "--json",
                "--output",
                str(source),
            ]
        )
        == 2
    )
    assert source.read_bytes() == original
    assert (
        json.loads(capsys.readouterr().out)["issues"][0]["code"] == "input.output_alias"
    )
    data = config.to_dict()
    cast(list[dict[str, object]], data["checks"])[2]["document"] = None
    selected = tmp_path / "missing-input.json"
    _write(selected, data)
    assert main(["project", str(selected), "--json"]) == 2
    partial = json.loads(capsys.readouterr().out)
    assert partial["scopes"][2]["outcome"] == "input_error"
    assert partial["scopes"][3]["verified"]
    assert partial["scopes"][4]["outcome"] == "unverified"
