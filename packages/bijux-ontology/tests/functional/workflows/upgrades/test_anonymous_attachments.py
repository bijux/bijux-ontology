"""Named RDF attachments scope anonymous changes and consumer uncertainty."""

from rdflib import BNode, Dataset, Graph, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    TermDeclaration,
    ValueDeclaration,
)
from bijux_ontology.execution.limits import Limits
from bijux_ontology.vocabulary.comparison import compare_vocabularies
from bijux_ontology.workflows.upgrades.assessment.checking import assess_upgrade

E = Namespace("urn:attachment:")


def vocabulary(
    label: str = "before",
    filler: URIRef | None = None,
    *,
    shared: bool = False,
    owner: bool = True,
) -> Graph:
    graph = Graph()
    for term, kind in (
        (E.Record, OWL.Class),
        (E.A, OWL.Class),
        (E.B, OWL.Class),
        (E.p, OWL.ObjectProperty),
        (E.q, OWL.ObjectProperty),
        (E.safe, OWL.ObjectProperty),
    ):
        graph.add((term, RDF.type, kind))
    node, head = BNode(label), BNode(label + "-list")
    graph.add((node, OWL.unionOf, head))
    graph.add((head, RDF.first, filler or E.A))
    graph.add((head, RDF.rest, RDF.nil))
    if owner:
        graph.add((E.p, RDFS.range, node))
    if shared:
        graph.add((E.q, RDFS.range, node))
    return graph


def model(*names: str) -> ModelDeclaration:
    return ModelDeclaration(
        "Record",
        (str(E.Record),),
        tuple(
            FieldDeclaration(name, str(E[name]), (ValueDeclaration("iri"),))
            for name in names
        ),
    )


def test_renaming_and_shared_attachments_reach_actual_fields() -> None:
    before, renamed = vocabulary(shared=True), vocabulary("renamed", shared=True)
    same = compare_vocabularies(before, renamed)
    assert same.anonymous is not None
    assert not same.changes and same.anonymous.report.verified
    changed = assess_upgrade(
        before, vocabulary("after", E.B, shared=True), models=(model("p", "q"),)
    )
    assert changed.changes.anonymous is not None
    assert changed.verdict == "unverified"
    assert {
        attachment.owner
        for change in changed.changes.anonymous.changes
        for attachment in change.attachments
    } == {str(E.p), str(E.q)}
    assert {
        impact.use.field
        for impact in changed.anonymous_uses
        if impact.relation == "attachment"
    } == {"p", "q"}
    assert all(impact.use.field != "safe" for impact in changed.anonymous_uses)
    assert all(
        fact.statement[0] is not None
        for change in changed.changes.anonymous.changes
        for fact in change.before
    )


def test_unused_attachments_leave_unrelated_obligations_complete() -> None:
    result = assess_upgrade(
        vocabulary(), vocabulary("after", E.B), models=(model("safe"),)
    )
    assert result.verdict == "informational" and result.report.complete
    assert not result.anonymous_uses
    assert not any(
        i.code == "upgrade.anonymous_impact_unverified" for i in result.report.issues
    )
    unowned = assess_upgrade(
        vocabulary(owner=False),
        vocabulary("after", E.B, owner=False),
        models=(model("safe"),),
    )
    assert unowned.changes.anonymous is not None
    assert unowned.verdict == "unverified"
    assert any(not change.qualified for change in unowned.changes.anonymous.changes)
    # Named fillers are references, not invented owners.
    relevant = assess_upgrade(
        vocabulary(), vocabulary("after", E.B), terms=(TermDeclaration(str(E.A)),)
    )
    assert relevant.changes.anonymous is not None
    assert relevant.verdict == "unverified"
    assert all(
        a.owner != str(E.A)
        for c in relevant.changes.anonymous.changes
        for a in c.attachments
    )
    assert any(i.use.iri == str(E.A) for i in relevant.anonymous_uses)


def test_joint_comparison_preserves_shared_vs_duplicated_topology() -> None:
    before = vocabulary(shared=True)
    after = vocabulary("private")
    other = BNode("other")
    head = BNode("other-list")
    after.add((E.q, RDFS.range, other)).add((other, OWL.unionOf, head))
    after.add((head, RDF.first, E.A)).add((head, RDF.rest, RDF.nil))
    result = compare_vocabularies(before, after)
    assert result.anonymous is not None
    assert any(c.kind == "anonymous" for c in result.changes)
    assert {a.owner for c in result.anonymous.changes for a in c.attachments} == {
        str(E.p),
        str(E.q),
    }


def test_actual_named_graph_move_and_tiny_bounds_remain_visible() -> None:
    before, after = Dataset(), Dataset()
    for triple in vocabulary():
        before.graph(E.G).add(triple)
    for triple in vocabulary("after", E.B):
        after.graph(E.H).add(triple)
    from bijux_ontology.documents.preparation import prepare_source
    from bijux_ontology.rdf.sources.types import RdfSource

    left = prepare_source(RdfSource(before, union=True))
    right = prepare_source(RdfSource(after, union=True))
    result = compare_vocabularies(left, right)
    assert result.anonymous is not None
    assert {a.graph.name for c in result.anonymous.changes for a in c.attachments} == {
        E.G,
        E.H,
    }
    for limits in (Limits(max_work=1), Limits(max_states=1), Limits(max_bytes=1)):
        limited = assess_upgrade(
            vocabulary(), vocabulary("after", E.B), models=(model("p"),), limits=limits
        )
        assert limited.verdict == "unverified" and limited.report.outcome == "exhausted"


def test_used_subproperty_keeps_anonymous_parent_dependency_uncertain() -> None:
    before, after = vocabulary(), vocabulary("after", E.B)
    for graph in (before, after):
        graph.add((E.safe, RDFS.subPropertyOf, E.p))
    result = assess_upgrade(before, after, models=(model("safe"),))
    assert result.verdict == "unverified"
    assert any(
        i.use.field == "safe" and i.relation == "hierarchy" and i.via == (str(E.p),)
        for i in result.anonymous_uses
    )


def test_selected_fact_qualification_requires_full_attachment_coverage() -> None:
    from bijux_ontology.vocabulary.facts.types import FactPredicate, FactSelection

    selection = FactSelection(
        (
            FactPredicate(
                str(RDFS.range), subject_role="property", object_role="class"
            ),
            FactPredicate(
                str(OWL.unionOf), subject_role="class", object_role="structural"
            ),
            FactPredicate(
                str(RDF.first), subject_role="structural", object_role="class"
            ),
            FactPredicate(
                str(RDF.rest), subject_role="structural", object_role="structural"
            ),
        )
    )
    result = assess_upgrade(
        vocabulary(),
        vocabulary("after", E.B),
        models=(model("safe"),),
        fact_selection=selection,
    )
    assert result.verdict == "informational" and result.report.complete
    assert result.changes.selected_facts is not None
    assert result.change_qualification is not None
    assert not result.changes.selected_facts.report.complete
    assert result.change_qualification.complete
    assert any(
        i.code == "vocabulary.fact_attribution_unverified"
        for i in result.changes.selected_facts.report.issues
    )

    # Unchanged selected neighborhoods are part of collected coverage too.
    before, after = vocabulary(), vocabulary("after", E.B)
    for graph in (before, after):
        node = BNode()
        graph.add((E.q, RDFS.range, node)).add((node, OWL.someValuesFrom, E.A))
    mixed = assess_upgrade(
        before, after, models=(model("safe"),), fact_selection=selection
    )
    assert mixed.verdict == "informational" and mixed.report.complete
