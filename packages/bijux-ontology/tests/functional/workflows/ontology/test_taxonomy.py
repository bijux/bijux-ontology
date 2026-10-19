"""Actual named taxonomy publications preserve identity and explicit choices."""

import threading
from dataclasses import replace

import pytest
from rdflib import BNode, Dataset, Graph, Namespace, URIRef
from rdflib import Literal as RdfLiteral
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.migrations.candidates import MigrationCheck
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.skos.checking import check_skos
from bijux_ontology.workflows.ontology.taxonomy import (
    ClassConcept,
    PreferredClassLabel,
    TaxonomyProjection,
    TaxonomyPublication,
    project_taxonomy,
)
from bijux_ontology.workflows.upgrades.plans.exporting import export_migration_plan

E = Namespace("urn:taxonomy:")
CLASSES = tuple(map(str, (E.root, E.middle, E.leaf)))


def ontology() -> Graph:
    """Supply a real three-level OWL hierarchy with multilingual exact literals."""
    graph = Graph()
    for node, text in ((E.root, "Root"), (E.middle, "Middle"), (E.leaf, "Leaf")):
        graph.add((node, RDF.type, OWL.Class))
        graph.add((node, RDFS.label, RdfLiteral(text, lang="en")))
        graph.add((node, RDFS.label, RdfLiteral(text + " français", lang="fr")))
    graph.add((E.middle, RDFS.subClassOf, E.root))
    graph.add((E.leaf, RDFS.subClassOf, E.middle))
    graph.add(
        (E.leaf, E.description, RdfLiteral("01", datatype=XSD.integer, normalize=False))
    )
    return graph


def requirements(
    concepts: tuple[str, ...], *, languages: tuple[str, ...] = ("en", "fr")
) -> tuple[MigrationCheck, ...]:
    """Use existing actual SKOS consumers on the generated graph, not fixtures."""
    return (
        MigrationCheck(
            "SKOS consumer",
            lambda source: check_skos(
                source.graph(),
                concepts=concepts,
                rules=("resource-integrity", "label-integrity", "language-coverage"),
                languages=languages,
            ),
        ),
    )


def policy() -> TaxonomyProjection:
    """Select deliberate same-IRI punning and actual consumer requirements."""
    return TaxonomyProjection(
        str(E.scheme),
        CLASSES,
        identity="same-iri-punning",
        definition_predicates=(str(E.description),),
        checks=requirements(CLASSES),
    )


def project(
    source: PreparedSource, specification: TaxonomyProjection | None = None
) -> TaxonomyPublication:
    """Exercise the public asserted-hierarchy convenience with owned snapshots."""
    return project_taxonomy(
        source, source, specification or policy(), hierarchy_basis="asserted"
    )


def test_three_level_separate_identity_and_portable_publication() -> None:
    """Separate concepts keep exact source bindings, roots, language and definitions."""
    source = prepare_rdf(ontology())
    concepts = tuple(map(str, (E.rootConcept, E.middleConcept, E.leafConcept)))
    specification = replace(
        policy(),
        identity="separate-iri",
        class_mappings=tuple(
            ClassConcept(old, new) for old, new in zip(CLASSES, concepts, strict=True)
        ),
        source_class_predicate=str(E.sourceClass),
        checks=requirements(concepts),
    )
    result = project(source, specification)
    assert result.report.verified, result.report.to_dict()
    assert result.publication is not None
    assert result.publication.source is not None
    graph = result.publication.source.graph()
    assert set(graph.subjects(SKOS.topConceptOf, E.scheme)) == {E.rootConcept}
    assert set(graph.subject_objects(SKOS.broader)) == {
        (E.middleConcept, E.rootConcept),
        (E.leafConcept, E.middleConcept),
    }
    assert set(graph.subjects(SKOS.inScheme, E.scheme)) == set(map(URIRef, concepts))
    assert str(graph.value(E.leafConcept, SKOS.definition)) == "01"
    for binding in result.bindings:
        assert (
            URIRef(binding.concept_iri),
            E.sourceClass,
            URIRef(binding.class_iri),
        ) in graph
    exported = export_migration_plan(result.publication, accept=True, format="turtle")
    assert exported.report.verified and exported.content is not None
    parsed = prepare_rdf(exported.content, format="turtle")
    assert requirements(concepts)[0].check(parsed).verified
    assert set(source.graph()) == set(ontology())


def test_same_iri_punning_never_generates_instance_hierarchy() -> None:
    """Per-position witnesses preserve C-a-C without joining independent roles."""
    graph = ontology()
    graph.add((E.leaf, RDF.type, E.leaf))
    graph.add((E.leaf, E.instanceProperty, E.unrelated))
    source = prepare_rdf(graph)
    result = project(source)
    assert result.report.verified, result.report.to_dict()
    assert result.publication is not None and result.publication.source is not None
    published = result.publication.source.graph()
    assert (E.leaf, RDF.type, SKOS.Concept) in published
    assert (E.leaf, RDF.type, OWL.Class) in published
    assert (E.leaf, SKOS.broader, E.middle) in published
    assert (E.leaf, RDF.type, E.middle) not in published
    assert (E.leaf, RDF.type, E.leaf) not in published
    assert (E.leaf, E.instanceProperty, E.unrelated) not in published
    occurrences = [
        item for item in result.roles if item.statement == (E.leaf, RDF.type, E.leaf)
    ]
    assert {(item.position, item.role) for item in occurrences} == {
        ("subject", "individual"),
        ("object", "class"),
    }
    assert (E.leaf, RDF.type, E.leaf) in source.graph()


def test_exact_label_choice_and_requested_expression_omission() -> None:
    """Conflicting labels/anonymous hierarchy block until each choice is explicit."""
    graph = ontology()
    selected = RdfLiteral("Other", lang="en")
    graph.add((E.root, RDFS.label, selected))
    expression = BNode()
    graph.add((E.leaf, RDFS.subClassOf, expression))
    graph.add((expression, RDF.type, OWL.Restriction))
    graph.add((expression, OWL.onProperty, E.someProperty))
    graph.add((expression, OWL.someValuesFrom, E.filler))
    source = prepare_rdf(graph)
    rejected = project(source)
    assert rejected.publication is None
    assert {"taxonomy.label_ambiguous", "taxonomy.expression_unrepresented"} <= {
        issue.code for issue in rejected.report.issues
    }
    selected_policy = replace(
        policy(),
        expressions="omit",
        preferred_labels=(PreferredClassLabel(str(E.root), selected),),
    )
    accepted = project(source, selected_policy)
    assert accepted.report.verified, accepted.report.to_dict()
    assert accepted.publication is not None and accepted.publication.source is not None
    output = accepted.publication.source.graph()
    assert selected in set(output.objects(E.root, SKOS.prefLabel))
    assert (E.leaf, SKOS.broader, E.filler) not in output
    assert not list(output.triples((None, E.someProperty, None)))
    assert {item.reason for item in accepted.exclusions} == {
        "label-choice",
        "expression",
    }
    invented = project(
        source,
        replace(
            selected_policy,
            preferred_labels=(
                PreferredClassLabel(str(E.root), RdfLiteral("Invented", lang="en")),
            ),
        ),
    )
    assert invented.publication is None
    assert "taxonomy.label_choice_invalid" in {
        issue.code for issue in invented.report.issues
    }
    equivalent = ontology()
    equivalent.add((E.leaf, OWL.equivalentClass, expression))
    equivalent.add((expression, RDF.type, OWL.Restriction))
    equivalent.add((expression, OWL.onProperty, E.someProperty))
    equivalent.add((expression, OWL.someValuesFrom, E.filler))
    equivalent_source = prepare_rdf(equivalent)
    unsupported = project(equivalent_source)
    assert unsupported.publication is None
    assert "taxonomy.expression_unrepresented" in {
        issue.code for issue in unsupported.report.issues
    }
    omitted = project(equivalent_source, replace(policy(), expressions="omit"))
    assert omitted.report.verified and omitted.publication is not None
    assert any(
        item.statement.predicate == OWL.equivalentClass for item in omitted.exclusions
    )
    for predicate in (OWL.equivalentClass, OWL.disjointWith):
        reverse = ontology()
        reverse.add((expression, predicate, E.leaf))
        reverse.add((expression, RDF.type, OWL.Restriction))
        reverse_source = prepare_rdf(reverse)
        rejected_reverse = project(reverse_source)
        assert rejected_reverse.publication is None
        assert "taxonomy.expression_unrepresented" in {
            issue.code for issue in rejected_reverse.report.issues
        }
        omitted_reverse = project(reverse_source, replace(policy(), expressions="omit"))
        assert omitted_reverse.report.verified
        assert any(
            isinstance(item.statement.subject, BNode)
            and item.statement.predicate == predicate
            and item.statement.object == E.leaf
            for item in omitted_reverse.exclusions
        )


def test_supplied_hierarchy_and_pinned_source_boundaries() -> None:
    """Supplied inferred edges differ without borrowing hidden graphs or reasoning."""
    source = prepare_rdf(ontology())
    inferred = ontology()
    inferred.add((E.leaf, RDFS.subClassOf, E.root))
    inferred.add((E.unselected, RDFS.subClassOf, E.root))
    result = project_taxonomy(
        source, prepare_rdf(inferred), policy(), hierarchy_basis="supplied-inferred"
    )
    assert result.report.verified, result.report.to_dict()
    assert result.publication is not None and result.publication.source is not None
    assert (E.leaf, SKOS.broader, E.root) in result.publication.source.graph()
    assert (E.leaf, RDFS.subClassOf, E.root) not in source.graph()
    dataset = Dataset()
    for triple in ontology():
        dataset.graph(E.selected).add(triple)
    dataset.graph(E.hidden).add((E.root, RDFS.label, RdfLiteral("Conflict", lang="en")))
    dataset.graph(E.hidden).add((E.leaf, RDFS.subClassOf, E.root))
    pinned = prepare_rdf(dataset)
    pinned.graph_name = str(E.selected)
    scoped = project(pinned)
    assert scoped.report.verified, scoped.report.to_dict()
    assert scoped.publication is not None and scoped.publication.source is not None
    assert (E.leaf, SKOS.broader, E.root) not in scoped.publication.source.graph()
    unpinned = prepare_rdf(dataset)
    assert project(unpinned).publication is None
    outside = ontology()
    outside.add((E.root, RDFS.subClassOf, E.outside))
    assert project(prepare_rdf(outside)).publication is None
    omitted = project(prepare_rdf(outside), replace(policy(), outside_parents="omit"))
    assert omitted.report.verified and omitted.exclusions[0].reason == "outside-parent"


def test_required_policy_failures_and_bounded_retained_prefix() -> None:
    """Missing coverage/final failure/cancellation never exposes a checked artifact."""
    source = prepare_rdf(ontology())
    assert project(source, replace(policy(), checks=())).publication is None
    failed = project(
        source, replace(policy(), checks=requirements(CLASSES, languages=("de",)))
    )
    assert failed.publication is None and any(
        issue.kind == "finding" for issue in failed.report.issues
    )
    unknown_graph = ontology()
    unknown_graph.remove((E.leaf, RDF.type, None))
    unknown_graph.remove((E.leaf, RDFS.subClassOf, None))
    unknown = project(prepare_rdf(unknown_graph))
    assert unknown.publication is None
    assert "taxonomy.class_role_unverified" in {
        issue.code for issue in unknown.report.issues
    }
    with pytest.raises(InputError):
        replace(
            policy(), identity="separate-iri", source_class_predicate=str(E.sourceClass)
        )
    with pytest.raises(InputError):
        replace(policy(), source_class_predicate=str(OWL.sameAs))
    cancelled = threading.Event()
    cancelled.set()
    stopped = project_taxonomy(
        source, source, policy(), hierarchy_basis="asserted", cancel=cancelled
    )
    assert stopped.report.outcome == "exhausted" and stopped.publication is None
    assert project(source).report.verified
    graph = ontology()
    graph.add((E.root, RDFS.label, RdfLiteral("Other", lang="en")))
    duplicate = prepare_rdf(graph)
    retained = 0
    for cutoff in range(3000, 30000, 500):
        budget = Budget(Limits(max_work=cutoff))
        result = project_taxonomy(
            duplicate, duplicate, policy(), hierarchy_basis="asserted", budget=budget
        )
        paid = [
            issue
            for issue in budget._retained_issues.values()
            if issue.code == "taxonomy.label_ambiguous"
        ]
        if paid:
            assert paid[0] in result.report.issues
            retained += result.report.outcome == "exhausted"
        assert result.publication is None
    assert retained
