"""Typed slots produce isolated ordinary profiles and real constraint behavior."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Literal as Attribute
from typing import cast

from rdflib import Literal, URIRef
from rdflib.namespace import SH

from bijux_ontology.limits import Limits
from bijux_ontology.rdf.terms import term_key
from bijux_ontology.shacl.authoring.compiling import compile_shapes
from bijux_ontology.shacl.authoring.types import (
    ShaclCount,
    ShaclNodeShape,
    ShaclPropertyShape,
    ShaclShapeRef,
    ShaclValue,
)
from bijux_ontology.workflows.projects.bundles import load_bundle
from bijux_ontology.workflows.projects.instantiation import (
    ConstraintSlot,
    InventorySlot,
    ParameterizedProfile,
    ProfileParameter,
    ProfileResource,
    ProfileShape,
    ResourceSlot,
    instantiate_profile,
)
from bijux_ontology.workflows.projects.instantiation.types import ParameterValue
from bijux_ontology.workflows.projects.profiles.loading import load_profile

QUOTE = 'quote " } SELECT ?this WHERE { ?s ?p ?o } # and unicode é'


def _json(path: Path, value: object) -> None:
    """Write ordinary supplied project and vocabulary inventory data."""
    path.write_text(json.dumps(value, ensure_ascii=False))


def _vocabulary(path: Path, iri: str, terms: tuple[str, ...]) -> ProfileResource:
    """Supply an independent locally closed controlled vocabulary bundle."""
    path.mkdir()
    (path / "vocabulary.ttl").write_text(
        "\n".join(
            f"<{term}> a <http://www.w3.org/2002/07/owl#Class>." for term in terms
        )
    )
    _json(path / "inventory.json", terms)
    _json(
        path / "project.json",
        {
            "version": 1,
            "rdf": {
                "vocabulary": {
                    "path": "vocabulary.ttl",
                    "iri": iri,
                    "effective_url": iri + "/effective",
                }
            },
            "checks": [
                {
                    "name": "terms",
                    "kind": "vocabulary",
                    "source": "vocabulary",
                    "inventory": "inventory.json",
                }
            ],
        },
    )
    return ProfileResource(load_bundle(path), "vocabulary", terms)


def _template(path: Path) -> ParameterizedProfile:
    """Author a complete typed baseline and ordinary mandatory parent profile."""
    path.mkdir()
    definitions = (
        ShaclNodeShape(
            URIRef("urn:Shape"),
            properties=(
                ShaclShapeRef(URIRef("urn:Values")),
                ShaclShapeRef(URIRef("urn:Title")),
            ),
            target_nodes=(URIRef("urn:r"),),
        ),
        ShaclPropertyShape(
            URIRef("urn:Values"),
            URIRef("urn:p"),
            constraints=(
                ShaclCount(1, 10),
                ShaclValue(node_kind="iri", in_values=(URIRef("urn:old"),)),
            ),
        ),
        ShaclPropertyShape(
            URIRef("urn:Title"),
            URIRef("urn:title"),
            constraints=(ShaclValue(has_value=Literal("placeholder")),),
        ),
    )
    (path / "shapes.nt").write_bytes(
        compile_shapes(definitions).graph().serialize(format="nt", encoding="utf-8")
    )
    (path / "data.ttl").write_text(
        "<urn:r> <urn:p> <urn:A>; <urn:title> " + Literal(QUOTE).n3() + "."
    )
    (path / "vocabulary.ttl").write_text(
        "<urn:old> a <http://www.w3.org/2002/07/owl#Class>."
    )
    _json(path / "inventory.json", ["urn:old"])
    _json(
        path / "project.json",
        {
            "version": 2,
            "rdf": {
                "shapes": {
                    "path": "shapes.nt",
                    "iri": "urn:authored:shapes",
                    "effective_url": "urn:authored:effective-shapes",
                    "format": "nt",
                },
                "vocabulary": {"path": "vocabulary.ttl", "iri": "urn:old-vocabulary"},
                "data": {"path": "data.ttl", "iri": "urn:data"},
            },
            "checks": [
                {
                    "name": "terms",
                    "kind": "vocabulary",
                    "source": "vocabulary",
                    "inventory": "inventory.json",
                },
                {
                    "name": "constraints",
                    "kind": "shacl",
                    "document": "data",
                    "shapes": "shapes",
                    "use_shapes": ["urn:Shape"],
                    "depends_on": ["terms"],
                },
            ],
        },
    )
    _json(
        path / "profile.json",
        {
            "version": 1,
            "id": "urn:authored",
            "artifacts": [
                {
                    "iri": "urn:shape-artifact",
                    "path": "shapes.nt",
                    "roles": ["urn:constraint-role"],
                    "format": "application/n-triples",
                },
                {
                    "iri": "urn:vocabulary-artifact",
                    "path": "vocabulary.ttl",
                    "roles": ["urn:vocabulary-role"],
                    "format": "text/turtle",
                },
            ],
            "shapes": [{"source": "shapes", "roots": ["urn:Shape"]}],
        },
    )
    return ParameterizedProfile(
        load_profile(path),
        (
            ProfileParameter("vocabulary", "resource"),
            ProfileParameter("minimum", "integer"),
            ProfileParameter("maximum", "integer"),
            ProfileParameter("kind", "enum", ("iri", "literal")),
            ProfileParameter("title", "rdf_term"),
        ),
        shapes=(ProfileShape("shapes", definitions),),
        constraints=(
            ConstraintSlot(
                "vocabulary", "shapes", URIRef("urn:Values"), 1, "in_values"
            ),
            ConstraintSlot("minimum", "shapes", URIRef("urn:Values"), 0, "minimum"),
            ConstraintSlot("maximum", "shapes", URIRef("urn:Values"), 0, "maximum"),
            ConstraintSlot("kind", "shapes", URIRef("urn:Values"), 1, "node_kind"),
            ConstraintSlot("title", "shapes", URIRef("urn:Title"), 0, "has_value"),
        ),
        inventories=(InventorySlot("vocabulary", "inventory.json"),),
        resources=(ResourceSlot("vocabulary", "vocabulary"),),
    )


def _bindings(
    vocabulary: ProfileResource, minimum: int = 1
) -> dict[str, ParameterValue]:
    """Keep independent input values explicit, including exact quoted RDF data."""
    return {
        "vocabulary": vocabulary,
        "minimum": minimum,
        "maximum": 3,
        "kind": "iri",
        "title": Literal(QUOTE),
    }


def test_two_vocabularies_bounds_and_quoted_data_are_real_isolated_profiles(
    tmp_path: Path,
) -> None:
    """One author profile produces distinct inventories, RDF and observed outcomes."""
    template = _template(tmp_path / "author")
    first = _vocabulary(tmp_path / "first", "urn:first", ("urn:A", "urn:B"))
    second = _vocabulary(tmp_path / "second", "urn:second", ("urn:B",))
    a = instantiate_profile(
        template,
        _bindings(first),
        instance_iri="urn:instance:a",
        destination=tmp_path / "a",
        parents={},
    )
    b = instantiate_profile(
        template,
        _bindings(second, 2),
        instance_iri="urn:instance:b",
        destination=tmp_path / "b",
        parents={},
    )
    assert a.report.verified and a.effective is not None
    assert b.report.verified and b.effective is not None
    assert a.effective.check(tmp_path / "a-check").report.verified
    assert not b.effective.check(tmp_path / "b-check").accepted
    a_layer = a.effective.layers[-1]
    assert a_layer.bundle is not None
    config = a_layer.bundle.configuration
    assert config.rdf["shapes"].document_iri == "urn:authored:shapes"
    assert config.rdf["shapes"].effective_url == "urn:authored:effective-shapes"
    assert config.rdf["vocabulary"].document_iri == "urn:first"
    assert config.rdf["vocabulary"].effective_url == "urn:first/effective"
    assert a.profile is not None
    descriptions = {artifact.iri: artifact for artifact in a.profile.artifacts}
    assert descriptions["urn:shape-artifact"].path != "shapes.nt"
    assert descriptions["urn:shape-artifact"].format == "application/n-triples"
    assert descriptions["urn:vocabulary-artifact"].path != "vocabulary.ttl"
    assert descriptions["urn:vocabulary-artifact"].roles == ("urn:vocabulary-role",)
    from bijux_ontology.documents.preparation import prepare_source

    shapes = prepare_source(a_layer.bundle.rdf["shapes"]).graph()
    literal = shapes.value(URIRef("urn:Title"), SH.hasValue)
    assert literal is not None and term_key(literal) == term_key(Literal(QUOTE))
    assert json.loads((tmp_path / "a/profile/inventory.json").read_text()) == [
        "urn:A",
        "urn:B",
    ]
    assert json.loads((tmp_path / "b/profile/inventory.json").read_text()) == ["urn:B"]
    assert json.loads((tmp_path / "author/inventory.json").read_text()) == ["urn:old"]
    (tmp_path / "first/vocabulary.ttl").write_text("")
    assert a.effective.check(tmp_path / "a-again").report.verified


def test_bad_bindings_and_slots_fail_before_outputs(tmp_path: Path) -> None:
    """No coercion, undeclared parameter or incompatible slot can alter an artifact."""
    template = _template(tmp_path / "author")
    resource = _vocabulary(tmp_path / "vocab", "urn:vocab", ("urn:A",))
    cases = [
        dict(_bindings(resource), minimum=True),
        dict(_bindings(resource), title=QUOTE),
        dict(_bindings(resource), kind="other"),
        dict(_bindings(resource), undeclared=1),
    ]
    missing = _bindings(resource)
    del missing["title"]
    cases.append(missing)
    for index, binding in enumerate(cases):
        destination = tmp_path / f"invalid-{index}"
        result = instantiate_profile(
            template,
            binding,
            instance_iri="urn:invalid",
            destination=destination,
            parents={},
        )
        assert not result.report.verified and result.profile is None
        assert not destination.exists()
    bad_slot = replace(
        template,
        constraints=(
            ConstraintSlot("minimum", "shapes", URIRef("urn:Values"), 1, "minimum"),
            *template.constraints[1:],
        ),
    )
    result = instantiate_profile(
        bad_slot,
        _bindings(resource),
        instance_iri="urn:bad-slot",
        destination=tmp_path / "bad-slot",
        parents={},
    )
    assert not result.report.verified and not (tmp_path / "bad-slot").exists()


def test_normalized_slot_metadata_and_shared_artifact_scope(tmp_path: Path) -> None:
    """Accepted string subclasses bind consistently; live artifacts stay unambiguous."""
    template = _template(tmp_path / "author")
    resource = _vocabulary(tmp_path / "vocab", "urn:scalar-selected", ("urn:A",))
    parameters = tuple(
        replace(parameter, name="urn:minimum")
        if parameter.name == "minimum"
        else parameter
        for parameter in template.parameters
    )
    constraints = tuple(
        replace(
            slot,
            parameter=URIRef("urn:minimum"),
            resource=URIRef("shapes"),
            attribute=cast(Attribute["minimum"], URIRef("minimum")),
        )
        if slot.parameter == "minimum"
        else slot
        for slot in template.constraints
    )
    normalized = replace(template, parameters=parameters, constraints=constraints)
    values = _bindings(resource)
    values["urn:minimum"] = values.pop("minimum")
    result = instantiate_profile(
        normalized,
        values,
        instance_iri="urn:scalar-instance",
        destination=tmp_path / "scalar",
        parents={},
    )
    assert result.report.verified and result.effective is not None
    assert result.effective.check(tmp_path / "scalar-check").report.verified
    config_path = tmp_path / "author/project.json"
    config = json.loads(config_path.read_text())
    config["rdf"]["retained-vocabulary"] = {
        "path": "vocabulary.ttl",
        "iri": "urn:retained-vocabulary",
    }
    _json(config_path, config)
    shared = replace(template, profile=load_profile(tmp_path / "author"))
    rejected = instantiate_profile(
        shared,
        _bindings(resource),
        instance_iri="urn:shared-instance",
        destination=tmp_path / "shared",
        parents={},
    )
    assert not rejected.report.verified and rejected.profile is None
    assert not (tmp_path / "shared").exists()
    manifest_path = tmp_path / "author/profile.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["artifacts"] = [
        artifact
        for artifact in manifest["artifacts"]
        if artifact["path"] != "vocabulary.ttl"
    ]
    _json(manifest_path, manifest)
    unambiguous = replace(template, profile=load_profile(tmp_path / "author"))
    allowed = instantiate_profile(
        unambiguous,
        _bindings(resource),
        instance_iri="urn:retained-instance",
        destination=tmp_path / "retained",
        parents={},
    )
    assert allowed.report.verified and allowed.effective is not None
    assert allowed.effective.check(tmp_path / "retained-check").report.verified


def test_unrepresented_baseline_and_budget_exhaustion_remain_non_green(
    tmp_path: Path,
) -> None:
    """Unknown original assertions survive as blocked preparation, never silent loss."""
    template = _template(tmp_path / "author")
    resource = _vocabulary(tmp_path / "vocab", "urn:vocab", ("urn:A",))
    with (tmp_path / "author/shapes.nt").open("a") as handle:
        handle.write('<urn:Shape> <urn:extension> "opaque" .')
    result = instantiate_profile(
        template,
        _bindings(resource),
        instance_iri="urn:opaque",
        destination=tmp_path / "opaque",
        parents={},
    )
    assert not result.report.verified and not (tmp_path / "opaque").exists()
    limited = instantiate_profile(
        template,
        _bindings(resource),
        instance_iri="urn:limit",
        destination=tmp_path / "limited",
        parents={},
        limits=Limits(max_work=10),
    )
    assert not limited.report.verified and any(
        issue.kind == "exhausted" for issue in limited.report.issues
    )
    assert not (tmp_path / "limited").exists()


def test_parent_mandatory_failure_is_not_removed_by_instantiation(
    tmp_path: Path,
) -> None:
    """Parameter slots never override parent jobs, resources or acceptance truth."""
    template = _template(tmp_path / "author")
    _vocabulary(tmp_path / "parent", "urn:parent-vocabulary", ("urn:Parent",))
    _json(tmp_path / "parent/inventory.json", ["urn:Missing"])
    _json(tmp_path / "parent/profile.json", {"version": 1, "id": "urn:parent"})
    parent = load_profile(tmp_path / "parent")
    manifest = json.loads((tmp_path / "author/profile.json").read_text())
    manifest["parents"] = [{"id": "urn:parent"}]
    _json(tmp_path / "author/profile.json", manifest)
    template = replace(template, profile=load_profile(tmp_path / "author"))
    resource = _vocabulary(tmp_path / "selected", "urn:selected", ("urn:A",))
    instance = instantiate_profile(
        template,
        _bindings(resource),
        instance_iri="urn:child-instance",
        destination=tmp_path / "instance",
        parents={parent.iri: parent},
    )
    assert instance.report.verified and instance.effective is not None
    assert [layer.iri for layer in instance.effective.layers] == [
        "urn:parent",
        "urn:child-instance",
    ]
    checked = instance.effective.check(tmp_path / "check")
    assert not checked.accepted and not checked.report.verified
    assert not checked.raw_reports[1][1].verified
    assert checked.raw_reports[2][1].verified
