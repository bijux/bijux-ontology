"""Selected facts retain exact scope and per-position application use roles."""

from rdflib import Dataset, Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

from bijux_ontology.declarations.resource_roles import ResourceRole
from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.execution.limits import Limits
from bijux_ontology.models.resource_roles import RoleUse
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.vocabulary.comparison import compare_vocabularies
from bijux_ontology.vocabulary.facts import FactGraph, FactPredicate, FactSelection
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary
from bijux_ontology.workflows.upgrades.assessment import assess_upgrade

EX = Namespace("urn:fact-impact:")


def _punned() -> Graph:
    """Establish separate class and individual facets for the same identifier."""
    return (
        Graph()
        .add((EX.C, RDF.type, OWL.Class))
        .add((EX.C, RDF.type, SKOS.Concept))
        .add((EX.C, SKOS.inScheme, EX.old))
    )


def test_individual_scheme_and_class_hierarchy_changes_keep_distinct_uses() -> None:
    before = _punned()
    after = Graph() + before
    after.set((EX.C, SKOS.inScheme, EX.new))
    after.add((EX.C, RDFS.subClassOf, EX.Parent))
    selection = FactSelection(
        (FactPredicate(str(SKOS.inScheme)), FactPredicate(str(RDFS.subClassOf)))
    )
    uses = (
        RoleUse(EX.C, ResourceRole("class", punning="allow"), "class-reference"),
        RoleUse(EX.C, ResourceRole("instance", punning="allow"), "instance-reference"),
    )
    result = assess_upgrade(
        before,
        after,
        models=(ModelDeclaration("consumer", (str(EX.C),), ()),),
        role_uses=uses,
        fact_selection=selection,
    )
    assert result.verdict == "informational", result.report.to_dict()
    assert result.changes.selected_facts is not None
    assert len(result.changes.selected_facts.differences) == 3
    scheme = [
        item
        for item in result.fact_uses
        if item.difference.fact.statement[1] == SKOS.inScheme
    ]
    assert scheme and all(item.use.role == "individual" for item in scheme)
    hierarchy = [
        item
        for item in result.fact_uses
        if item.difference.fact.statement[1] == RDFS.subClassOf
    ]
    assert hierarchy and all(item.use.role == "class" for item in hierarchy)
    assert all(
        item.use.role == "individual"
        for item in result.affected
        if item.change.predicate == str(SKOS.inScheme)
    )
    assert any(
        item.use.role == "class"
        for item in result.affected
        if item.change.predicate == str(RDFS.subClassOf)
    )
    # Both positions of a self-type assertion retain their actual different roles.
    after = Graph() + before
    after.add((EX.C, RDF.type, EX.C))
    self_type = assess_upgrade(
        before,
        after,
        role_uses=uses,
        fact_selection=FactSelection((FactPredicate(str(RDF.type)),)),
    )
    exact = [
        item
        for item in self_type.fact_uses
        if item.difference.fact.statement == (EX.C, RDF.type, EX.C)
    ]
    assert {(item.occurrence.position, item.use.role) for item in exact} == {
        ("subject", "individual"),
        ("object", "class"),
    }
    # Class hierarchy propagation must not reintroduce a rejected datatype facet.
    datatype_before = (
        Graph()
        .add((EX.C, RDF.type, OWL.Class))
        .add((EX.C, RDF.type, RDFS.Datatype))
        .add((EX.M, RDF.type, OWL.Class))
        .add((EX.p, RDF.type, OWL.DatatypeProperty))
    )
    datatype_after = Graph() + datatype_before
    datatype_after.add((EX.C, RDFS.subClassOf, EX.Parent))
    model = ModelDeclaration(
        "M",
        (str(EX.M),),
        (
            FieldDeclaration(
                "value", str(EX.p), (ValueDeclaration("literal", datatype=str(EX.C)),)
            ),
        ),
    )
    datatype = assess_upgrade(
        datatype_before,
        datatype_after,
        models=(model,),
        fact_selection=FactSelection((FactPredicate(str(RDFS.subClassOf)),)),
    )
    assert not any(item.use.role == "datatype" for item in datatype.affected)
    assert not any(item.use.role == "datatype" for item in datatype.fact_uses)


def test_custom_selection_threads_through_comparison_and_upgrade_without_guessing() -> (
    None
):
    before = (
        Graph()
        .add((EX.C, RDF.type, OWL.Class))
        .add((EX.C, EX.custom, Literal("01", datatype=XSD.integer, normalize=False)))
    )
    after = (
        Graph()
        .add((EX.C, RDF.type, OWL.Class))
        .add((EX.C, EX.custom, Literal("1", datatype=XSD.integer, normalize=False)))
    )
    assert not compare_vocabularies(before, after).changes
    selected = FactSelection((FactPredicate(str(EX.custom), subject_role="class"),))
    compared = compare_vocabularies(before, after, fact_selection=selected)
    assert compared.selected_facts is not None
    assert {
        str(item.fact.statement[2]) for item in compared.selected_facts.differences
    } == {"01", "1"}
    use = (RoleUse(EX.C, ResourceRole("class"), "custom-reference"),)
    upgraded = assess_upgrade(before, after, role_uses=use, fact_selection=selected)
    assert upgraded.verdict == "informational"
    assert len(upgraded.fact_uses) == 2
    assert all(
        item.occurrence.position == "subject" and item.relation == "direct"
        for item in upgraded.fact_uses
    )
    unknown = assess_upgrade(
        before,
        after,
        role_uses=use,
        fact_selection=FactSelection((FactPredicate(str(EX.custom)),)),
    )
    assert unknown.verdict == "unverified"
    assert unknown.fact_uses and all(
        item.relation == "uncertain" for item in unknown.fact_uses
    )
    # A policy for a selected lookup entity cannot qualify an unrelated local use.
    lookup = (
        Graph().add((EX.A, RDF.type, OWL.Class)).add((EX.A, RDFS.label, Literal("A")))
    )
    local = (
        Graph()
        .add((EX.B, RDF.type, OWL.NamedIndividual))
        .add((EX.B, RDFS.label, Literal("old")))
    )
    changed = Graph() + local
    changed.set((EX.B, RDFS.label, Literal("new")))
    outside = assess_upgrade(
        load_vocabulary(local, lookup=(lookup,)),
        load_vocabulary(changed, lookup=(lookup,)),
        role_uses=(RoleUse(EX.B, ResourceRole("instance"), "local-instance"),),
        fact_selection=FactSelection(
            (FactPredicate(str(RDFS.label), subject_role="class"),),
            entities=(str(EX.A),),
            positions=("subject",),
            scopes=("lookup",),
        ),
    )
    assert outside.verdict == "unverified"
    assert not outside.fact_uses
    assert any(
        issue.code == "upgrade.fact_impact_unverified"
        for issue in outside.report.issues
    )

    # A qualified graph cannot erase a changed, unqualified sibling in one aggregate.
    def snapshot(class_label: str, other_label: str) -> Vocabulary:
        """Retain both named graph roles in one supplied semantic vocabulary."""
        data = Dataset()
        data.graph(EX.class_graph).add((EX.B, RDF.type, OWL.Class)).add(
            (EX.B, RDFS.label, Literal(class_label))
        )
        data.graph(EX.other_graph).add((EX.B, RDF.type, OWL.NamedIndividual)).add(
            (EX.B, RDFS.label, Literal(other_label))
        )
        return load_vocabulary(RdfSource(data, union=True))

    mixed_before, mixed_after = (
        snapshot("class-old", "other-old"),
        snapshot("class-new", "other-new"),
    )
    roles = (
        RoleUse(EX.B, ResourceRole("instance", punning="allow"), "instance"),
        RoleUse(EX.B, ResourceRole("class", punning="allow"), "class"),
    )
    mixed = assess_upgrade(
        mixed_before,
        mixed_after,
        role_uses=roles,
        fact_selection=FactSelection(
            (FactPredicate(str(RDFS.label), subject_role="class"),),
            graphs=(FactGraph("named", EX.class_graph),),
        ),
    )
    assert mixed.verdict == "unverified"
    assert any(item.use.role == "class" for item in mixed.fact_uses)
    assert not any(item.use.role == "individual" for item in mixed.fact_uses)
    assert any(
        issue.code == "upgrade.fact_impact_unverified" for issue in mixed.report.issues
    )
    full = assess_upgrade(
        mixed_before,
        mixed_after,
        role_uses=roles,
        fact_selection=FactSelection(
            (FactPredicate(str(RDFS.label), subject_role="class"),)
        ),
    )
    assert full.verdict == "informational", full.report.to_dict()


def test_shared_workflow_limits_preserve_unknown_coverage_instead_of_removals() -> None:
    before, after = _punned(), _punned()
    after.set((EX.C, SKOS.inScheme, EX.new))
    selection = FactSelection((FactPredicate(str(SKOS.inScheme)),))
    use = (RoleUse(EX.C, ResourceRole("instance", punning="allow"), "instance"),)
    for limits in (
        Limits(max_resources=1),
        Limits(max_bytes=1),
        Limits(max_work=1),
        Limits(max_work=10),
        Limits(max_work=100),
    ):
        result = assess_upgrade(
            before, after, role_uses=use, fact_selection=selection, limits=limits
        )
        assert result.report.outcome == "exhausted"
        assert not result.report.complete
        assert not result.changes.changes
