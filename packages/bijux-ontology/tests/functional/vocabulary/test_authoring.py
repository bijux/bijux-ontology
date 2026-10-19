"""Native vocabulary edits preserve scope and require real publication contracts."""

import threading
from typing import cast

from rdflib import BNode, Dataset, Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

from bijux_ontology.absence import ABSENT
from bijux_ontology.documents.migrations import export_migration
from bijux_ontology.limits import Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.migrations import MigrationCheck
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.rdf.terms import literal_term, term_key
from bijux_ontology.reports import Report, combine_reports
from bijux_ontology.skos.checking import check_skos
from bijux_ontology.vocabulary.annotations import (
    AnnotationRequirement,
    LanguageRequirement,
    check_annotation_coverage,
)
from bijux_ontology.vocabulary.authoring import (
    AnnotationChange,
    CreateSkosResource,
    DeprecateResource,
    PreferredLabelChange,
    QualifiedAnnotationChange,
    SchemeMembership,
    author_vocabulary,
)
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.obo import read_obo_records

E = Namespace("urn:authoring:")
OIO = Namespace("http://www.geneontology.org/formats/oboInOwl#")
DEFINITION = Namespace("http://purl.obolibrary.org/obo/").IAO_0000115
OLD = Literal("Original", lang="en")
NEW = Literal("Replacement", lang="en")


def _base() -> PreparedSource:
    """One selected concept has independent languages and exact typed metadata."""
    return prepare_rdf(
        Graph()
        .add((E.concept, RDF.type, SKOS.Concept))
        .add((E.scheme, RDF.type, SKOS.ConceptScheme))
        .add((E.concept, SKOS.prefLabel, OLD))
        .add((E.concept, SKOS.prefLabel, Literal("Français", lang="fr")))
        .add((E.note, RDF.type, OWL.AnnotationProperty))
        .add((E.concept, E.note, literal_term("01", datatype=str(XSD.integer))))
    )


def _publication(source: PreparedSource) -> Report:
    """Apply existing SKOS integrity and explicit language/datatype coverage."""
    view = inspect_vocabulary(source.graph())
    return combine_reports(
        "publication",
        check_skos(
            view, concepts=(str(E.concept), str(E.created)), profile="core-integrity"
        ),
        check_annotation_coverage(
            view,
            (
                AnnotationRequirement(
                    str(SKOS.prefLabel), languages=(LanguageRequirement("en"),)
                ),
            ),
            entities=(str(E.concept), str(E.created)),
        ).report,
        check_annotation_coverage(
            view,
            (AnnotationRequirement(str(E.note), datatypes=(str(XSD.integer),)),),
            entities=(str(E.concept),),
        ).report,
    )


def test_create_label_promotion_membership_and_exact_metadata_commit_together() -> None:
    """A repaired publication passes actual checks, preserving unselected languages."""
    source = _base()
    original = set(source.graph())
    result = author_vocabulary(
        source,
        (
            CreateSkosResource(E.created, "concept"),
            PreferredLabelChange(E.created, ABSENT, Literal("Created", lang="en")),
            PreferredLabelChange(E.concept, OLD, NEW),
            SchemeMembership(E.created, E.scheme, top=True),
            AnnotationChange(
                E.concept,
                E.note,
                (literal_term("01", datatype=str(XSD.integer)),),
                (literal_term("001", datatype=str(XSD.integer)),),
            ),
            DeprecateResource(E.concept),
        ),
        before_checks=(MigrationCheck("publication", _publication),),
        after_checks=(MigrationCheck("publication", _publication),),
    )
    assert (
        result.source is not None
        and result.patch is not None
        and result.report.verified
    )
    assert not result.before[0].verified and result.after[0].verified
    assert set(source.graph()) == original
    graph = result.source.graph()
    assert (E.concept, SKOS.prefLabel, NEW) in graph and (
        E.concept,
        SKOS.altLabel,
        OLD,
    ) in graph
    assert (E.concept, SKOS.prefLabel, Literal("Français", lang="fr")) in graph
    assert str(graph.value(E.concept, E.note)) == "001"
    assert (E.created, SKOS.inScheme, E.scheme) in graph
    assert (E.created, SKOS.topConceptOf, E.scheme) in graph and (
        E.scheme,
        SKOS.hasTopConcept,
        E.created,
    ) in graph
    assert graph.value(E.concept, OWL.deprecated) == Literal(True)
    assert export_migration(result, format="turtle").content is None
    assert export_migration(result, accept=True, format="turtle").report.verified
    removed = author_vocabulary(
        result.source,
        (
            SchemeMembership(E.created, E.scheme, member=False),
            DeprecateResource(E.concept, previous=Literal(True), value=False),
        ),
        before_checks=(MigrationCheck("publication", _publication),),
        after_checks=(MigrationCheck("publication", _publication),),
    )
    assert removed.source is not None and removed.report.verified
    revised = removed.source.graph()
    assert (E.created, SKOS.inScheme, E.scheme) not in revised
    assert (E.created, SKOS.topConceptOf, E.scheme) not in revised
    assert (E.scheme, SKOS.hasTopConcept, E.created) not in revised
    assert revised.value(E.concept, OWL.deprecated) == Literal(False)


def test_second_preferred_label_fails_actual_integrity_and_cannot_export() -> None:
    """A structurally valid candidate is not an approved partial publication."""
    source = _base()
    before = cast(
        tuple[Literal, ...], tuple(source.graph().objects(E.concept, SKOS.prefLabel))
    )
    result = author_vocabulary(
        source,
        (AnnotationChange(E.concept, SKOS.prefLabel, before, (*before, NEW)),),
        annotation_predicates=(SKOS.prefLabel,),
        before_checks=(
            MigrationCheck("integrity", lambda value: check_skos(value.graph())),
        ),
        after_checks=(
            MigrationCheck("integrity", lambda value: check_skos(value.graph())),
        ),
    )
    assert result.source is not None and result.after[0].outcome == "disagreement"
    assert (
        not result.report.verified
        and export_migration(result, accept=True, format="turtle").content is None
    )
    assert tuple(source.graph().objects(E.concept, SKOS.prefLabel)) == before


def _obo_contract(expected: int) -> MigrationCheck:
    """Read actual owned OBO records and compare the selected attachment count."""

    def check(source: PreparedSource) -> Report:
        records = read_obo_records(inspect_vocabulary(source.graph()), str(E.concept))
        from bijux_ontology.reports import Issue

        return Report(
            "obo-attachments",
            1,
            ()
            if len(records.attachments) == expected
            else (Issue("test.attachment_count", "Actual attachment count differs"),),
            children=(records.report,),
        )

    return MigrationCheck("obo-records", check)


def test_qualified_add_remove_preserves_other_attachments_and_opaque_values() -> None:
    """Exact OBO anchors retain independent attachments and qualifier bodies."""
    source = _base()
    text = Literal("Definition", lang="en")
    add = QualifiedAnnotationChange(
        E.concept, DEFINITION, text, ((OIO.hasDbXref, Literal("DB:1")),)
    )
    result = author_vocabulary(
        source,
        (add,),
        annotation_predicates=(OIO.hasDbXref,),
        before_checks=(_obo_contract(0),),
        after_checks=(_obo_contract(1),),
    )
    assert result.source is not None and result.report.verified
    original = result.source
    graph = original.graph()
    extra = BNode()
    body = BNode()
    for triple in (
        (extra, RDF.type, OWL.Axiom),
        (extra, OWL.annotatedSource, E.concept),
        (extra, OWL.annotatedProperty, DEFINITION),
        (extra, OWL.annotatedTarget, text),
        (extra, OIO.hasDbXref, body),
        (body, RDFS.label, Literal("opaque")),
    ):
        graph.add(triple)
    source = prepare_rdf(graph)
    graph = source.graph()
    attachments = tuple(graph.subjects(OWL.annotatedSource, E.concept))
    selected = next(
        node
        for node in attachments
        if any(
            term_key(value) == term_key(Literal("DB:1"))
            for value in graph.objects(node, OIO.hasDbXref)
        )
    )
    remove = QualifiedAnnotationChange(
        E.concept,
        DEFINITION,
        text,
        ((OIO.hasDbXref, Literal("DB:1")),),
        action="remove",
        attachment=cast(BNode, selected),
    )
    removed = author_vocabulary(
        source,
        (remove,),
        annotation_predicates=(OIO.hasDbXref,),
        before_checks=(_obo_contract(2),),
        after_checks=(_obo_contract(1),),
    )
    assert removed.source is not None and removed.report.verified
    graph = removed.source.graph()
    assert (E.concept, DEFINITION, text) in graph
    assert len(tuple(graph.subjects(OWL.annotatedSource, E.concept))) == 1
    assert any(str(value) == "opaque" for value in graph.objects(None, RDFS.label))
    assert len(tuple(source.graph().subjects(OWL.annotatedSource, E.concept))) == 2


def test_shared_qualified_delete_and_reserved_punning_are_rejected_atomically() -> None:
    """Neither shared annotation bodies nor structural property roles are erased."""
    node = BNode()
    text = Literal("Definition", lang="en")
    graph = _base().graph()
    for triple in (
        (E.concept, DEFINITION, text),
        (node, RDF.type, OWL.Axiom),
        (node, OWL.annotatedSource, E.concept),
        (node, OWL.annotatedProperty, DEFINITION),
        (node, OWL.annotatedTarget, text),
        (node, OIO.hasDbXref, Literal("DB:1")),
        (E.unselected, E.uses, node),
    ):
        graph.add(triple)
    source = prepare_rdf(graph)
    owned = cast(BNode, source.graph().value(E.unselected, E.uses))
    edits = (
        PreferredLabelChange(E.concept, OLD, NEW),
        QualifiedAnnotationChange(
            E.concept,
            DEFINITION,
            text,
            ((OIO.hasDbXref, Literal("DB:1")),),
            action="remove",
            attachment=owned,
        ),
    )
    result = author_vocabulary(source, edits, annotation_predicates=(OIO.hasDbXref,))
    assert result.source is None and not result.report.verified
    assert (E.concept, SKOS.prefLabel, OLD) in source.graph()
    foreign = author_vocabulary(
        source,
        (
            QualifiedAnnotationChange(
                E.concept,
                DEFINITION,
                text,
                ((OIO.hasDbXref, Literal("DB:1")),),
                action="remove",
                attachment=BNode(str(owned)),
            ),
        ),
        annotation_predicates=(OIO.hasDbXref,),
    )
    assert foreign.source is None
    for predicate in (
        RDF.type,
        RDF.first,
        RDF.rest,
        OWL.annotatedSource,
        RDFS.subClassOf,
        SKOS.broader,
    ):
        graph = _base().graph()
        graph.add((predicate, RDF.type, OWL.AnnotationProperty))
        prepared = prepare_rdf(graph)
        result = author_vocabulary(
            prepared,
            (AnnotationChange(E.concept, predicate, (), (E.other,)),),
            annotation_predicates=(predicate,),
        )
        assert result.source is None and not result.report.verified

    graph = _base().graph()
    graph.add((DEFINITION, RDF.type, OWL.ObjectProperty))
    rejected = author_vocabulary(
        prepare_rdf(graph),
        (QualifiedAnnotationChange(E.concept, DEFINITION, text, ()),),
    )
    assert rejected.source is None and not rejected.report.verified


def test_physical_graph_before_sets_limits_and_missing_contracts_stay_explicit() -> (
    None
):
    """All edits share one authorized graph and a required actual after-check scope."""
    graph = _base().graph()
    dataset = Dataset()
    for triple in graph:
        dataset.graph(E.selected).add(triple)
    dataset.graph(E.hidden).add(
        (E.concept, SKOS.prefLabel, Literal("Hidden", lang="de"))
    )
    source = prepare_rdf(dataset)
    absent = author_vocabulary(source, (PreferredLabelChange(E.concept, OLD, NEW),))
    assert absent.source is None
    result = author_vocabulary(
        source, (PreferredLabelChange(E.concept, OLD, NEW),), graph=E.selected
    )
    assert result.source is not None and not result.report.verified
    assert (
        E.concept,
        SKOS.prefLabel,
        Literal("Hidden", lang="de"),
    ) in result.source.graph(E.hidden)
    assert export_migration(result, accept=True, format="turtle").content is None
    pinned = prepare_rdf(RdfSource(dataset, graph_name=str(E.hidden)))
    assert (
        author_vocabulary(
            pinned, (PreferredLabelChange(E.concept, OLD, NEW),), graph=E.selected
        ).source
        is None
    )
    wrong = author_vocabulary(
        _base(), (AnnotationChange(E.concept, E.note, (), (Literal("wrong"),)),)
    )
    assert wrong.source is None
    cancelled = threading.Event()
    cancelled.set()
    for result in (
        author_vocabulary(
            _base(),
            (PreferredLabelChange(E.concept, OLD, NEW),),
            limits=Limits(max_work=10),
        ),
        author_vocabulary(
            _base(), (PreferredLabelChange(E.concept, OLD, NEW),), cancel=cancelled
        ),
    ):
        assert result.report.outcome == "exhausted" and result.source is None
