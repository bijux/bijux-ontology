"""Real project inputs retain cwd independence, reuse and partial failures."""

import json
from pathlib import Path
from typing import cast

import pytest

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.workflows.projects.checking import PreparedChecks, check_project
from bijux_ontology.workflows.projects.configuration import ProjectConfig, load_project

EX = "https://example.org/"


def test_local_query_rules_use_project_paths_and_preserve_required_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ontology.ttl").write_text(
        f"<{EX}Child> <http://www.w3.org/2000/01/rdf-schema#subClassOf> <{EX}Parent> .",
        encoding="utf-8",
    )
    (tmp_path / "labels.rq").write_text(
        "SELECT ?term WHERE {?term ?p ?o FILTER NOT EXISTS "
        "{?term <http://www.w3.org/2000/01/rdf-schema#label> ?label}}",
        encoding="utf-8",
    )
    (tmp_path / "rules.json").write_text(
        json.dumps(
            [
                {
                    "name": "labels",
                    "query_file": "labels.rq",
                    "scope": {"kind": "default"},
                }
            ]
        ),
        encoding="utf-8",
    )
    job = {
        "name": "competency",
        "kind": "query-rules",
        "source": "terms",
        "rules": "rules.json",
    }
    data = {"version": 1, "rdf": {"terms": {"path": "ontology.ttl"}}, "checks": [job]}
    (tmp_path / "project.json").write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.chdir(tmp_path.parent)
    configuration = load_project(tmp_path / "project.json")
    assert str(tmp_path / "rules.json") in str(configuration.explain())
    report = check_project(configuration)
    assert report.complete and report.outcome == "disagreement"
    finding = next(
        issue for issue in report.issues if issue.code == "data.query_violation"
    )
    assert finding.details is not None and finding.details.term == EX + "Child"
    assert finding.details.source == str(tmp_path / "labels.rq")
    ignored = {**data, "checks": [{**job, "union": True}]}
    invalid = check_project(ProjectConfig(ignored, directory=tmp_path))
    assert invalid.outcome == "input_error" and "union" in invalid.issues[0].message
    (tmp_path / "ontology.ttl").write_text("", encoding="utf-8")
    empty = check_project(load_project(tmp_path / "project.json"))
    assert empty.outcome == "input_error" and not empty.verified
    assert any(issue.code == "data.query_empty_scope" for issue in empty.issues)


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


def test_resource_replacement_invalidates_imported_range_and_preserves_readers(
    tmp_path: Path,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from bijux_ontology.rdf.sources import RdfSource

    core = tmp_path / "core.ttl"
    imported = tmp_path / "range.ttl"
    other = tmp_path / "other.ttl"
    core.write_text(
        f"<{EX}ontology> <http://www.w3.org/2002/07/owl#imports> <{EX}ranges> . "
        f"<{EX}Record> a <http://www.w3.org/2002/07/owl#Class> . "
        f"<{EX}value> a <http://www.w3.org/2002/07/owl#DatatypeProperty> ."
    )
    statement = (
        f"<{EX}value> <http://www.w3.org/2000/01/rdf-schema#range> "
        "<http://www.w3.org/2001/XMLSchema#{}> ."
    )
    imported.write_text(statement.format("string"))
    other.write_text(f"<{EX}Other> a <http://www.w3.org/2002/07/owl#Class> .")
    (tmp_path / "models.json").write_text(
        json.dumps(
            [
                {
                    "identifier": "record",
                    "types": [EX + "Record"],
                    "fields": [
                        {
                            "name": "value",
                            "predicate": EX + "value",
                            "values": [
                                {
                                    "kind": "literal",
                                    "datatype": "http://www.w3.org/2001/XMLSchema#string",
                                }
                            ],
                        }
                    ],
                }
            ]
        )
    )
    job: dict[str, object] = {
        "name": "models",
        "kind": "models",
        "source": "core",
        "declarations": "models.json",
    }
    old = PreparedChecks(
        ProjectConfig(
            {
                "version": 1,
                "rdf": {
                    "core": {"path": "core.ttl"},
                    "range": {"path": "range.ttl", "iri": EX + "ranges"},
                    "other": {"path": "other.ttl"},
                },
                "checks": [job],
            },
            directory=tmp_path,
        )
    )
    assert old.check(job).verified
    unrelated, unrelated_view = old.source("other"), old.vocabulary("other")
    imported.write_text(statement.format("integer"))
    revised = old.replace_resources(
        rdf={"range": RdfSource(imported, document_iri=EX + "ranges")}
    )
    imported.write_text("invalid after acquisition")
    assert revised.source("other") is unrelated
    assert revised.vocabulary("other") is unrelated_view
    assert revised.source("core") is old.source("core")
    assert revised.vocabulary("core") is not old.vocabulary("core")
    assert old.check(job).verified
    assert "models.datatype_conflict" in {
        issue.code for issue in revised.check(job).issues
    }
    with ThreadPoolExecutor(max_workers=4) as pool:
        observed = tuple(
            pool.map(lambda session: session.check(job).verified, [old, revised] * 5)
        )
    assert observed == (True, False) * 5


def test_context_replacement_rebuilds_dependent_rdf_and_keeps_original_documents(
    tmp_path: Path,
) -> None:
    from rdflib import Literal, URIRef
    from rdflib.namespace import XSD

    (tmp_path / "outer.json").write_text(
        json.dumps({"@context": {"@import": EX + "inner"}})
    )
    (tmp_path / "inner.json").write_text(
        json.dumps({"@context": {"label": EX + "label"}})
    )
    (tmp_path / "data.jsonld").write_text(
        json.dumps({"@context": EX + "outer", "@id": EX + "record", "label": "Name"})
    )
    (tmp_path / "other.ttl").write_text(
        f"<{EX}Other> a <http://www.w3.org/2002/07/owl#Class> ."
    )
    (tmp_path / "expectations.json").write_text(
        json.dumps([{"path": ["label"], "predicate": EX + "label"}])
    )
    job: dict[str, object] = {
        "name": "mapping",
        "kind": "context",
        "document": "data.jsonld",
        "expectations": "expectations.json",
    }
    old = PreparedChecks(
        ProjectConfig(
            {
                "version": 1,
                "rdf": {
                    "data": {"path": "data.jsonld"},
                    "other": {"path": "other.ttl"},
                },
                "contexts": {EX + "outer": "outer.json", EX + "inner": "inner.json"},
                "checks": [job],
            },
            directory=tmp_path,
        )
    )
    original = old.source("data")
    unrelated = old.source("other")
    assert old.check(job).verified
    replacement = {"@context": {"label": EX + "changed"}}
    revised = old.replace_resources(contexts={EX + "inner": replacement})
    replacement["@context"]["label"] = EX + "later"
    (tmp_path / "outer.json").write_text("invalid after first use")
    (tmp_path / "data.jsonld").write_text("invalid after first use")
    assert revised.source("other") is unrelated
    assert old.source("data") is original
    updated = revised.source("data")
    assert updated is not original
    assert (
        URIRef(EX + "record"),
        URIRef(EX + "label"),
        Literal("Name", datatype=XSD.string),
    ) in original.graph()
    assert (
        URIRef(EX + "record"),
        URIRef(EX + "changed"),
        Literal("Name", datatype=XSD.string),
    ) in updated.graph()
    # Context job inputs themselves remain ordinary per-call files.
    (tmp_path / "data.jsonld").write_text(
        json.dumps({"@context": EX + "outer", "@id": EX + "record", "label": "Name"})
    )
    assert old.check(job).verified
    assert "context.mapping_mismatch" in {
        issue.code for issue in revised.check(job).issues
    }


def test_replacement_owns_graphs_and_bounds_retained_storage_with_fresh_budgets(
    tmp_path: Path,
) -> None:
    from rdflib import Dataset, Graph, URIRef
    from rdflib.namespace import OWL, RDF

    from bijux_ontology.diagnostics.errors import LimitExceeded
    from bijux_ontology.rdf.sources import RdfSource

    (tmp_path / "terms.ttl").write_text(f"<{EX}Record> a <{OWL.Class}> .")
    config = ProjectConfig(
        {
            "version": 1,
            "rdf": {"terms": {"path": "terms.ttl"}},
            "contexts": {EX + "context": "unread.json"},
            "limits": {"max_resources": 1},
            "checks": [{"name": "one", "kind": "ontology", "source": "terms"}],
        },
        directory=tmp_path,
    )
    old = PreparedChecks(config)
    snapshot = old.source("terms")
    with pytest.raises(LimitExceeded, match="max_resources"):
        old.replace_resources(contexts={EX + "context": {"@context": {}}})
    assert old.source("terms") is snapshot
    with pytest.raises(InputError, match="registered"):
        old.replace_resources(rdf={"unknown": RdfSource(Graph())})
    selected = old
    for index in range(3):
        graph = Graph().add((URIRef(EX + str(index)), RDF.type, OWL.Class))
        selected = selected.replace_resources(rdf={"terms": RdfSource(graph)})
        graph.remove((None, None, None))
        assert len(selected.source("terms").graph()) == 1
    assert old.source("terms") is snapshot

    bounded = PreparedChecks(
        ProjectConfig(
            {
                "version": 1,
                "rdf": {
                    "terms": {"path": "terms.ttl"},
                    "extra": {"path": "unread.ttl"},
                },
                "limits": {"max_triples": 2},
                "checks": [{"name": "one", "kind": "ontology", "source": "terms"}],
            },
            directory=tmp_path,
        )
    )
    baseline = bounded.source("terms")
    extra = Dataset()
    for name in ("one", "two"):
        extra.graph(URIRef(EX + name)).add((URIRef(EX + "Same"), RDF.type, OWL.Class))
    with pytest.raises(LimitExceeded, match="max_triples"):
        bounded.replace_resources(rdf={"extra": RdfSource(extra)})
    assert bounded.source("terms") is baseline


def test_context_replacement_releases_only_invalidated_snapshot_accounting(
    tmp_path: Path,
) -> None:
    (tmp_path / "context.json").write_text(
        json.dumps({"@context": {name: EX + name for name in ("a", "b", "c")}})
    )
    (tmp_path / "data.jsonld").write_text(
        json.dumps(
            {
                "@context": EX + "context",
                "@id": EX + "record",
                "a": "one",
                "b": "two",
                "c": "two",
            }
        )
    )
    (tmp_path / "extra.ttl").write_text(
        f"<{EX}Other> a <http://www.w3.org/2002/07/owl#Class> ."
    )
    old = PreparedChecks(
        ProjectConfig(
            {
                "version": 1,
                "rdf": {
                    "data": {"path": "data.jsonld"},
                    "extra": {"path": "extra.ttl"},
                },
                "contexts": {EX + "context": "context.json"},
                "limits": {"max_triples": 3},
                "checks": [{"name": "one", "kind": "ontology", "source": "extra"}],
            },
            directory=tmp_path,
        )
    )
    original = old.source("data")
    assert len(original.graph()) == 3
    revised = old.replace_resources(
        contexts={
            EX + "context": {"@context": {"a": EX + "a", "b": EX + "b", "c": EX + "b"}}
        }
    )
    assert len(revised.source("data").graph()) == 2
    assert len(revised.source("extra").graph()) == 1
    assert old.source("data") is original and len(original.graph()) == 3
