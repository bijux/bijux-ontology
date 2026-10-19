"""Controlled membership binds existing actual-output contracts conservatively."""

from dataclasses import replace

import pytest
from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.collection import Collection
from rdflib.namespace import OWL, RDF, SKOS, XSD

from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.declarations.value_constraints import ValueConstraints
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.models.checking import check_models
from bijux_ontology.models.emission.application import application_matches
from bijux_ontology.models.emission.checking import check_emission
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import ModelUsage, RdfModelUsage
from bijux_ontology.reports import AcceptancePolicy, IssueSelection, Report
from bijux_ontology.skos.publication.types import ConceptReachability
from bijux_ontology.skos.subsets.selection import select_concepts
from bijux_ontology.skos.subsets.types import ConceptSubset
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.collections.types import CollectionOccurrence

EX = Namespace("https://example.org/")


def _vocabulary() -> Graph:
    """Supply concept membership and lifecycle independently of emitted records."""
    graph = Graph()
    graph.add((EX.scheme, RDF.type, SKOS.ConceptScheme))
    for concept in (EX.A, EX.B, EX.C):
        graph.add((concept, RDF.type, SKOS.Concept))
        graph.add((concept, SKOS.inScheme, EX.scheme))
    graph.add((EX.A, OWL.deprecated, Literal(False)))
    graph.add((EX.B, OWL.deprecated, Literal(True)))
    graph.add((EX.Record, RDF.type, OWL.Class))
    graph.add((EX.choice, RDF.type, OWL.ObjectProperty))
    return graph


def _field() -> FieldDeclaration:
    """Declare a normal application predicate; object IRIs are data by default."""
    return FieldDeclaration("choice", str(EX.choice), (ValueDeclaration("iri"),))


def _actual(subset: ConceptSubset, identifier: str) -> tuple[Report, Report]:
    """Run the same binding through real JSON-LD and native RDF observations."""
    model = ModelDeclaration("record", (str(EX.Record),), (subset.bind(_field()),))
    document = {
        "@context": {"@vocab": str(EX), "choice": {"@type": "@id"}},
        "@type": "Record",
        "choice": identifier,
    }
    graph = Graph()
    graph.add((EX.record, RDF.type, EX.Record))
    graph.add((EX.record, EX.choice, URIRef(identifier)))
    return (
        check_emission(document, (model,), (ModelUsage("record"),)),
        check_rdf_emission(
            prepare_source(graph), (model,), (RdfModelUsage("record", EX.record),)
        ),
    )


def test_closed_named_scope_binds_actual_iris_and_retains_required_report_once() -> (
    None
):
    vocabulary = _vocabulary()
    view = inspect_vocabulary(vocabulary)
    subset = select_concepts(view, scheme=str(EX.scheme), closed=True)
    assert subset.report.verified and set(subset.values) == {EX.A, EX.C}
    assert subset.excluded == (EX.B,)
    for identifier, valid in ((EX.A, True), (EX.B, False), (EX.outside, False)):
        reports = _actual(subset, str(identifier))
        assert all(report.verified is valid for report in reports)
        assert all(
            report.outcome == ("agreement" if valid else "disagreement")
            for report in reports
        )
        assert all(
            sum(child is subset.report for child in report.children) == 1
            for report in reports
        )
    # Caller mutation cannot change the already owned selection.
    vocabulary.add((EX.outside, RDF.type, SKOS.Concept))
    vocabulary.add((EX.outside, SKOS.inScheme, EX.scheme))
    assert all(
        report.outcome == "disagreement" for report in _actual(subset, str(EX.outside))
    )
    first = subset.bind(_field())
    second = subset.bind(replace(_field(), name="other", predicate=str(EX.other)))
    model = ModelDeclaration("record", (str(EX.Record),), (first, second))
    vocabulary.add((EX.other, RDF.type, OWL.ObjectProperty))
    static = check_models((model,), vocabulary)
    assert static.verified and static.children == (subset.report,)


def test_open_membership_retains_known_positive_and_negative_evidence() -> None:
    subset = select_concepts(inspect_vocabulary(_vocabulary()), scheme=str(EX.scheme))
    assert not subset.report.complete and subset.values
    field = subset.bind(_field())
    constraints = field.values[0].constraints
    assert constraints is not None
    for identifier, matched in ((EX.A, True), (EX.B, False), (EX.outside, None)):
        assert application_matches(constraints, identifier, budget=Budget()) is matched
    for identifier in (EX.A, EX.outside):
        reports = _actual(subset, str(identifier))
        assert all(report.outcome == "unverified" for report in reports)
        assert all(
            not any(issue.code == "emission.value_mismatch" for issue in report.issues)
            for report in reports
        )
    assert all(
        report.outcome == "disagreement" for report in _actual(subset, str(EX.B))
    )
    report = _actual(subset, str(EX.A))[0]
    policy = AcceptancePolicy(
        strict=True,
        exceptions=tuple(IssueSelection.for_issue(issue) for issue in report.issues),
    )
    assert report.exit_code(policy=policy) == 0 and not report.verified


def test_lifecycle_policies_keep_absence_and_conflict_explicit() -> None:
    graph = _vocabulary()
    view = inspect_vocabulary(graph)
    allowed = select_concepts(
        view, scheme=str(EX.scheme), closed=True, deprecation="allow"
    )
    assert allowed.report.verified and set(allowed.values) == {EX.A, EX.B, EX.C}
    assert all(report.verified for report in _actual(allowed, str(EX.B)))
    active = select_concepts(
        view, scheme=str(EX.scheme), closed=True, deprecation="require-active"
    )
    assert active.values == (EX.A,) and active.excluded == (EX.B,)
    assert not active.report.complete
    assert all(report.outcome == "unverified" for report in _actual(active, str(EX.C)))
    graph.add((EX.C, OWL.deprecated, Literal("maybe", datatype=XSD.string)))
    graph.add((EX.B, OWL.deprecated, Literal(False)))
    conflicted = select_concepts(
        inspect_vocabulary(graph), scheme=str(EX.scheme), closed=True
    )
    assert conflicted.values == (EX.A,) and not conflicted.excluded
    assert not conflicted.report.complete
    assert all(
        report.outcome == "unverified" for report in _actual(conflicted, str(EX.B))
    )


def test_interior_roots_and_cross_scheme_bridges_reuse_authored_paths() -> None:
    graph = _vocabulary()
    graph.add((EX.C, SKOS.broader, EX.bridge))
    graph.add((EX.bridge, SKOS.broader, EX.A))
    graph.add((EX.bridge, SKOS.inScheme, EX.otherScheme))
    view = inspect_vocabulary(graph)
    excluded = select_concepts(
        view,
        scheme=str(EX.scheme),
        roots=(str(EX.A),),
        closed=True,
        deprecation="allow",
    )
    assert excluded.report.verified and excluded.values == (EX.A,)
    included = select_concepts(
        view,
        scheme=str(EX.scheme),
        roots=(str(EX.A),),
        cross_scheme="include",
        closed=True,
        deprecation="allow",
    )
    assert included.report.verified and set(included.values) == {EX.A, EX.C}
    paths = [
        item
        for item in included.witnesses
        if isinstance(item, ConceptReachability) and item.concept == EX.C
    ]
    assert len(paths) == 1 and len(paths[0].steps) == 2
    assert all(
        report.outcome == "disagreement" for report in _actual(included, str(EX.B))
    )
    graph.add((EX.A, SKOS.broader, EX.C))
    cyclic = select_concepts(
        inspect_vocabulary(graph),
        scheme=str(EX.scheme),
        roots=(str(EX.A),),
        cross_scheme="include",
        closed=True,
        deprecation="allow",
    )
    assert cyclic.report.verified and set(cyclic.values) == {EX.A, EX.C}
    # Scheme membership or labels alone never invent a Concept role.
    graph.add((EX.label, SKOS.inScheme, EX.scheme))
    graph.add((EX.label, SKOS.prefLabel, Literal("label")))
    invalid_root = select_concepts(
        inspect_vocabulary(graph),
        scheme=str(EX.scheme),
        roots=(str(EX.label),),
        closed=True,
    )
    assert invalid_root.report.outcome == "unverified"


def test_nested_collection_paths_and_cycle_uncertainty_remain_visible() -> None:
    graph = _vocabulary()
    graph.add((EX.collection, RDF.type, SKOS.Collection))
    graph.add((EX.collection, SKOS.member, EX.ordered))
    graph.add((EX.collection, SKOS.member, EX.C))
    graph.add((EX.ordered, RDF.type, SKOS.OrderedCollection))
    head = BNode()
    graph.add((EX.ordered, SKOS.memberList, head))
    Collection(graph, head, [EX.A, EX.B, EX.A])
    subset = select_concepts(
        inspect_vocabulary(graph), collection=str(EX.collection), closed=True
    )
    assert subset.report.verified and set(subset.values) == {EX.A, EX.C}
    paths = [
        item
        for item in subset.witnesses
        if isinstance(item, CollectionOccurrence) and item.member == EX.A
    ]
    assert {item.path[-1].position for item in paths} == {0, 2}
    assert all(report.verified for report in _actual(subset, str(EX.A)))
    graph.add((EX.ordered, SKOS.member, EX.collection))
    cyclic = select_concepts(
        inspect_vocabulary(graph), collection=str(EX.collection), closed=True
    )
    assert cyclic.values and not cyclic.report.complete and cyclic.witnesses
    reports = _actual(cyclic, str(EX.outside))
    assert all(not report.verified for report in reports)
    assert all(
        not any(issue.code == "emission.value_mismatch" for issue in report.issues)
        for report in reports
    )


def test_incomplete_selection_report_is_a_required_child_even_with_known_member() -> (
    None
):
    missing = Report("membership", 1, requested=2, complete=False)
    enumeration = ValueEnumeration((EX.A,), report=missing)
    field = replace(
        _field(),
        values=(
            ValueDeclaration(
                "iri", constraints=ValueConstraints(enumeration=enumeration)
            ),
        ),
    )
    model = ModelDeclaration("record", (str(EX.Record),), (field,))
    constraints = field.values[0].constraints
    assert constraints is not None
    assert application_matches(constraints, EX.A, budget=Budget()) is True
    document = {
        "@context": {"@vocab": str(EX), "choice": {"@type": "@id"}},
        "@type": "Record",
        "choice": str(EX.A),
    }
    report = check_emission(document, (model,), (ModelUsage("record"),))
    assert not report.verified and report.children == (missing,)
    assert report.children[0].requested == 2
    optional_empty = ValueEnumeration((EX.A,), report=Report("empty", allow_empty=True))
    assert (
        not optional_empty.qualified
        and not optional_empty.qualification_report().verified
    )


def test_known_empty_missing_scopes_binding_overlap_and_shared_limits() -> None:
    graph = _vocabulary()
    graph.add((EX.empty, RDF.type, SKOS.ConceptScheme))
    view = inspect_vocabulary(graph)
    empty = select_concepts(view, scheme=str(EX.empty), closed=True)
    assert empty.report.verified and not empty.values
    assert all(report.outcome == "disagreement" for report in _actual(empty, str(EX.A)))
    missing = select_concepts(view, scheme=str(EX.absent), closed=True)
    assert all(report.outcome == "unverified" for report in _actual(missing, str(EX.A)))
    constrained = replace(
        _field(),
        values=(
            ValueDeclaration(
                "iri", constraints=ValueConstraints(iri_namespaces=(str(EX),))
            ),
        ),
    )
    subset = select_concepts(view, scheme=str(EX.scheme), closed=True)
    retained = subset.bind(constrained).values[0].constraints
    assert retained is not None and retained.iri_namespaces == (str(EX),)
    overlap = replace(
        _field(),
        values=(
            ValueDeclaration("iri", constraints=ValueConstraints(enumeration=(EX.A,))),
        ),
    )
    with pytest.raises(InputError):
        subset.bind(overlap)
    limited = select_concepts(
        view, scheme=str(EX.scheme), closed=True, budget=Budget(Limits(max_work=10))
    )
    assert limited.report.outcome == "exhausted" and not limited.report.complete
    assert all(not report.verified for report in _actual(limited, str(EX.A)))
    for options in (
        {"scheme": str(EX.scheme), "collection": str(EX.collection)},
        {"scheme": str(EX.scheme), "roots": ()},
        {"collection": str(EX.collection), "roots": (str(EX.A),)},
    ):
        with pytest.raises(InputError):
            select_concepts(view, **options)


def test_partial_lifecycle_evidence_never_becomes_closed_absence() -> None:
    subset = select_concepts(
        inspect_vocabulary(_vocabulary()),
        scheme=str(EX.scheme),
        closed=True,
        budget=Budget(Limits(max_work=30)),
    )
    assert subset.report.outcome == "exhausted" and not subset.report.complete
    assert subset.values == (EX.A,) and subset.excluded == (EX.B,)
    assert len(subset.witnesses) == 3
    constraints = subset.bind(_field()).values[0].constraints
    assert constraints is not None
    for term, expected in ((EX.A, True), (EX.B, False), (EX.outside, None)):
        assert application_matches(constraints, term, budget=Budget()) is expected
        for report in _actual(subset, str(term)):
            assert report.outcome == "exhausted" and not report.verified
            assert report.children == (subset.report,)
            assert any(
                issue.code == "emission.value_mismatch" for issue in report.issues
            ) is (term == EX.B)


def test_direct_enumeration_validates_owned_terms_and_empty_contradiction() -> None:
    for invalid in (URIRef("relative"), URIRef("https://example.org/\ud800")):
        with pytest.raises(InputError):
            ValueEnumeration((invalid,))
    with pytest.raises(InputError):
        ValueEnumeration((Literal("\ud800"),))
    with pytest.raises(LimitExceeded):
        ValueEnumeration((Literal("", datatype=URIRef("urn:" + "x" * 5_000_000)),))
    exact = Literal("01", datatype=XSD.integer, normalize=False)
    enumeration = ValueEnumeration((exact,))
    assert str(enumeration.values[0]) == "01"
    assert (
        application_matches(
            ValueConstraints(enumeration=ValueEnumeration(())), None, budget=Budget()
        )
        is False
    )
