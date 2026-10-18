"""Finite selected publication conventions reuse actual native term evidence."""

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology import prepare_source
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.vocabulary.annotations.types import AnnotationRequirement
from bijux_ontology.vocabulary.facts.types import FactPredicate
from bijux_ontology.workflows.ontology.obo import (
    OboPublicationProfile,
    check_obo_publication,
)
from bijux_ontology.workflows.upgrades.lifecycle.types import (
    HistoryCoverage,
    LifecyclePolicy,
    Release,
)

EX = Namespace("https://example.org/publication/")
OIO = Namespace("http://www.geneontology.org/formats/oboInOwl#")
DEFINITION = "http://purl.obolibrary.org/obo/IAO_0000115"


def release(label: str = "current") -> Release:
    """One real OBO-in-OWL release with separately qualified text assertions."""
    return Release(
        label,
        prepare_source(
            """
            @prefix e:<https://example.org/publication/>.
            @prefix owl:<http://www.w3.org/2002/07/owl#>.
            @prefix rdfs:<http://www.w3.org/2000/01/rdf-schema#>.
            @prefix oio:<http://www.geneontology.org/formats/oboInOwl#>.
            e:T a owl:Class; rdfs:label "record";
                <http://purl.obolibrary.org/obo/IAO_0000115> "A record.";
                oio:hasExactSynonym "entry"; oio:inSubset e:basic.
            [] a owl:Axiom; owl:annotatedSource e:T;
                owl:annotatedProperty <http://purl.obolibrary.org/obo/IAO_0000115>;
                owl:annotatedTarget "A record."; oio:hasDbXref "PMID:123".
            [] a owl:Axiom; owl:annotatedSource e:T;
                owl:annotatedProperty oio:hasExactSynonym;
                owl:annotatedTarget "entry"; oio:hasSynonymType e:preferred.
            e:basic a owl:AnnotationProperty;
                rdfs:subPropertyOf oio:SubsetProperty; rdfs:comment "Core terms".
            """,
            format="turtle",
        ),
    )


def profile(
    *,
    lifecycle: LifecyclePolicy | None = None,
    exclude_terms: tuple[str, ...] = (),
    exclude_namespaces: tuple[str, ...] = (),
) -> OboPublicationProfile:
    """A caller-authored selection; no package-wide publication defaults."""
    return OboPublicationProfile(
        annotations=(AnnotationRequirement(DEFINITION),),
        record_annotations=True,
        definition_xrefs=True,
        synonym_types=(str(EX.preferred),),
        subset_membership=True,
        lifecycle=lifecycle,
        exclude_terms=exclude_terms,
        exclude_namespaces=exclude_namespaces,
    )


def test_selected_release_qualification_and_native_subset_evidence() -> None:
    current = release()
    before = set(current.source.graph())
    result = check_obo_publication(current, (EX.T,), profile=profile())
    assert result.report.verified and result.records and result.subsets is not None
    assert result.records[0].records[0].assertion.scope == "local"
    assert result.subsets.definitions[0].iri == EX.basic
    assert any(edge.subject == EX.T for edge in result.subset_evidence)
    assert set(current.source.graph()) == before
    previous = release("previous")
    interval = check_obo_publication(
        current,
        (EX.T,),
        profile=profile(lifecycle=LifecyclePolicy()),
        history=(previous,),
        coverage=HistoryCoverage(("previous", "current"), True),
    )
    assert interval.report.verified and interval.lifecycle is not None


def test_malformed_synonym_and_retired_logical_reuse_are_located() -> None:
    old = release("retired")
    graph = old.source.graph()
    graph.add((EX.T, OWL.deprecated, Literal(True)))
    old = Release("retired", prepare_source(graph))
    graph = release().source.graph()
    graph.add((EX.T, RDFS.subClassOf, EX.Other))
    for node in graph.subjects(OWL.annotatedProperty, OIO.hasExactSynonym):
        graph.set((node, OIO.hasSynonymType, Literal("not-a-type-IRI")))
    current = Release("current", prepare_source(graph))
    result = check_obo_publication(
        current,
        (EX.T,),
        profile=profile(
            lifecycle=LifecyclePolicy(reserved=(FactPredicate(str(RDFS.subClassOf)),))
        ),
        history=(old,),
        coverage=HistoryCoverage(("retired", "current"), True),
    )
    assert not result.report.verified and result.lifecycle is not None
    codes = {issue.code for issue in result.report.issues}
    assert "obo.synonym_type_shape" in codes
    assert "upgrade.lifecycle_violation" in codes
    assert all(
        issue.location == str(EX.T)
        for issue in result.report.issues
        if issue.code in {"obo.synonym_type_shape", "upgrade.lifecycle_violation"}
    )


def test_import_exclusion_unselected_recommendations_and_non_obo_input() -> None:
    graph = release().source.graph()
    graph.add((EX.Ontology, OWL.imports, EX.Support))
    support = Graph().add((EX.Imported, RDF.type, OWL.Class))
    support.add((EX.Imported, OIO.inSubset, EX.Undefined))
    current = Release(
        "current", prepare_source(graph), imports={str(EX.Support): support}
    )
    result = check_obo_publication(
        current,
        (EX.T, EX.Imported),
        profile=profile(exclude_terms=(str(EX.Imported),)),
    )
    assert result.report.verified and result.excluded == (EX.Imported,)
    assert all(edge.subject != EX.Imported for edge in result.subset_evidence)
    ordinary = Release(
        "ordinary",
        prepare_source(Graph().add((EX.T, RDFS.label, Literal("ordinary")))),
    )
    selected = OboPublicationProfile(
        annotations=(AnnotationRequirement(str(RDFS.label)),)
    )
    assert check_obo_publication(ordinary, (EX.T,), profile=selected).report.verified
    disabled = check_obo_publication(ordinary, (EX.T,), profile=OboPublicationProfile())
    assert disabled.report.outcome == "not_applicable"
    assert not disabled.report.verified
    unqualified = release().source.graph()
    unqualified.remove((None, OIO.hasDbXref, None))
    unqualified.remove((None, OIO.hasSynonymType, None))
    unselected = check_obo_publication(
        Release("current", prepare_source(unqualified)),
        (EX.T,),
        profile=OboPublicationProfile(record_annotations=True),
    )
    assert unselected.report.verified


def test_definition_sources_are_not_borrowed_and_closed_subset_is_selected() -> None:
    graph = release().source.graph()
    for node in graph.subjects(OWL.annotatedProperty, URIRef(DEFINITION)):
        graph.remove((node, OIO.hasDbXref, None))
    # Other annotations never provide a missing definition's qualification.
    graph.add((EX.T, OIO.hasDbXref, Literal("PMID:borrowed")))
    graph.add((EX.T, OIO.inSubset, EX.Undefined))
    result = check_obo_publication(
        Release("current", prepare_source(graph)), (EX.T,), profile=profile()
    )
    assert not result.report.verified
    codes = {issue.code for issue in result.report.issues}
    assert "obo.publication_definition_xref" in codes
    assert "obo.subset_undefined" in codes


def test_missing_scope_open_history_and_cumulative_budget_do_not_verify() -> None:
    current = release()
    missing = check_obo_publication(current, (EX.Absent,), profile=profile())
    assert not missing.report.verified
    excluded = check_obo_publication(
        current,
        (EX.T,),
        profile=profile(exclude_namespaces=(str(EX),)),
    )
    assert not excluded.report.verified and excluded.excluded == (EX.T,)
    history = check_obo_publication(
        current, (EX.T,), profile=profile(lifecycle=LifecyclePolicy())
    )
    assert not history.report.verified and history.lifecycle is not None
    exhausted = check_obo_publication(
        current,
        (EX.T,),
        profile=profile(),
        budget=Budget(Limits(max_work=1)),
    )
    assert exhausted.report.outcome == "exhausted"


def test_imported_annotations_cannot_fill_owned_term_requirements() -> None:
    graph = release().source.graph()
    graph.add((EX.Ontology, OWL.imports, EX.Support))
    graph.remove((EX.T, URIRef(DEFINITION), None))
    support = release().source.graph()
    # Imported support still supplies a referenced subset definition.
    graph.remove((EX.basic, None, None))
    current = Release(
        "current", prepare_source(graph), imports={str(EX.Support): support}
    )
    result = check_obo_publication(current, (EX.T,), profile=profile())
    assert not result.report.verified and result.annotations is not None
    assert any(
        issue.code == "annotations.minimum_missing" for issue in result.report.issues
    )
    assert result.subsets is not None and result.subsets.report.verified
    assert any(edge.scope.startswith("import[") for edge in result.subset_evidence)


def test_publication_and_lifecycle_share_exact_per_release_import_bindings() -> None:
    graph = release().source.graph()
    support = Graph()
    for triple in tuple(graph.triples((EX.basic, None, None))):
        support.add(triple)
    graph.remove((EX.basic, None, None))
    graph.add((EX.Ontology, OWL.imports, EX.Support))
    old_support = Graph()
    for triple in support:
        old_support.add(triple)
    old_support.set((EX.basic, RDFS.comment, Literal("Earlier subset description")))
    previous = Release(
        "previous", prepare_source(graph), imports={str(EX.Support): old_support}
    )
    current = Release(
        "current", prepare_source(graph), imports={str(EX.Support): support}
    )
    result = check_obo_publication(
        current,
        (EX.T,),
        profile=profile(lifecycle=LifecyclePolicy()),
        history=(previous,),
        coverage=HistoryCoverage(("previous", "current"), True),
    )
    assert result.report.verified and result.release is current
    assert result.lifecycle is not None
    assert result.lifecycle.observations[-1].release is current
    assert result.lifecycle.observations[0].release is previous
    assert result.subsets is not None and result.subsets.report.verified
    missing = check_obo_publication(
        Release("missing", current.source),
        (EX.T,),
        profile=profile(lifecycle=LifecyclePolicy()),
        history=(previous,),
        coverage=HistoryCoverage(("previous", "missing"), True),
    )
    assert missing.report.outcome == "unverified" and not missing.report.verified
    assert any(i.location == str(EX.Support) for i in missing.report.issues)
