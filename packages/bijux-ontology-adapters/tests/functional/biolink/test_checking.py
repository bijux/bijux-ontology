"""Actual local BMT preserves qualified context, policy and release distinctions."""

import gzip
import importlib.util
import json
from dataclasses import replace
from hashlib import sha256
from importlib import import_module
from importlib.metadata import version as installed_version
from pathlib import Path

import pytest

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology_adapters.biolink import (
    BiolinkAssociation,
    BiolinkModel,
    BiolinkPolicy,
    BiolinkQualifier,
    BiolinkResource,
    check_biolink,
    prepare_biolink,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "biolink"
# Exact raw official release files; gzip is only fixture storage. Model sources:
# https://github.com/biolink/biolink-model/tree/v4.4.4 and /tree/v4.3.6.
LIMITS = Limits(max_total_bytes=200_000_000, max_work=8_000_000, max_states=1_000_000)


def _resource(name: str, version: str, content: bytes) -> BiolinkResource:
    """Capture local bytes and their exact identity without toolkit defaults."""
    return BiolinkResource(name, version, content, sha256(content).hexdigest())


def _inputs(
    version: str,
) -> tuple[BiolinkResource, BiolinkResource, tuple[BiolinkResource, ...]]:
    """Include the native installed LinkML types as an explicitly supplied import."""
    if importlib.util.find_spec("bmt") is None:
        pytest.skip("Actual optional BMT installation is required")
    schema_directory = import_module("linkml_runtime").SCHEMA_DIRECTORY

    def read(name: str) -> bytes:
        """Read the original official bytes from compressed package test fixtures."""
        return gzip.decompress((FIXTURES / name).read_bytes())

    return (
        _resource("model", version, read(version + "-biolink-model.yaml.gz")),
        _resource("predicate-map", version, read("predicate-mapping.yaml.gz")),
        (
            _resource("attributes", version, read(version + "-attributes.yaml.gz")),
            _resource(
                "linkml:types",
                installed_version("linkml-runtime"),
                (Path(schema_directory) / "types.yaml").read_bytes(),
            ),
        ),
    )


@pytest.fixture(scope="module")
def model() -> BiolinkModel:
    """Prepare the real pinned official release using actual installed BMT."""
    first, second, imports = _inputs("4.4.4")
    result = prepare_biolink(first, second, imports=imports, limits=LIMITS)
    assert result.report.verified and result.identity is not None
    assert result.identity.model_version == "4.4.4"
    assert result.identity.toolkit_version == installed_version("bmt")
    return result


def _example() -> BiolinkAssociation:
    """Use the first ChemicalAffectsGeneAssociation example in official4.4.4."""
    return BiolinkAssociation(
        "biolink:ChemicalAffectsGeneAssociation",
        "biolink:ChemicalEntity",
        "biolink:affects",
        "biolink:Gene",
        (
            BiolinkQualifier("biolink:qualified_predicate", "biolink:causes"),
            BiolinkQualifier("biolink:object_aspect_qualifier", "activity"),
            BiolinkQualifier("biolink:object_direction_qualifier", "decreased"),
            BiolinkQualifier("biolink:causal_mechanism_qualifier", "inhibition"),
        ),
        subject="PUBCHEM.COMPOUND:168290237",
        object="NCBIGene:200424",
        location="chemical-gene-example.json",
    )


def test_real_official_qualified_example_preserves_full_statement(
    model: BiolinkModel,
) -> None:
    """Native effective slots and original CURIE/qualifier order remain inspectable."""
    first, second, imports = _inputs("4.4.4")
    mismatch = prepare_biolink(
        first, second, imports=imports, require_bmt="1.0.0", limits=LIMITS
    )
    assert not mismatch.report.verified and mismatch.identity is None
    assert any("exact requirement" in issue.message for issue in mismatch.report.issues)
    example = _example()
    result = check_biolink(model, (example,), limits=LIMITS)
    assert result.report.verified and result.associations == (example,)
    interpretation = result.interpretations[0]
    assert interpretation.predicate == "biolink:affects"
    assert interpretation.qualified_predicate == "biolink:causes"
    assert interpretation.qualifiers == tuple(
        (q.type_id, q.value) for q in example.qualifiers
    )
    context = json.loads(interpretation.constraints_json)
    assert context["slots"]["object"]["range"] == "biological entity"
    assert context["slots"]["qualified predicate"]["subproperty_of"] == "causes"
    assert (
        "chemical affects biological entity association"
        in context["association_ancestors"]
    )
    assert interpretation.report is result.report.children[0].children[0]
    with pytest.raises(InputError):
        BiolinkModel()


def test_qualifier_predicate_and_endpoint_mutations_are_not_hidden(
    model: BiolinkModel,
) -> None:
    """A basic affects match cannot erase a bad qualified statement or original cell."""
    example = _example()
    bad_value = replace(
        example,
        qualifiers=(
            *example.qualifiers[:2],
            BiolinkQualifier("biolink:object_direction_qualifier", "sideways"),
            example.qualifiers[3],
        ),
    )
    bad_predicate = replace(
        example,
        qualifiers=(
            BiolinkQualifier("biolink:qualified_predicate", "biolink:related_to"),
            *example.qualifiers[1:],
        ),
    )
    bad_subject = replace(example, subject_category="biolink:Gene")
    duplicate = replace(
        example, qualifiers=(*example.qualifiers, example.qualifiers[2])
    )
    result = check_biolink(
        model,
        (example, bad_value, bad_predicate, bad_subject, duplicate),
        limits=LIMITS,
    )
    assert not result.report.verified and result.report.checked == 5
    assert result.interpretations[0].report.verified
    issues = tuple(result.report)
    assert any(
        i.code == "biolink.qualifier_value"
        and i.details
        and i.details.path == (1, "qualifiers", 2, "value")
        for i in issues
    )
    assert any(
        i.code == "biolink.qualifier_value"
        and i.details
        and i.details.path[:2] == (2, "qualifiers")
        for i in issues
    )
    assert any(
        i.code == "biolink.endpoint_range"
        and i.details
        and i.details.path == (3, "subject_category")
        for i in issues
    )
    assert any(i.code == "biolink.qualifier_cardinality" for i in issues)
    assert result.associations[1].qualifiers[2].value == "sideways"


def test_abstract_and_mixin_submission_policies_keep_native_inheritance(
    model: BiolinkModel,
) -> None:
    """Submission policies retain native inherited mixin semantics."""
    abstract = replace(_example(), object_category="biolink:BiologicalEntity")
    rejected = check_biolink(model, (abstract,), limits=LIMITS)
    assert any(i.code == "biolink.abstract_element" for i in rejected.report)
    allowed = check_biolink(
        model, (abstract,), policy=BiolinkPolicy(allow_abstract=True), limits=LIMITS
    )
    assert allowed.report.verified
    mixin = BiolinkAssociation(
        "biolink:GeneToGeneAssociation",
        "biolink:GeneOrGeneProduct",
        "biolink:related_to",
        "biolink:Gene",
    )
    rejected = check_biolink(model, (mixin,), limits=LIMITS)
    assert any(i.code == "biolink.mixin_element" for i in rejected.report)
    allowed = check_biolink(
        model, (mixin,), policy=BiolinkPolicy(allow_mixins=True), limits=LIMITS
    )
    assert allowed.report.verified
    assert json.loads(allowed.interpretations[0].constraints_json)["mixins_inherited"]
    required = BiolinkAssociation(
        "biolink:GeneRegulatesGeneAssociation",
        "biolink:Gene",
        "biolink:regulates",
        "biolink:Gene",
    )
    missing = check_biolink(model, (required,), limits=LIMITS)
    assert any(i.code == "biolink.required_qualifier" for i in missing.report)


def test_actual_published_releases_change_the_same_qualifier_context(
    model: BiolinkModel,
) -> None:
    """An old unbound class slot cannot borrow the new release's contextual range."""
    first, second, imports = _inputs("4.3.6")
    old = prepare_biolink(first, second, imports=imports, limits=LIMITS)
    assert (
        old.report.verified and old.identity and old.identity.model_version == "4.3.6"
    )
    association = BiolinkAssociation(
        "biolink:GeneToGeneAssociation",
        "biolink:Gene",
        "biolink:related_to",
        "biolink:Gene",
        (BiolinkQualifier("biolink:subject_context_qualifier", "UBERON:0001062"),),
    )
    policy = BiolinkPolicy(allow_abstract=True)
    before = check_biolink(old, (association,), policy=policy, limits=LIMITS)
    after = check_biolink(model, (association,), policy=policy, limits=LIMITS)
    assert not before.report.verified and after.report.verified
    assert any(i.code == "biolink.qualifier_context" for i in before.report)
    assert before.associations == after.associations
    assert before.identity != after.identity


def test_required_inputs_unknown_rules_and_shared_limits(model: BiolinkModel) -> None:
    """No missing-resource default, empty extraction or unsupported rule can pass."""
    first, second, imports = _inputs("4.4.4")
    missing = prepare_biolink(first, second, imports=imports[1:], limits=LIMITS)
    assert not missing.report.verified and missing.identity is None
    wrong = prepare_biolink(
        replace(first, sha256="0" * 64), second, imports=imports, limits=LIMITS
    )
    assert wrong.report.outcome == "input_error"
    assert check_biolink(model, (), limits=LIMITS).report.outcome == "input_error"
    statistical = replace(
        _example(),
        qualifiers=(
            *_example().qualifiers,
            BiolinkQualifier(
                "biolink:statistical_significance_qualifier", "significant"
            ),
        ),
    )
    uncovered = check_biolink(model, (statistical,), limits=LIMITS)
    assert not uncovered.report.verified and any(
        i.code == "biolink.rule_unverified" for i in uncovered.report
    )
    unknown = replace(
        _example(),
        qualifiers=(
            *_example().qualifiers,
            BiolinkQualifier("biolink:unavailable_qualifier", "original-value"),
        ),
    )
    retained = check_biolink(model, (unknown,), limits=LIMITS)
    assert not retained.report.verified
    assert retained.interpretations[0].qualifiers[-1] == (
        "biolink:unavailable_qualifier",
        "original-value",
    )
    two_failures = replace(
        _example(),
        qualifiers=(
            BiolinkQualifier("biolink:qualified_predicate", "biolink:related_to"),
            BiolinkQualifier("biolink:object_aspect_qualifier", "not-an-aspect"),
        ),
    )
    cut = check_biolink(model, (two_failures,), limits=replace(LIMITS, max_issues=1))
    assert cut.report.outcome == "exhausted"
    assert any(i.code == "biolink.qualifier_value" for i in cut.report)
    budget = Budget(LIMITS)
    budget.consume("states", LIMITS.max_states - 1)
    stopped = check_biolink(model, (_example(),), budget=budget)
    assert stopped.report.outcome == "exhausted"
    assert budget.remaining("states") <= 1


def test_genuine_core_requires_optional_native_toolkit() -> None:
    """Core remains importable; absent native BMT is explicit unknown."""
    if importlib.util.find_spec("bmt") is not None:
        pytest.skip("This control requires genuine core without optional BMT")
    model = _resource("model", "local", b"id: urn:local\nname: local\nversion: local\n")
    mapping = _resource("predicate-map", "local", b"predicate mappings: []\n")
    prepared = prepare_biolink(model, mapping)
    assert prepared.report.outcome == "unverified"
    assert any("[biolink]" in i.message for i in prepared.report)
    assert not check_biolink(prepared, (_example(),)).report.verified
