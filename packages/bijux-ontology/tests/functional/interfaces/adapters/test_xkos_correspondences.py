"""Real grouped XKOS round trips and explicit finite record conversion policies."""

from rdflib import RDF, SKOS, BNode, Dataset, Graph, Literal, URIRef

from bijux_ontology.interfaces.adapters.xkos.correspondences import (
    AssociationRequirement,
    ClassificationRelease,
    Correspondence,
    RecordConversion,
    assess_assignments,
    read_correspondence,
    write_correspondence,
)
from bijux_ontology.interfaces.adapters.xkos.correspondences.types import XKOS as X
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.migrations.values.types import ValueMap
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView

T = URIRef("urn:correspondence:table")
OLD = URIRef("urn:classification:old")
NEW = URIRef("urn:classification:new")
A, B, C, D = (URIRef("urn:category:" + name) for name in ("A", "B", "C", "D"))
GROUP = URIRef("urn:association:group")
SIMPLE = URIRef("urn:association:simple")
E, F = URIRef("urn:category:E"), URIRef("urn:category:F")
RECORD = URIRef("urn:record:enterprise")


def release(
    scheme: URIRef, concepts: tuple[URIRef, ...], *, complete: bool = True
) -> ClassificationRelease:
    """Build a supplied finite classification with explicit version membership."""
    graph = Graph()
    graph.add((scheme, RDF.type, SKOS.ConceptScheme))
    for concept in concepts:
        graph.add((concept, RDF.type, SKOS.Concept))
        graph.add((concept, SKOS.inScheme, scheme))
        graph.add((concept, SKOS.notation, Literal("same code")))
    return ClassificationRelease(scheme, VocabularyView(graph), complete)


def graph() -> Graph:
    """Retain one many-to-many group and one independent one-to-one control."""
    result = Graph()
    result.add((T, RDF.type, X.Correspondence))
    result.add((T, X.compares, OLD))
    result.add((T, X.compares, NEW))
    for identifier, sources, targets in ((GROUP, (A, B), (C, D)), (SIMPLE, (E,), (F,))):
        result.add((T, X.madeOf, identifier))
        result.add((identifier, RDF.type, X.ConceptAssociation))
        for category in sources:
            result.add((identifier, X.sourceConcept, category))
        for category in targets:
            result.add((identifier, X.targetConcept, category))
    result.add(
        (
            GROUP,
            SKOS.note,
            Literal("Caller-owned allocation policy is separate", lang="en"),
        )
    )
    return result


def table(
    source: PreparedSource | Graph | None = None, *, budget: Budget | None = None
) -> Correspondence:
    """Use exact supplied source and target release views, never notation matching."""
    return read_correspondence(
        graph() if source is None else source,
        T,
        source_release=release(OLD, (A, B, E)),
        target_release=release(NEW, (C, D, F)),
        requirements=(AssociationRequirement(GROUP, (A, B), (C, D)),),
        budget=budget,
    )


def test_intact_group_and_one_to_one_round_trip() -> None:
    """Actual RDF model serialization preserves groups and optional annotations."""
    original = prepare_rdf(graph())
    read = table(original)
    assert read.report.verified, read.report.to_dict()
    groups = {group.identifier: group for group in read.associations}
    assert (
        groups[GROUP].kind == "many-to-many"
        and set(groups[GROUP].sources) == {A, B}
        and set(groups[GROUP].targets) == {C, D}
    )
    assert groups[SIMPLE].kind == "one-to-one"
    written = write_correspondence(read)
    assert written.report.verified and written.source is not None
    assert compare_rdf(original, written.source).verified
    output = written.source.graph()
    assert len(tuple(output.objects(T, X.madeOf))) == 2
    assert not tuple(output.triples((None, SKOS.exactMatch, None)))
    read_again = table(written.source)
    assert read_again.report.verified and len(read_again.associations) == 2
    output.remove((GROUP, X.targetConcept, C))
    assert C in tuple(written.source.graph().objects(GROUP, X.targetConcept))


def test_missing_endpoint_wrong_release_and_incomplete_required_side() -> None:
    """Reject equal notations and reused category names in the wrong release."""
    missing = read_correspondence(
        graph(),
        T,
        source_release=release(OLD, (A, E)),
        target_release=release(NEW, (C, D, F)),
    )
    assert not missing.report.verified and any(
        issue.code == "xkos.endpoint_missing" for issue in missing.report.issues
    )
    wrong = read_correspondence(
        graph(),
        T,
        source_release=release(URIRef("urn:classification:other-version"), (A, B, E)),
        target_release=release(NEW, (C, D, F)),
    )
    assert not wrong.report.verified and any(
        issue.code == "xkos.release_mismatch" for issue in wrong.report.issues
    )
    incomplete = graph()
    incomplete.remove((GROUP, X.sourceConcept, B))
    checked = table(incomplete)
    assert not checked.report.verified and any(
        issue.code == "xkos.side_inventory" for issue in checked.report.issues
    )
    empty = graph()
    empty.remove((GROUP, X.targetConcept, None))
    checked = read_correspondence(
        empty,
        T,
        source_release=release(OLD, (A, B, E)),
        target_release=release(NEW, (C, D, F)),
    )
    assert not checked.report.verified and any(
        issue.code == "xkos.side_missing" for issue in checked.report.issues
    )
    assert not write_correspondence(checked).report.verified
    # A zero-sided deletion is valid XKOS, separately from a required target policy.
    allowed = read_correspondence(
        empty,
        T,
        source_release=release(OLD, (A, B, E)),
        target_release=release(NEW, (C, D, F)),
        require_targets=False,
    )
    assert allowed.report.verified and any(
        group.kind == "deletion" for group in allowed.associations
    )
    no_categories = graph()
    no_categories.remove((None, X.sourceConcept, None))
    no_categories.remove((None, X.targetConcept, None))
    meaningless = read_correspondence(
        no_categories,
        T,
        source_release=release(OLD, (A, B, E)),
        target_release=release(NEW, (C, D, F)),
        require_sources=False,
        require_targets=False,
    )
    assert not meaningless.report.verified


def test_explicit_record_choice_rechecks_group_and_destination() -> None:
    """An actual record is not split or assigned an inferred target without policy."""
    records = Graph()
    records.add((RECORD, X.classifiedUnder, A))
    records.add((RECORD, URIRef("urn:property:count"), Literal(17)))
    source = prepare_rdf(records)
    correspondence = table()
    unresolved = assess_assignments(correspondence, source, (RECORD,))
    assert not unresolved.report.verified and unresolved.resolutions[0].target is None
    choice = RecordConversion(RECORD, GROUP, ValueMap(((A, C),)))
    resolved = assess_assignments(
        correspondence, source, (RECORD,), conversions=(choice,)
    )
    assert resolved.report.verified and resolved.resolutions[0].target == C
    assert tuple(source.graph().objects(RECORD, X.classifiedUnder)) == (A,)
    assert tuple(source.graph().objects(RECORD, URIRef("urn:property:count"))) == (
        Literal(17),
    )
    invalid = assess_assignments(
        correspondence,
        source,
        (RECORD,),
        conversions=(RecordConversion(RECORD, GROUP, ValueMap(((A, F),))),),
    )
    assert not invalid.report.verified and invalid.resolutions[0].target is None
    removed_destination = read_correspondence(
        graph(),
        T,
        source_release=release(OLD, (A, B, E)),
        target_release=release(NEW, (D, F)),
    )
    assert not assess_assignments(
        removed_destination, source, (RECORD,), conversions=(choice,)
    ).report.verified
    # A supplied table entry for a different source category is not exercised.
    unused = assess_assignments(
        correspondence,
        source,
        (RECORD,),
        conversions=(RecordConversion(RECORD, GROUP, ValueMap(((B, D),))),),
    )
    assert not unused.report.verified and any(
        issue.code == "xkos.conversion_unexercised" for issue in unused.report.issues
    )


def test_blank_associations_backing_graphs_and_physical_pin() -> None:
    """Fresh global blank mapping retains exact graph authority and all source facts."""
    data = Dataset()
    name = BNode("physical")
    association = BNode("association")
    selected = data.graph(name)
    for triple in graph():
        subject, predicate, value = triple
        selected.add(
            (
                association if subject == GROUP else subject,
                predicate,
                association if value == GROUP else value,
            )
        )
    outside = data.graph(URIRef("urn:graph:outside"))
    outside.add(
        (
            association,
            URIRef("urn:property:outside"),
            Literal(
                "01",
                datatype=URIRef("http://www.w3.org/2001/XMLSchema#integer"),
                normalize=False,
            ),
        )
    )
    data.graph(URIRef("urn:graph:empty"))
    source = prepare_rdf(data)
    owned_name = next(
        value for value in source._graph_names() if isinstance(value, BNode)
    )
    source.graph_name = owned_name
    owned_group = next(source._read().objects(T, X.madeOf))
    if not isinstance(owned_group, BNode):
        owned_group = next(
            value
            for value in source._read().objects(T, X.madeOf)
            if isinstance(value, BNode)
        )
    checked = read_correspondence(
        source,
        T,
        source_release=release(OLD, (A, B, E)),
        target_release=release(NEW, (C, D, F)),
    )
    assert checked.report.verified
    exported = write_correspondence(checked)
    assert exported.report.verified and exported.source is not None
    assert (
        isinstance(exported.source.graph_name, BNode)
        and exported.source.graph_name is not owned_name
    )
    assert any(
        value is exported.source.graph_name for value in exported.source._graph_names()
    )
    assert exported.source.graph_name != "@default"
    assert len(tuple(exported.source._owned_contexts(None))) == len(
        tuple(source._owned_contexts(None))
    )
    fresh_group = next(
        value
        for value in exported.source._read().objects(T, X.madeOf)
        if isinstance(value, BNode)
    )
    backing = next(
        value
        for value in exported.source._owned_contexts(None)
        if value.identifier == URIRef("urn:graph:outside")
    )
    assert fresh_group in tuple(backing.subjects(URIRef("urn:property:outside"), None))
    assert compare_rdf(
        prepare_rdf(source.dataset()), prepare_rdf(exported.source.dataset())
    ).verified
    foreign = BNode(str(owned_name))
    rejected = read_correspondence(
        source,
        T,
        source_release=release(OLD, (A, B, E)),
        target_release=release(NEW, (C, D, F)),
        graph_name=foreign,
    )
    assert not rejected.report.verified
    source.graph_name = BNode(str(owned_name))
    assert not write_correspondence(checked).report.verified


def test_raw_invalid_terms_and_cumulative_paid_prefix() -> None:
    """Malformed required groups are visible and paid findings survive later cuts."""
    malformed = graph()
    malformed.add((T, X.madeOf, Literal("not a resource")))
    checked = table(malformed)
    assert not checked.report.verified and Literal("not a resource") in checked.links
    assert not write_correspondence(checked).report.verified
    small = Budget(Limits(max_work=50))
    result = table(budget=small)
    assert result.report.outcome == "exhausted" and not result.report.verified
