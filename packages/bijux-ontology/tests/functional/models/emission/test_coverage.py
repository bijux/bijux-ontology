"""Closed application scopes preserve source, graph, alias and value obligations."""

from dataclasses import replace

from rdflib import Dataset, Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.declarations.types import (
    EmissionCoverage,
    EmissionGroup,
    FieldDeclaration,
    ModelDeclaration,
    OutputRequirement,
    SubjectIdentity,
    ValueDeclaration,
)
from bijux_ontology.models.contracts.checking import check_models
from bijux_ontology.models.emission.checking import check_emission
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import ModelUsage, RdfModelUsage
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.reports import Report

EX = Namespace("https://example.org/coverage/")
META = ("@context", "@id", "@type")


def model(*, coverage: EmissionCoverage | None = None) -> ModelDeclaration:
    return ModelDeclaration(
        "record",
        (str(EX.Record),),
        (
            FieldDeclaration(
                "label",
                str(EX.label),
                (ValueDeclaration("literal", datatype=str(XSD.string)),),
                emission="optional",
            ),
        ),
        coverage=coverage,
    )


def document() -> dict[str, object]:
    return {
        "@context": {"@vocab": str(EX)},
        "@id": str(EX.root),
        "@type": "Record",
        "label": "Root",
    }


def check(output: object, declaration: ModelDeclaration) -> Report:
    return check_emission(output, (declaration,), (ModelUsage("record"),))


def test_new_mapped_unmapped_and_structural_source_fields_are_not_hidden() -> None:
    original = model()
    output = document()
    output["extra"] = "New field"
    assert check(output, original).verified
    closed = replace(original, coverage=EmissionCoverage(metadata=META))
    report = check(output, closed)
    assert {"emission.source_field_undeclared", "emission.predicate_undeclared"} <= {
        issue.code for issue in report.issues
    }
    output["@context"] = {"label": str(EX.label)}
    dropped = check(output, closed)
    assert any(
        issue.code == "emission.source_field_undeclared" and issue.location == "/extra"
        for issue in dropped.issues
    )
    output.pop("extra")
    output["@included"] = {"@id": str(EX.other), "label": "Other"}
    included = check(output, closed)
    assert any(
        issue.code == "emission.source_field_undeclared"
        and issue.location == "/@included"
        for issue in included.issues
    )


def test_extensions_are_visible_policy_choices_and_direction_is_preserved() -> None:
    output = document()
    output["extra"] = "Extension"
    declaration = model(
        coverage=EmissionCoverage(metadata=META, extensions=(str(EX.extra),))
    )
    result = check(output, declaration)
    assert result.exit_code() == 0 and result.outcome == "disagreement"
    assert {issue.code for issue in result.issues} == {"emission.extension_allowed"}
    assert all(issue.severity == "info" for issue in result.issues)
    output.pop("extra")
    output["@reverse"] = {"related": {"@id": str(EX.peer)}}
    reverse = replace(
        declaration,
        coverage=EmissionCoverage(metadata=META, reverse_extensions=(str(EX.related),)),
    )
    allowed = check(output, reverse)
    assert allowed.exit_code() == 0, allowed.to_dict()
    assert any(issue.actual == "reverse " + str(EX.related) for issue in allowed.issues)
    wrong_direction = check(
        output,
        replace(
            reverse,
            coverage=EmissionCoverage(metadata=META, extensions=(str(EX.related),)),
        ),
    )
    assert wrong_direction.exit_code() == 1


def test_keyword_aliases_keep_identity_and_type_obligations() -> None:
    output = {
        "@context": {"@vocab": str(EX), "identity": "@id", "kind": "@type"},
        "identity": str(EX.root),
        "kind": "Record",
        "label": "Root",
    }
    declaration = model(coverage=EmissionCoverage(metadata=META))
    assert check(output, declaration).verified
    denied = check(output, replace(declaration, coverage=EmissionCoverage()))
    assert any(
        issue.code == "emission.source_field_undeclared"
        and issue.location in {"/identity", "/kind"}
        for issue in denied.issues
    )
    strict = replace(declaration, subject=SubjectIdentity(str(EX.expected), False))
    assert any(
        issue.code == "emission.subject_mismatch"
        for issue in check(output, strict).issues
    )
    indexed = replace(
        declaration, coverage=EmissionCoverage(metadata=(*META, "@index"))
    )
    canonical = document()
    canonical["@index"] = "position"
    assert check(canonical, indexed).verified
    aliased = document()
    aliased["@context"] = {"@vocab": str(EX), "index": "@index"}
    aliased["index"] = "position"
    unknown = check(aliased, indexed)
    assert not unknown.verified and any(
        issue.code == "emission.source_coverage_unverified" for issue in unknown.issues
    )
    assert not any(
        issue.code == "emission.source_field_undeclared" for issue in unknown.issues
    )
    output["kind"] = "Other"
    assert any(
        issue.code == "emission.types_mismatch"
        for issue in check(output, declaration).issues
    )


def test_explicit_aliases_check_values_shared_counts_and_required_primary_keys() -> (
    None
):
    declaration = model(
        coverage=EmissionCoverage(metadata=META, aliases=(("alias", "label"),))
    )
    output = document()
    output["@context"] = {"@vocab": str(EX), "alias": str(EX.label)}
    output.pop("label")
    output["alias"] = "Alias"
    assert check(output, declaration).verified
    output["alias"] = 9
    assert any(
        issue.code == "emission.value_mismatch"
        for issue in check(output, declaration).issues
    )
    output["alias"] = "Alias"
    required = replace(
        declaration,
        fields=(
            replace(
                declaration.fields[0],
                emission="guaranteed",
                output=OutputRequirement(require_property=True),
            ),
        ),
    )
    assert any(
        issue.code == "emission.required_occurrence_missing"
        and issue.location == "/label"
        for issue in check(output, required).issues
    )
    output["label"] = "Primary"
    counted = replace(
        declaration,
        fields=(
            replace(declaration.fields[0], output=OutputRequirement(maximum_values=1)),
        ),
    )
    result = check(output, counted)
    assert any(
        issue.code == "emission.value_count_mismatch" for issue in result.issues
    ), result.to_dict()
    minimum = replace(
        declaration,
        fields=(
            replace(declaration.fields[0], output=OutputRequirement(minimum_values=2)),
        ),
    )
    assert check(output, minimum).verified, check(output, minimum).to_dict()
    output.pop("label")
    output["alias"] = ["First", "Second"]
    assert check(output, minimum).verified, check(output, minimum).to_dict()
    output["label"] = "Primary"
    output["alias"] = "Primary"
    duplicate = check(output, counted)
    assert not any(
        issue.code == "emission.value_count_mismatch" for issue in duplicate.issues
    )
    assert duplicate.outcome != "disagreement", duplicate.to_dict()


def test_alias_children_are_discovered_and_closed_at_their_own_paths() -> None:
    parent = replace(
        model(),
        coverage=EmissionCoverage(metadata=META, aliases=(("nested", "child"),)),
        fields=(
            FieldDeclaration("label", str(EX.label), (ValueDeclaration("literal"),)),
            FieldDeclaration(
                "child", str(EX.child), (ValueDeclaration("object", model="child"),)
            ),
        ),
    )
    child = ModelDeclaration(
        "child",
        (str(EX.Child),),
        (
            FieldDeclaration(
                "label",
                str(EX.label),
                (ValueDeclaration("literal"),),
                emission="guaranteed",
            ),
        ),
        coverage=EmissionCoverage(metadata=META),
    )
    nested: dict[str, object] = {"@id": str(EX.kid), "@type": "Child", "label": "Child"}
    output = document()
    output["@context"] = {"@vocab": str(EX), "nested": str(EX.child)}
    output["nested"] = nested

    def reports() -> Report:
        return check_emission(output, (parent, child), (ModelUsage("record"),))

    assert reports().verified, reports().to_dict()
    nested["extra"] = "Child extension"
    extra = reports()
    assert any(
        issue.code == "emission.source_field_undeclared"
        and issue.location == "/nested/extra"
        for issue in extra.issues
    )
    assert not any(
        issue.code == "emission.predicate_undeclared"
        and issue.location == "/"
        and issue.actual == "forward " + str(EX.extra)
        for issue in extra.issues
    )
    nested.pop("extra")
    nested.pop("label")
    assert any(
        issue.code == "emission.required_occurrence_missing"
        and "/nested/label" in issue.location
        for issue in reports().issues
    )


def test_native_and_entity_coverage_stays_graph_scoped_with_source_unknown() -> None:
    declaration = model(coverage=EmissionCoverage(metadata=META))
    dataset = Dataset()
    for name in (EX.one, EX.two):
        graph = dataset.graph(name)
        graph.add((EX.root, RDF.type, EX.Record))
        graph.add((EX.root, EX.label, Literal("Root")))
    dataset.graph(EX.two).add((EX.root, EX.extra, Literal("Other graph")))
    source = prepare_rdf(dataset)
    usages = (RdfModelUsage("record", EX.root),)
    one = check_rdf_emission(source, (declaration,), usages, graph_name=EX.one)
    assert {issue.code for issue in one.issues} == {
        "emission.source_coverage_unverified"
    }
    two = check_rdf_emission(source, (declaration,), usages, graph_name=EX.two)
    assert any(issue.code == "emission.predicate_undeclared" for issue in two.issues)
    native = Graph().add((EX.root, RDF.type, EX.Record))
    native.add((EX.peer, EX.related, EX.root))
    incident = check_rdf_emission(prepare_rdf(native), (declaration,), usages)
    assert any(
        issue.actual == "reverse " + str(EX.related)
        and issue.code == "emission.predicate_undeclared"
        for issue in incident.issues
    )
    output = {
        "@context": {"@vocab": str(EX)},
        "@graph": [
            {
                "@id": str(EX.one),
                "@graph": [{"@id": str(EX.root), "@type": "Record", "label": "Root"}],
            },
            {
                "@id": str(EX.two),
                "@graph": [{"@id": str(EX.root), "@type": "Record", "extra": "Other"}],
            },
        ],
    }
    entity = check_emission(
        output,
        (declaration,),
        (ModelUsage("record", reference=str(EX.root), graph=str(EX.one), entity=True),),
    )
    assert {issue.code for issue in entity.issues} == {
        "emission.source_coverage_unverified"
    }


def test_group_static_projection_preserves_other_field_alias_contract() -> None:
    base = model()
    declaration = replace(
        base,
        fields=(
            *base.fields,
            FieldDeclaration(
                "other",
                str(EX.other),
                (ValueDeclaration("literal", datatype=str(XSD.string)),),
            ),
        ),
        emission_groups=(EmissionGroup("choice", ("label", "other"), maximum=1),),
        coverage=EmissionCoverage(metadata=META, aliases=(("alias", "other"),)),
    )
    graph = Graph().add((EX.Record, RDF.type, OWL.Class))
    for predicate in (EX.label, EX.other):
        graph.add((predicate, RDF.type, OWL.DatatypeProperty))
        graph.add((predicate, RDFS.range, XSD.string))
    assert check_models((declaration,), graph).verified
