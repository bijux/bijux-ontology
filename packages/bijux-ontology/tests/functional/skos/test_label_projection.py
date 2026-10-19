"""Actual label conversions preserve identities and require checked consumers."""

import threading
from dataclasses import replace

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace
from rdflib.namespace import RDF, SKOS, XSD

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    OutputRequirement,
    ValueDeclaration,
)
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.documents.migrations import export_migration
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.rdf.migrations.candidates import MigrationCheck
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.sources.types import RdfSource
from bijux_ontology.skos.label_projection import (
    LabelIdentity,
    project_xl_labels,
    promote_labels,
)
from bijux_ontology.skos.xl import XL

EX = Namespace("https://example.org/label-projection/")


def consumer(source: PreparedSource) -> Report:
    """Run the ordinary native field checker on the actual candidate graph."""
    model = ModelDeclaration(
        "Concept",
        (),
        (
            FieldDeclaration(
                "label",
                str(SKOS.prefLabel),
                (ValueDeclaration("literal", language="en"),),
                emission="guaranteed",
                output=OutputRequirement(
                    minimum_values=1, allow_null=True, allow_empty=True
                ),
            ),
        ),
    )
    return check_rdf_emission(source, (model,), (RdfModelUsage("Concept", EX.C),))


CHECKS = (MigrationCheck("plain-consumer", consumer),)


def xl_graph() -> Graph:
    """Two named label resources may intentionally carry the same form."""
    return (
        Graph()
        .add((EX.C, RDF.type, SKOS.Concept))
        .add((EX.C, XL.prefLabel, EX.L1))
        .add((EX.C, XL.prefLabel, EX.L2))
        .add((EX.L1, XL.literalForm, Literal("same", lang="en")))
        .add((EX.L2, XL.literalForm, Literal("same", lang="en")))
        .add((EX.L1, XL.labelRelation, EX.L2))
        .add((EX.L1, EX.author, Literal("editor")))
    )


def test_plain_projection_native_consumer_and_explicit_metadata_loss() -> None:
    graph = xl_graph()
    blank = BNode("label")
    graph.add((EX.D, XL.altLabel, blank))
    graph.add((blank, XL.literalForm, Literal("other", datatype=XSD.string)))
    graph.add((blank, EX.related, EX.L1))
    source = prepare_rdf(graph)
    original = set(source.graph())
    graph.remove((None, None, None))
    result = project_xl_labels(source, after_checks=CHECKS)
    assert result.report.verified, result.report.to_dict()
    assert result.source is not None and result.patch is not None
    plain = result.source.graph()
    assert set(plain.objects(EX.C, SKOS.prefLabel)) == {Literal("same", lang="en")}
    assert (EX.D, SKOS.altLabel, Literal("other", datatype=XSD.string)) in plain
    assert len(result.correspondence) == 3
    assert len({item.resource for item in result.correspondence}) == 3
    assert any(quad.predicate == XL.labelRelation for quad in result.omitted)
    assert any(quad.predicate == EX.author for quad in result.omitted)
    assert all(
        not tuple(plain.triples((label, None, None))) for label in (EX.L1, EX.L2)
    )
    assert set(source.graph()) == original
    exported = export_migration(result, accept=True, format="turtle")
    assert exported.content is not None and exported.report.verified
    assert export_migration(result, format="turtle").content is None
    plain.remove((None, None, None))
    assert result.source.graph()


def test_explicit_promotion_fresh_distinct_resources_and_selection_guards() -> None:
    source = prepare_rdf(
        Graph()
        .add((EX.C, SKOS.prefLabel, Literal("same", lang="en")))
        .add((EX.D, SKOS.prefLabel, Literal("same", lang="en")))
    )
    first = LabelIdentity(EX.C, SKOS.prefLabel, Literal("same", lang="en"), EX.L1)
    second = LabelIdentity(EX.D, SKOS.prefLabel, Literal("same", lang="en"), EX.L2)
    promoted = promote_labels(source, (first, second), after_checks=CHECKS)
    assert promoted.report.verified, promoted.report.to_dict()
    assert promoted.source is not None and len(promoted.correspondence) == 2
    graph = promoted.source.graph()
    assert (EX.C, XL.prefLabel, EX.L1) in graph and (EX.D, XL.prefLabel, EX.L2) in graph
    assert (EX.L1, XL.literalForm, Literal("same", lang="en")) in graph
    assert (EX.L2, XL.literalForm, Literal("same", lang="en")) in graph
    assert (EX.C, SKOS.prefLabel, Literal("same", lang="en")) in graph
    assert not tuple(source.graph().triples((None, XL.prefLabel, None)))
    for identities in (
        (),
        (first, first),
        (first, replace(second, resource=EX.L1)),
        (replace(first, resource=EX.C),),
        (replace(first, owner=EX.Missing),),
        (replace(first, value=Literal("different", lang="en")),),
    ):
        result = promote_labels(source, identities, after_checks=CHECKS)
        assert not result.report.verified and result.source is None
    for datatype in (EX.UsedDatatype, XSD.string):
        typed_source = prepare_rdf(
            source.graph().add(
                (EX.Metadata, EX.value, Literal("opaque", datatype=datatype))
            )
        )
        collision = promote_labels(
            typed_source, (replace(first, resource=datatype),), after_checks=CHECKS
        )
        assert not collision.report.verified and collision.source is None
        assert any(
            issue.code == "skos.labels_unverified" for issue in collision.report.issues
        )
    with pytest.raises(InputError):
        replace(first, resource=Literal(str(EX.L1)))  # type: ignore[arg-type]
    roundtrip = project_xl_labels(promoted.source, after_checks=CHECKS)
    assert roundtrip.report.verified and roundtrip.source is not None
    assert set(roundtrip.source.graph()) == set(source.graph())


def test_invalid_xl_and_consumer_obligations_cannot_be_shortcut() -> None:
    for graph in (
        Graph().add((EX.C, XL.prefLabel, EX.Missing)),
        xl_graph().add((EX.L1, XL.literalForm, Literal("other", lang="en"))),
        xl_graph().add((EX.L1, XL.literalForm, Literal(1))),
        xl_graph().add((EX.C, SKOS.prefLabel, Literal("conflict", lang="en"))),
    ):
        result = project_xl_labels(prepare_rdf(graph), after_checks=CHECKS)
        assert not result.report.verified and result.source is None
    source = prepare_rdf(xl_graph())
    missing = project_xl_labels(source)
    assert not missing.report.verified and missing.source is None
    calls = []

    def reject(actual: PreparedSource) -> Report:
        """The selected consumer sees actual plain output and can reject it."""
        calls.append(tuple(actual.graph().objects(EX.C, SKOS.prefLabel)))
        return Report(
            "consumer", 1, (Issue("consumer.label", "Consumer rejects label"),)
        )

    failed = project_xl_labels(
        source, after_checks=(MigrationCheck("required", reject),)
    )
    assert len(calls) == 1 and calls[0] == (Literal("same", lang="en"),)
    assert failed.source is not None and not failed.report.verified
    assert export_migration(failed, accept=True, format="turtle").content is None


def test_physical_partition_selection_and_owned_blank_identity() -> None:
    dataset = Dataset()
    for triple in xl_graph():
        dataset.graph(EX.Selected).add(triple)
    dataset.graph(EX.Hidden).add((EX.L1, XL.literalForm, Literal("hidden", lang="fr")))
    source = prepare_rdf(RdfSource(dataset, graph_name=str(EX.Selected)))
    result = project_xl_labels(source, after_checks=CHECKS)
    assert result.report.verified and result.source is not None
    assert result.source.graph_name == str(EX.Selected)
    assert EX.Hidden not in {
        graph.identifier for graph in result.source.dataset().graphs()
    }
    assert source.dataset().graph(EX.Hidden)
    for descriptor in (RdfSource(dataset), RdfSource(dataset, union=True)):
        rejected = project_xl_labels(prepare_rdf(descriptor), after_checks=CHECKS)
        assert not rejected.report.verified and rejected.source is None
    node = BNode("owner")
    blank_source = prepare_rdf(Graph().add((node, SKOS.altLabel, Literal("blank"))))
    owned = next(blank_source.graph().subjects())
    assert isinstance(owned, BNode)
    identity = LabelIdentity(owned, SKOS.altLabel, Literal("blank"), EX.BlankLabel)
    assert promote_labels(
        blank_source,
        (identity,),
        after_checks=(
            MigrationCheck(
                "actual", lambda actual: Report("native", len(actual.graph()))
            ),
        ),
    ).report.verified
    foreign = replace(identity, owner=BNode(str(owned)))
    assert not promote_labels(
        blank_source, (foreign,), after_checks=CHECKS
    ).report.verified


def test_bounds_cancellation_and_paid_consumer_prefix() -> None:
    source = prepare_rdf(xl_graph())
    event = threading.Event()
    event.set()
    assert (
        project_xl_labels(source, after_checks=CHECKS, cancel=event).report.outcome
        == "exhausted"
    )
    assert (
        project_xl_labels(
            source, after_checks=CHECKS, limits=Limits(max_work=1)
        ).report.outcome
        == "exhausted"
    )
    operation = Budget()
    paid = Issue("consumer.actual", "Actual selected consumer finding")

    def first(actual: PreparedSource) -> Report:
        """Pay an actual finding before the later callback exhausts its operation."""
        assert actual.graph()
        return Report("first", 1, (paid,))

    def later(actual: PreparedSource) -> Report:
        """Use the shared operation's remaining work after the first result."""
        assert actual.graph()
        operation.consume("work", operation.remaining("work") + 1)
        return Report("later", 1)

    result = project_xl_labels(
        source,
        budget=operation,
        after_checks=(
            MigrationCheck("first", first),
            MigrationCheck("later", later),
        ),
    )
    assert result.report.outcome == "exhausted"
    assert any(issue.code == paid.code for issue in result.report.issues)
