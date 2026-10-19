"""Declared entity categories and scoped coverage retain RDF/OWL distinctions."""

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

from bijux_ontology.declarations import TermDeclaration
from bijux_ontology.vocabulary.checking import check_vocabulary, declared_kinds
from bijux_ontology.vocabulary.sources.view import Vocabulary

EX = Namespace("https://example.org/")


def test_individual_custom_and_skos_declarations_are_not_observed_only_terms() -> None:
    graph = Graph().add((EX.concept, RDF.type, SKOS.Concept))
    graph.add((EX.person, RDF.type, OWL.NamedIndividual))
    graph.add((EX.organism, RDF.type, EX.CustomCategory))
    graph.add(
        (EX.observed, RDFS.label, Literal("Observation alone is not a declaration"))
    )
    terms = [
        TermDeclaration(str(EX.concept), (str(SKOS.Concept),)),
        TermDeclaration(str(EX.person), (str(OWL.NamedIndividual),)),
        TermDeclaration(str(EX.organism), (str(EX.CustomCategory),)),
    ]
    assert check_vocabulary(graph, terms).verified
    missing = check_vocabulary(graph, [TermDeclaration(str(EX.observed))])
    assert missing.issues[0].code == "vocabulary.term_missing"
    assert declared_kinds(graph, str(EX.concept)) == frozenset((str(SKOS.Concept),))


def test_builtin_roles_and_datatype_class_relationship_have_explicit_regimes() -> None:
    graph = Graph().add((EX.Datatype, RDF.type, RDFS.Datatype))
    terms = [
        TermDeclaration(str(RDF.type), (str(RDF.Property),)),
        TermDeclaration(str(RDF.first), (str(RDF.Property),)),
        TermDeclaration(str(RDF.List), (str(RDFS.Class),)),
        TermDeclaration(str(RDFS.label), (str(OWL.AnnotationProperty),)),
        TermDeclaration(str(XSD.integer), (str(RDFS.Datatype),)),
    ]
    assert check_vocabulary(graph, terms).verified
    datatype_class = [TermDeclaration(str(EX.Datatype), (str(RDFS.Class),))]
    assert check_vocabulary(graph, datatype_class, regime="rdf").verified
    dl = check_vocabulary(graph, datatype_class, regime="owl-dl")
    assert (
        dl.regime == "owl-dl" and dl.issues[0].code == "vocabulary.entity_kind_mismatch"
    )
    wrong_role = check_vocabulary(
        graph, [TermDeclaration(str(RDF.List), (str(RDF.Property),))]
    )
    assert wrong_role.exit_code() == 1


def test_coverage_selects_local_or_import_declarations_without_lookup_ownership() -> (
    None
):
    local = Graph().add((EX.Class, RDF.type, OWL.Class))
    local.add((EX.concept, RDF.type, SKOS.Concept))
    imported = Graph().add((EX.Imported, RDF.type, OWL.Class))
    lookup = Graph().add((EX.Lookup, RDF.type, OWL.Class))
    vocabulary = Vocabulary(local, imports=[imported], lookup=[lookup])
    inventory = [TermDeclaration(str(EX.Class))]
    assert check_vocabulary(
        vocabulary, inventory, require_coverage=True, coverage_kinds=[str(OWL.Class)]
    ).verified
    closure = check_vocabulary(
        vocabulary,
        inventory,
        require_coverage=True,
        coverage_scope="closure",
        coverage_kinds=[str(OWL.Class)],
    )
    assert {issue.actual for issue in closure.issues} == {str(EX.Imported)}
    local_missing = check_vocabulary(
        vocabulary, [TermDeclaration(str(EX.Lookup))], local_namespaces=[str(EX)]
    )
    assert local_missing.issues[0].code == "vocabulary.term_missing"


def test_reserved_names_include_xsd_and_canonical_container_members() -> None:
    graph = Graph().add((EX.subject, URIRef(str(RDF) + "_1"), EX.object))
    graph.add((EX.subject, RDFS.range, URIRef(str(XSD) + "integerTypo")))
    report = check_vocabulary(graph, [], lint=True)
    assert {issue.actual for issue in report.issues} == {str(XSD) + "integerTypo"}
    assert check_vocabulary(
        graph, [TermDeclaration(str(RDF) + "_1", (str(RDF.Property),))]
    ).verified


def test_vocabulary_limits_retain_findings_and_stop_without_verifying() -> None:
    from bijux_ontology.limits import Limits

    graph = Graph().add((EX.A, RDF.type, OWL.Class))
    terms = [TermDeclaration(str(EX.Missing)), TermDeclaration(str(EX.Other))]
    report = check_vocabulary(graph, terms, limits=Limits(max_issues=1))
    assert report.outcome == "exhausted" and not report.complete
    assert report.issues[0].code == "vocabulary.term_missing"
    assert report.issues[-1].kind == "exhausted"
    view = Vocabulary(graph, limits=Limits(max_work=1))
    report = check_vocabulary(
        view, [TermDeclaration(str(EX.A)), TermDeclaration(str(EX.A))]
    )
    assert report.checked == 1 and report.outcome == "exhausted"
    excluded = check_vocabulary(
        view, terms, exclude=(str(EX.Missing), str(EX.Other)), allow_empty=True
    )
    assert excluded.checked == 0 and excluded.exclusions == tuple(
        sorted(term.iri for term in terms)
    )
    assert not excluded.verified
