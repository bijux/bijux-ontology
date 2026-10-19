"""Mandatory inherited profile checks remain separate, portable and truthful."""

import json
from dataclasses import replace
from pathlib import Path
from zipfile import Path as ZipPath
from zipfile import ZipFile

import pytest

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.limits import Limits
from bijux_ontology.workflows.projects.bundles import load_bundle
from bijux_ontology.workflows.projects.profiles import (
    ProfileShapes,
    compose_profile,
    load_profile,
    profile_to_prof,
    read_prof,
)


def _package(
    root: Path,
    iri: str,
    *,
    parents: list[dict[str, object]] | None = None,
    shape: str | None = None,
    count: int = 1,
    separate: bool = False,
    invalid: bool = False,
) -> Path:
    """Supply ordinary jobs; the profile manifest never invents executable rules."""
    root.mkdir()
    rdf = {"terms": {"path": "terms.ttl", "iri": iri + "/terms"}}
    checks: list[dict[str, object]] = [
        {
            "name": "terms",
            "kind": "vocabulary",
            "source": "terms",
            "inventory": "terms.json",
        }
    ]
    (root / "terms.ttl").write_text(
        "<urn:Term> a <http://www.w3.org/2002/07/owl#Class>."
    )
    (root / "terms.json").write_text(
        json.dumps(["urn:Missing" if invalid else "urn:Term"])
    )
    manifest: dict[str, object] = {
        "version": 1,
        "id": iri,
        "parents": parents or [],
        "artifacts": [
            {
                "iri": iri + "/artifact",
                "path": "terms.ttl",
                "roles": ["http://www.w3.org/ns/dx/prof/role/vocabulary"],
                "format": "text/turtle",
            }
        ],
    }
    if shape is not None:
        rdf["shapes"] = {"path": "shapes.ttl", "iri": iri + "/shapes"}
        rdf["data"] = {"path": "data.ttl", "iri": iri + "/data"}
        (root / "shapes.ttl").write_text(
            "@prefix sh:<http://www.w3.org/ns/shacl#>. "
            f"<{shape}> a sh:NodeShape;sh:targetNode <urn:r>;"
            f"sh:property [sh:path <urn:p>;sh:minCount {count}]."
        )
        (root / "data.ttl").write_text('<urn:r> <urn:p> "value".')
        checks.append(
            {
                "name": "shape",
                "kind": "shacl",
                "document": "data",
                "shapes": "shapes",
                "use_shapes": [shape],
            }
        )
        manifest["shapes"] = [{"source": "shapes", "roots": [shape]}]
    if separate:
        manifest["conflicts"] = {
            "same_shape": "separate",
            "severity_changes": "preserve_each",
        }
    (root / "project.json").write_text(
        json.dumps({"version": 1, "rdf": rdf, "checks": checks})
    )
    (root / "profile.json").write_text(json.dumps(manifest))
    return root


def test_inheritance_executes_every_layer_and_owns_inputs(tmp_path: Path) -> None:
    """Duplicate job names retain parent checks and owned input snapshots."""
    parent_path = _package(tmp_path / "parent", "urn:parent")
    child_path = _package(
        tmp_path / "child", "urn:child", parents=[{"id": "urn:parent"}]
    )
    parent, child = load_profile(parent_path), load_profile(child_path)
    effective = compose_profile(child, {parent.iri: parent})
    assert effective.report.verified
    (parent_path / "terms.json").write_text('["urn:Missing"]')
    result = effective.check(tmp_path / "run")
    assert result.accepted and result.report.verified
    assert len(result.raw_reports) == 3
    assert all(report.verified for _, report in result.raw_reports)
    for index, altered in enumerate(
        (
            replace(effective, layers=()),
            replace(effective, layers=effective.layers[1:]),
            replace(
                effective,
                layers=(
                    replace(effective.layers[0], selected=False),
                    *effective.layers[1:],
                ),
            ),
        )
    ):
        rejected = altered.check(tmp_path / f"altered-{index}")
        assert not rejected.accepted and not rejected.report.verified
        assert not (tmp_path / f"altered-{index}").exists()
    forged = replace(child, parents=())
    assert not compose_profile(forged, {parent.iri: parent}).report.verified
    for forged in (
        replace(child, artifacts=(replace(child.artifacts[0], format="x" * 100_000),)),
        replace(child, shapes=(ProfileShapes("x" * 100_000, ("urn:Shape",)),)),
    ):
        bounded = compose_profile(forged, {}, limits=Limits(max_bytes=5000))
        assert bounded.report.outcome == "exhausted" and not bounded.report.verified
        assert bounded.iri == "" and not bounded.layers
    assert not effective.check(tmp_path / "run").accepted
    bad_parent = load_profile(parent_path)
    bad = compose_profile(child, {bad_parent.iri: bad_parent})
    rejected = bad.check(tmp_path / "bad-run")
    assert not rejected.accepted
    assert any(not report.verified for _, report in rejected.raw_reports)


def test_optional_layers_cycles_and_missing_jobs(tmp_path: Path) -> None:
    """Absent optional layers are visible; selected missing layers and cycles fail."""
    path = _package(
        tmp_path / "child",
        "urn:child",
        parents=[{"id": "urn:optional", "required": False}],
    )
    child = load_profile(path)
    effective = compose_profile(child, {})
    assert effective.report.verified
    assert [(layer.iri, layer.selected) for layer in effective.layers] == [
        ("urn:optional", False),
        ("urn:child", True),
    ]
    assert effective.check(tmp_path / "run").accepted
    selected = compose_profile(child, {}, optional_layers=("urn:optional",))
    assert not selected.report.verified
    assert not selected.check(tmp_path / "missing").accepted
    assert not (tmp_path / "missing").exists()
    cycle_path = _package(
        tmp_path / "cycle", "urn:optional", parents=[{"id": "urn:child"}]
    )
    assert not compose_profile(
        child, {"urn:optional": load_profile(cycle_path)}
    ).report.verified
    raw = json.loads((path / "profile.json").read_text())
    raw["drop_checks"] = ["terms"]
    (path / "profile.json").write_text(json.dumps(raw))
    with pytest.raises(InputError):
        load_profile(path)


def test_shape_conflicts_need_explicit_separate_checks(tmp_path: Path) -> None:
    """Both same-ID definitions remain mandatory under the explicit conflict policy."""
    parent_path = _package(tmp_path / "parent", "urn:parent", shape="urn:Shape")
    child_path = _package(
        tmp_path / "child",
        "urn:child",
        parents=[{"id": "urn:parent"}],
        shape="urn:Shape",
        count=2,
    )
    parent = load_profile(parent_path)
    assert not compose_profile(
        load_profile(child_path), {parent.iri: parent}
    ).report.verified
    manifest = json.loads((child_path / "profile.json").read_text())
    manifest["conflicts"] = {
        "same_shape": "separate",
        "severity_changes": "preserve_each",
    }
    (child_path / "profile.json").write_text(json.dumps(manifest))
    effective = compose_profile(load_profile(child_path), {parent.iri: parent})
    assert effective.report.verified and len(effective.decisions) == 1
    checked = effective.check(tmp_path / "run")
    assert not checked.accepted
    assert checked.raw_reports[1][1].verified
    assert not checked.raw_reports[2][1].verified
    assert any(
        issue.code.startswith("shacl.") for issue in checked.raw_reports[2][1].issues
    )


def test_portable_prof_is_descriptive_and_metadata_is_bounded(tmp_path: Path) -> None:
    """Moved Traversables retain checks; PROF stays descriptive."""
    source = _package(tmp_path / "source", "urn:profile")
    archive_path = tmp_path / "profile.zip"
    with ZipFile(archive_path, "w") as archive:
        for path in source.iterdir():
            archive.write(path, path.name)
    with ZipFile(archive_path) as archive:
        profile = load_profile(ZipPath(archive))
        effective = compose_profile(profile, {})
    assert effective.report.verified and effective.check(tmp_path / "unpacked").accepted
    described = profile_to_prof(profile)
    assert described.report.verified and described.source is not None
    reread = read_prof(described.source, profile_iri=profile.iri)
    assert reread.report.verified and reread.iri == profile.iri
    assert reread.artifacts == described.artifacts
    assert not hasattr(reread, "check")
    small = compose_profile(load_profile(source), {}, limits=Limits(max_resources=1))
    assert not small.report.verified and any(
        issue.kind == "exhausted" for issue in small.report.issues
    )


def test_parent_imports_and_relative_contexts_use_real_inherited_paths(
    tmp_path: Path,
) -> None:
    """Normal project resources resolve inherited logical bindings."""
    parent_path = _package(tmp_path / "parent", "urn:parent")
    child_path = _package(
        tmp_path / "child", "urn:child", parents=[{"id": "urn:parent"}]
    )
    parent = json.loads((parent_path / "project.json").read_text())
    parent["rdf"]["terms"]["effective_url"] = "https://example.org/base"
    parent["contexts"] = {"https://example.org/contexts/base": "base.json"}
    (parent_path / "base.json").write_text(
        json.dumps({"@context": {"name": "urn:name"}})
    )
    (parent_path / "project.json").write_text(json.dumps(parent))
    child = json.loads((child_path / "project.json").read_text())
    child["contexts"] = {"https://example.org/contexts/child": "child.json"}
    (child_path / "child.json").write_text(
        json.dumps({"@context": {"@import": "base"}})
    )
    (child_path / "document.json").write_text(
        json.dumps({"@context": "https://example.org/contexts/child", "name": "Record"})
    )
    (child_path / "expectations.json").write_text(
        json.dumps([{"path": ["name"], "predicate": "urn:name"}])
    )
    child["checks"][0]["references"] = True
    child["checks"].append(
        {
            "name": "context",
            "kind": "context",
            "document": "document.json",
            "expectations": "expectations.json",
        }
    )
    (child_path / "project.json").write_text(json.dumps(child))
    (child_path / "terms.ttl").write_text(
        "<urn:child/terms> <http://www.w3.org/2002/07/owl#imports> "
        "<https://example.org/base>. "
        "<urn:Child> a <http://www.w3.org/2002/07/owl#Class>; "
        "<http://www.w3.org/2000/01/rdf-schema#subClassOf> <urn:Term>."
    )
    (child_path / "terms.json").write_text('["urn:Child", "urn:Term"]')
    base = load_profile(parent_path)
    effective = compose_profile(load_profile(child_path), {base.iri: base})
    assert effective.report.verified
    checked = effective.check(tmp_path / "run")
    assert checked.report.verified
    assert all(raw.verified for _, raw in checked.raw_reports)


def test_conflicting_resource_identity_and_layer_inventory_caps(tmp_path: Path) -> None:
    """Logical conflicts reject before writes and layer caps exhaust."""
    parent_path = _package(tmp_path / "parent", "urn:parent")
    child_path = _package(
        tmp_path / "child", "urn:child", parents=[{"id": "urn:parent"}]
    )
    raw = json.loads((child_path / "project.json").read_text())
    raw["rdf"]["terms"]["iri"] = "urn:parent/terms"
    (child_path / "project.json").write_text(json.dumps(raw))
    (child_path / "terms.ttl").write_text(
        "<urn:Different> a <http://www.w3.org/2002/07/owl#Class>."
    )
    parent = load_profile(parent_path)
    conflict = compose_profile(load_profile(child_path), {parent.iri: parent})
    assert not conflict.report.verified
    assert not conflict.check(tmp_path / "conflict").accepted
    assert not (tmp_path / "conflict").exists()
    previous = None
    registry = {}
    for index in range(8):
        path = _package(
            tmp_path / f"layer-{index}",
            f"urn:layer:{index}",
            parents=[] if previous is None else [{"id": previous}],
        )
        profile = load_profile(path)
        registry[profile.iri] = profile
        previous = profile.iri
    assert compose_profile(profile, registry).report.verified
    bounded = compose_profile(profile, registry, limits=Limits(max_states=20))
    assert not bounded.report.verified
    assert any(issue.kind == "exhausted" for issue in bounded.report.issues)


def test_profile_runs_versioned_emission_jobs_and_dependency_truth(
    tmp_path: Path,
) -> None:
    """Versioned ordinary serializer-output checks remain mandatory in a profile."""
    path = _package(tmp_path / "profile", "urn:profile")
    with (path / "terms.ttl").open("a") as handle:
        handle.write(" <urn:p> a <http://www.w3.org/2002/07/owl#DatatypeProperty>.")
    model = {
        "identifier": "Record",
        "types": ["urn:Term"],
        "fields": [
            {
                "name": "label",
                "predicate": "urn:p",
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
    (path / "models.json").write_text(json.dumps([model]))
    (path / "usages.json").write_text(json.dumps([{"model": "Record", "path": []}]))
    (path / "document.json").write_text(
        json.dumps(
            {
                "@context": {"label": "urn:p"},
                "@id": "urn:record",
                "@type": "urn:Term",
                "label": "Actual",
            }
        )
    )
    config = json.loads((path / "project.json").read_text())
    config["version"] = 2
    config["checks"].append(
        {
            "name": "output",
            "kind": "emission",
            "source": "terms",
            "declarations": "models.json",
            "document": "document.json",
            "usages": "usages.json",
            "depends_on": ["terms"],
        }
    )
    (path / "project.json").write_text(json.dumps(config))
    effective = compose_profile(load_profile(path), {})
    assert effective.report.verified
    assert effective.check(tmp_path / "positive").report.verified
    (path / "document.json").write_text(
        json.dumps({"@id": "urn:record", "@type": "urn:Term"})
    )
    failed = compose_profile(load_profile(path), {}).check(tmp_path / "negative")
    assert not failed.accepted
    assert any(
        issue.code == "emission.required_occurrence_missing"
        for issue in failed.report.issues
    )


def test_executable_bundle_replacement_cannot_drop_or_weaken_required_jobs(
    tmp_path: Path,
) -> None:
    """Content witnesses bind composition to exact executable owned resources."""
    original = load_profile(
        _package(tmp_path / "original", "urn:profile", shape="urn:Shape", count=2)
    )
    effective = compose_profile(original, {})
    assert effective.report.verified
    assert not effective.check(tmp_path / "original-run").report.verified
    for name, shape in (("omitted", None), ("weakened", "urn:Shape")):
        path = _package(tmp_path / name, "urn:profile", shape=shape, count=0)
        bundle = load_bundle(path)
        altered = replace(
            effective,
            layers=(
                replace(
                    effective.layers[0],
                    bundle=bundle,
                    jobs=tuple(
                        str(job["name"]) for job in bundle.configuration.checks()
                    ),
                ),
            ),
        )
        destination = tmp_path / (name + "-run")
        assert not altered.check(destination).report.verified
        assert not destination.exists()
    # Independent reconstruction from identical owned package content is valid.
    reconstructed = replace(
        effective,
        layers=(replace(effective.layers[0], bundle=load_bundle(original.source)),),
    )
    assert (
        reconstructed.check(tmp_path / "reconstructed").report.outcome == "disagreement"
    )


def test_each_prior_named_shape_definition_requires_its_own_conflict_decision(
    tmp_path: Path,
) -> None:
    """A separately retained middle definition cannot disappear from later review."""
    first = load_profile(
        _package(tmp_path / "first", "urn:first", shape="urn:Shared", count=1)
    )
    middle = load_profile(
        _package(
            tmp_path / "middle",
            "urn:middle",
            parents=[{"id": first.iri}],
            shape="urn:Shared",
            count=2,
            separate=True,
        )
    )
    path = _package(
        tmp_path / "last",
        "urn:last",
        parents=[{"id": middle.iri}],
        shape="urn:Shared",
        count=1,
    )
    last = load_profile(path)
    registry = {first.iri: first, middle.iri: middle}
    rejected = compose_profile(last, registry)
    assert not rejected.report.verified
    assert any(
        issue.code == "profile.shape_conflict" for issue in rejected.report.issues
    )
    raw = json.loads((path / "profile.json").read_text())
    raw["conflicts"] = {"same_shape": "separate", "severity_changes": "preserve_each"}
    (path / "profile.json").write_text(json.dumps(raw))
    separated = compose_profile(load_profile(path), registry)
    assert separated.report.verified
    assert {(item.before, item.after) for item in separated.decisions} == {
        (first.iri, middle.iri),
        (first.iri, last.iri),
        (middle.iri, last.iri),
    }
