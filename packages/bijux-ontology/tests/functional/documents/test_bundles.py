"""Portable supplied resources retain their logical bases and selected closure."""

import json
import socket
import stat
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import cast
from zipfile import Path as ZipPath
from zipfile import ZipFile, ZipInfo

import pytest
from rdflib import Namespace

from bijux_ontology.declarations import TermDeclaration
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.jsonld.expansion import expand_document
from bijux_ontology.jsonld.resources import OfflineDocument
from bijux_ontology.limits import Limits
from bijux_ontology.vocabulary import check_vocabulary
from bijux_ontology.workflows.projects.bundles import load_bundle
from bijux_ontology.workflows.projects.checking import check_project
from bijux_ontology.workflows.projects.configuration import ProjectConfig
from bijux_ontology.workflows.projects.resources import ProjectResources

EX = Namespace("https://example.org/")


def bundle_directory(root: Path) -> Path:
    """Create small real ontology/context/shapes inputs with relative identifiers."""
    root.mkdir()
    files = {
        "ontology.trig": """
@prefix owl:<http://www.w3.org/2002/07/owl#>.
@prefix rdfs:<http://www.w3.org/2000/01/rdf-schema#>.
<https://example.org/ontology> {
  <> owl:imports <https://example.org/import>.
  <#Record> a owl:Class; rdfs:subClassOf <https://example.org/import#Parent>.
}
<https://example.org/application> { <urn:poison> owl:imports <urn:unavailable>. }
""",
        "import.ttl": "<#Parent> a <http://www.w3.org/2002/07/owl#Class>.",
        "shapes.trig": """
@prefix sh:<http://www.w3.org/ns/shacl#>.
<https://example.org/shapes> {
  <#Shape> a sh:NodeShape; sh:targetNode <https://example.org/data#record>;
    sh:property [ sh:path <https://example.org/name>; sh:minCount 1 ].
}
<https://example.org/unselected> { <#Shape> sh:rule <urn:unsupported>. }
""",
        "data.ttl": '<#record> <https://example.org/name> "Record".',
        "context.json": json.dumps({"@context": {"@import": "base-context"}}),
        "base-context.json": json.dumps({"@context": {"name": str(EX.name)}}),
        "document.json": json.dumps({"@context": str(EX.context), "name": "Record"}),
        "expectations.json": json.dumps(
            [{"path": ["name"], "predicate": str(EX.name)}]
        ),
        "inventory.json": json.dumps(
            [str(EX["ontology#Record"]), str(EX["import#Parent"])]
        ),
    }
    manifest = {
        "version": 1,
        "rdf": {
            "ontology": {
                "path": "ontology.trig",
                "iri": str(EX.ontology),
                "graph_name": str(EX.ontology),
            },
            "import": {"path": "import.ttl", "iri": str(EX["import"])},
            "shapes": {
                "path": "shapes.trig",
                "iri": str(EX.shapes),
                "graph_name": str(EX.shapes),
            },
            "data": {"path": "data.ttl", "iri": str(EX.data)},
        },
        "contexts": {
            str(EX.context): "context.json",
            str(EX["base-context"]): "base-context.json",
        },
        "checks": [
            {
                "name": "terms",
                "kind": "vocabulary",
                "source": "ontology",
                "inventory": "inventory.json",
                "references": True,
            },
            {
                "name": "jsonld",
                "kind": "context",
                "document": "document.json",
                "expectations": "expectations.json",
            },
            {
                "name": "constraints",
                "kind": "shacl",
                "document": "data",
                "shapes": "shapes",
            },
        ],
    }
    files["project.json"] = json.dumps(manifest)
    for name, payload in files.items():
        (root / name).write_text(payload)
    return root


def test_bundle_directory_archive_and_packaged_resources_work_offline_after_moving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = bundle_directory(tmp_path / "source")

    def offline(*args: object, **kwargs: object) -> None:
        """Fail if supplied-resource processing attempts any socket access."""
        raise AssertionError("Network access is unavailable")

    monkeypatch.setattr(socket, "socket", offline)
    original = load_bundle(source)
    assert original.rdf_scope == ("ontology", "import", "shapes", "data")
    assert original.context_scope == (str(EX.context), str(EX["base-context"]))
    assert original.context_documents == ("document.json",)
    assert check_vocabulary(
        original.vocabulary("ontology"), [TermDeclaration(str(EX["import#Parent"]))]
    ).verified
    archive = tmp_path / "resources.zip"
    original.write_archive(archive)
    moved = tmp_path / "moved"
    original.materialize(moved)
    source.rename(tmp_path / "unavailable")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    for index, artifact in enumerate((moved, archive)):
        prepared = load_bundle(artifact)
        result = check_project(prepared.materialize(tmp_path / f"execution-{index}"))
        assert result.verified, result.to_dict()
        expanded = expand_document(
            {"@context": str(EX.context), "name": "Record"},
            document_loader=prepared.document_loader,
        )
        assert expanded == [{str(EX.name): [{"@value": "Record"}]}]
    with ZipFile(archive) as handle:
        packaged = cast(Traversable, ZipPath(handle))
        prepared = load_bundle(packaged)
        assert check_project(prepared.materialize(tmp_path / "packaged")).verified
    (moved / "import.ttl").unlink()
    assert check_vocabulary(
        original.vocabulary("ontology"), [TermDeclaration(str(EX["import#Parent"]))]
    ).verified
    with pytest.raises(InputError, match="new directory"):
        original.materialize(moved)


@pytest.mark.parametrize("missing", ["import.ttl", "base-context.json"])
def test_missing_transitive_resource_fails_preparation(
    tmp_path: Path, missing: str
) -> None:
    source = bundle_directory(tmp_path / "bundle")
    manifest = json.loads((source / "project.json").read_text())
    if missing == "import.ttl":
        del manifest["rdf"]["import"]
    else:
        del manifest["contexts"][str(EX["base-context"])]
    (source / "project.json").write_text(json.dumps(manifest))
    with pytest.raises(InputError, match="supplied|JSON-LD"):
        load_bundle(source)


def test_conflicting_logical_bindings_and_unstable_bases_are_rejected(
    tmp_path: Path,
) -> None:
    source = bundle_directory(tmp_path / "bundle")
    original = json.loads((source / "project.json").read_text())
    conflict = json.loads(json.dumps(original))
    conflict["rdf"]["import"]["effective_url"] = str(EX.ontology)
    (source / "project.json").write_text(json.dumps(conflict))
    with pytest.raises(InputError, match="Conflicting bundle logical binding"):
        load_bundle(source)
    del original["rdf"]["data"]["iri"]
    (source / "project.json").write_text(json.dumps(original))
    with pytest.raises(InputError, match="logical iri"):
        load_bundle(source)


@pytest.mark.parametrize(
    "name", ["../escape", "/escape", "C:/escape", "a/../../escape", "a\\escape"]
)
def test_archive_paths_cannot_escape_any_destination(tmp_path: Path, name: str) -> None:
    archive = tmp_path / "unsafe.zip"
    with ZipFile(archive, "w") as handle:
        handle.writestr(name, "untrusted")
    with pytest.raises(InputError, match="portable relative paths"):
        load_bundle(archive)
    assert not (tmp_path / "escape").exists()


def test_archive_links_duplicates_and_oversized_content_are_rejected(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "unsafe.zip"
    link = ZipInfo("link")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with ZipFile(archive, "w") as handle:
        handle.writestr(link, "../escape")
    with pytest.raises(InputError, match="links"):
        load_bundle(archive)
    with ZipFile(archive, "w") as handle:
        handle.writestr("same", "first")
        with pytest.warns(UserWarning):
            handle.writestr("same", "second")
    with pytest.raises(InputError, match="Duplicate"):
        load_bundle(archive)
    with ZipFile(archive, "w") as handle:
        handle.writestr("large", "x" * 100)
    with pytest.raises(LimitExceeded, match="max_bytes"):
        load_bundle(archive, limits=Limits(max_bytes=16))


def test_directory_symlinks_and_manifest_path_escape_fail_without_copying(
    tmp_path: Path,
) -> None:
    source = bundle_directory(tmp_path / "bundle")
    outside = tmp_path / "outside.ttl"
    outside.write_text("<urn:a> <urn:p> <urn:b>.")
    (source / "import.ttl").unlink()
    (source / "import.ttl").symlink_to(outside)
    with pytest.raises(InputError, match="escapes|symlinks"):
        load_bundle(source)
    manifest = json.loads((source / "project.json").read_text())
    manifest["rdf"]["import"]["path"] = "../outside.ttl"
    (source / "project.json").write_text(json.dumps(manifest))
    with pytest.raises(InputError, match="portable"):
        load_bundle(source)


def dependency_directories(root: Path) -> tuple[Path, Path]:
    """Separate actual RDF and relative context imports across two owned packages."""
    parent = bundle_directory(root / "parent")
    child = bundle_directory(root / "child")
    parent_data = json.loads((parent / "project.json").read_text())
    parent_data["rdf"] = {"import": parent_data["rdf"]["import"]}
    parent_data["contexts"] = {
        str(EX["base-context"]): parent_data["contexts"][str(EX["base-context"])]
    }
    parent_data["checks"] = [
        {
            "name": "parent-terms",
            "kind": "vocabulary",
            "source": "import",
            "inventory": "inventory.json",
        }
    ]
    (parent / "inventory.json").write_text(json.dumps([str(EX["import#Parent"])]))
    (parent / "project.json").write_text(json.dumps(parent_data))
    child_data = json.loads((child / "project.json").read_text())
    del child_data["rdf"]["import"]
    del child_data["contexts"][str(EX["base-context"])]
    (child / "project.json").write_text(json.dumps(child_data))
    return parent, child


def test_owned_dependencies_preserve_closure_and_explicit_local_materialization(
    tmp_path: Path,
) -> None:
    parent_path, child_path = dependency_directories(tmp_path)
    parent_data = json.loads((parent_path / "project.json").read_text())
    parent_data["rdf"]["import"]["effective_url"] = str(EX.effective)
    (parent_path / "project.json").write_text(json.dumps(parent_data))
    (parent_path / "inventory.json").write_text(
        json.dumps([str(EX["effective#Parent"])])
    )
    for filename in ("ontology.trig", "inventory.json"):
        child_file = child_path / filename
        child_file.write_text(
            child_file.read_text().replace(str(EX["import"]), str(EX.effective))
        )
    parent = load_bundle(parent_path)
    with pytest.raises(InputError, match="supplied|JSON-LD"):
        load_bundle(child_path)
    child = load_bundle(child_path, dependencies=(parent,))
    assert child.rdf_scope == ("ontology", "shapes", "data")
    assert child.context_scope == (str(EX.context),)
    assert "import.ttl" not in child.files and "base-context.json" not in child.files
    assert check_vocabulary(
        child.vocabulary("ontology"), [TermDeclaration(str(EX["effective#Parent"]))]
    ).verified
    assert expand_document(
        {"@context": str(EX.context), "name": "actual"},
        document_loader=child.document_loader,
    ) == [{str(EX.name): [{"@value": "actual"}]}]
    diamond = load_bundle(child_path, dependencies=(child, parent))
    assert set(diamond.imports) == set(child.imports)
    parent_path.rename(tmp_path / "parent-unavailable")
    child_path.rename(tmp_path / "child-unavailable")
    assert check_vocabulary(
        child.vocabulary("ontology"), [TermDeclaration(str(EX["effective#Parent"]))]
    ).verified
    with pytest.raises(InputError, match="local_only"):
        child.materialize(tmp_path / "implicit")
    with pytest.raises(InputError, match="standalone"):
        child.write_archive(tmp_path / "incomplete.zip")
    assert not (tmp_path / "implicit").exists()
    assert not (tmp_path / "incomplete.zip").exists()
    materialized_parent = parent.materialize(tmp_path / "owned-parent")
    materialized_child = child.materialize(tmp_path / "owned-child", local_only=True)
    data = materialized_child.to_dict()
    rdf = cast(dict[str, object], data["rdf"])
    for name, source in parent.rdf.items():
        assert source.document_iri is not None
        rdf[source.document_iri] = {
            "path": str(materialized_parent.rdf[name].content),
            "iri": source.document_iri,
            "format": source.format,
            "effective_url": source.effective_url,
            "graph_name": source.graph_name,
            "union": source.union,
        }
    contexts = cast(dict[str, object], data["contexts"])
    contexts.update(
        {iri: str(path) for iri, path in materialized_parent.contexts.items()}
    )
    execution = ProjectConfig(data, directory=materialized_child.directory)
    assert check_project(materialized_parent).verified
    assert check_project(execution).verified
    view = child.configuration
    view.directory = tmp_path / "caller-mutation"
    assert child.configuration.directory != view.directory


def test_dependency_conflicts_and_child_limits_do_not_depend_on_parent_order(
    tmp_path: Path,
) -> None:
    parent_path, child_path = dependency_directories(tmp_path)
    parent = load_bundle(parent_path)
    original_rdf = (parent_path / "import.ttl").read_text()
    (parent_path / "import.ttl").write_text(
        original_rdf + "\n<urn:extra> a <http://www.w3.org/2002/07/owl#Class>."
    )
    changed_rdf = load_bundle(parent_path)
    (parent_path / "import.ttl").write_text(original_rdf)
    original_context = (parent_path / "base-context.json").read_text()
    (parent_path / "base-context.json").write_text(
        json.dumps({"@context": {"name": str(EX.other)}})
    )
    changed_context = load_bundle(parent_path)
    (parent_path / "base-context.json").write_text(original_context)
    manifest = json.loads((parent_path / "project.json").read_text())
    manifest["rdf"]["import"]["effective_url"] = str(EX["other-base"])
    (parent_path / "project.json").write_text(json.dumps(manifest))
    changed_base = load_bundle(parent_path)
    for changed in (changed_rdf, changed_context, changed_base):
        for dependencies in ((parent, changed), (changed, parent)):
            with pytest.raises(InputError, match="Conflicting bundle logical binding"):
                load_bundle(child_path, dependencies=dependencies)
    with pytest.raises(LimitExceeded):
        load_bundle(child_path, dependencies=(parent,), limits=Limits(max_resources=1))
    with pytest.raises(LimitExceeded):
        load_bundle(
            child_path, dependencies=(parent,), limits=Limits(max_total_bytes=4000)
        )
    del manifest["rdf"]["import"]["effective_url"]
    (parent_path / "project.json").write_text(json.dumps(manifest))
    (parent_path / "import.ttl").write_text(original_rdf + "\n#" + "x" * 10_000)
    large_parent = load_bundle(parent_path)
    with pytest.raises(LimitExceeded, match="Dependency payload exceeds max_bytes"):
        load_bundle(
            child_path, dependencies=(large_parent,), limits=Limits(max_bytes=5000)
        )


def test_redirected_context_metadata_survives_bundle_move_and_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = bundle_directory(tmp_path / "source")
    manifest_path = source / "project.json"
    manifest = json.loads(manifest_path.read_text())
    effective = str(EX["redirect/context"])
    imported = str(EX["redirect/base-context"])
    manifest["contexts"][str(EX.context)] = {
        "path": "context.json",
        "effective_url": effective,
    }
    manifest["contexts"][imported] = manifest["contexts"].pop(str(EX["base-context"]))
    manifest_path.write_text(json.dumps(manifest))

    def offline(*args: object, **kwargs: object) -> None:
        raise AssertionError("Supplied contexts must remain offline")

    monkeypatch.setattr(socket, "socket", offline)
    prepared = load_bundle(source)
    response = prepared.document_loader(str(EX.context), {})
    assert response["documentUrl"] == effective
    assert response["document"] == {"@context": {"@import": "base-context"}}
    archive = tmp_path / "owned.zip"
    prepared.write_archive(archive)
    source.rename(tmp_path / "unavailable")
    relocated = load_bundle(archive).materialize(tmp_path / "relocated")
    assert check_project(relocated).verified
    resources = ProjectResources(relocated)
    assert expand_document(
        {"@context": str(EX.context), "name": "original"},
        document_loader=resources.document_loader,
    ) == [{str(EX.name): [{"@value": "original"}]}]
    revised = resources.replace_resources(
        rdf={},
        contexts={
            imported: OfflineDocument({"@context": {"name": str(EX.changed)}}),
        },
    )
    assert revised.document_loader(str(EX.context), {})["documentUrl"] == effective
    assert expand_document(
        {"@context": str(EX.context), "name": "revised"},
        document_loader=revised.document_loader,
    ) == [{str(EX.changed): [{"@value": "revised"}]}]
    assert expand_document(
        {"@context": str(EX.context), "name": "old reader"},
        document_loader=resources.document_loader,
    ) == [{str(EX.name): [{"@value": "old reader"}]}]


def test_conflicting_context_response_metadata_is_not_hidden_by_equal_payloads(
    tmp_path: Path,
) -> None:
    left = bundle_directory(tmp_path / "left")
    right = bundle_directory(tmp_path / "right")
    manifest_path = right / "project.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["contexts"][str(EX.context)] = {
        "path": "context.json",
        "effective_url": str(EX["redirect/context"]),
    }
    # Both resources exist, so the failure must concern their conflicting bases.
    manifest["contexts"][str(EX["redirect/base-context"])] = "base-context.json"
    manifest_path.write_text(json.dumps(manifest))
    dependency = load_bundle(left)
    with pytest.raises(InputError, match="Conflicting bundle logical binding"):
        load_bundle(right, dependencies=(dependency,))
