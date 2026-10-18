"""Real portable SKOS publications retain selected dependencies and checked scope."""

import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from rdflib import BNode, Dataset, Graph, Namespace
from rdflib import Literal as RdfLiteral
from rdflib.collection import Collection
from rdflib.namespace import RDF, SKOS, XSD

from bijux_ontology.documents.export import write_export
from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.migrations.candidates import MigrationCheck
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.reports import Issue, Report
from bijux_ontology.skos.checking import check_skos
from bijux_ontology.skos.collections import check_collection_sequence
from bijux_ontology.skos.publication.roots import check_top_concepts
from bijux_ontology.skos.publication.types import TopConceptPolicy
from bijux_ontology.skos.xl import XL
from bijux_ontology.vocabulary.inspection import inspect_vocabulary
from bijux_ontology.workflows.ontology.publication import (
    SkosPublication,
    compile_skos_publication,
)
from bijux_ontology.workflows.upgrades.plans.exporting import export_migration_plan

E = Namespace("urn:publication:")


def fixture() -> Graph:
    """Supply shared XL identities, equal forms and nested ordered dependencies."""
    graph = Graph()
    graph.add((E.scheme, RDF.type, SKOS.ConceptScheme))
    graph.add((E.scheme, SKOS.prefLabel, RdfLiteral("Scheme", lang="en")))
    graph.add((E.scheme, SKOS.hasTopConcept, E.a))
    label = BNode()
    graph.add((label, RDF.type, XL.Label))
    graph.add((label, XL.literalForm, RdfLiteral("Shared", lang="en")))
    for concept in (E.a, E.b):
        graph.add((concept, RDF.type, SKOS.Concept))
        graph.add((concept, SKOS.inScheme, E.scheme))
        graph.add((concept, SKOS.prefLabel, RdfLiteral("Français", lang="fr")))
        graph.add((concept, XL.prefLabel, label))
    for alternate in (E.first_label, E.other_label):
        graph.add((alternate, RDF.type, XL.Label))
        graph.add((alternate, XL.literalForm, RdfLiteral("Equal", lang="de")))
        graph.add((E.a, XL.altLabel, alternate))
    graph.add((E.b, SKOS.broader, E.a))
    graph.add((E.a, E.code, RdfLiteral("01", datatype=XSD.integer, normalize=False)))
    for collection in (E.ordered, E.nested, E.empty):
        graph.add((collection, RDF.type, SKOS.OrderedCollection))
        graph.add((collection, XL.prefLabel, E.collection_label))
    graph.add((E.collection_label, RDF.type, XL.Label))
    graph.add((E.collection_label, XL.literalForm, RdfLiteral("Collection", lang="en")))
    graph.add((E.ordered, SKOS.memberList, E.named_head))
    Collection(graph, E.named_head, [E.a, E.nested, E.b, E.a, E.empty])
    head = BNode()
    graph.add((E.nested, SKOS.memberList, head))
    Collection(graph, head, [E.b])
    graph.add((E.empty, SKOS.memberList, RDF.nil))
    return graph


def checks() -> tuple[MigrationCheck, ...]:
    """Exercise existing SKOS, language, top-concept and exact sequence contracts."""
    return (
        MigrationCheck(
            "multilingual XL",
            lambda source: check_skos(
                source.graph(),
                concepts=(str(E.a), str(E.b)),
                rules=(
                    "label-integrity",
                    "resource-integrity",
                    "xl",
                    "language-coverage",
                ),
                languages=("en", "fr"),
            ),
        ),
        MigrationCheck(
            "authored top concept",
            lambda source: (
                check_top_concepts(
                    inspect_vocabulary(source.graph()),
                    TopConceptPolicy(str(E.scheme), required=(str(E.a),)),
                ).report
            ),
        ),
        MigrationCheck(
            "original list positions",
            lambda source: check_collection_sequence(
                inspect_vocabulary(source.graph()),
                str(E.ordered),
                expected=tuple(map(str, (E.a, E.nested, E.b, E.a, E.empty))),
            ),
        ),
    )


def specification(**options: Any) -> SkosPublication:
    """Construct a selected publication with actual ordinary consumer contracts."""
    return replace(
        SkosPublication(
            str(E.scheme),
            "retain-reference",
            collections=(str(E.ordered),),
            annotation_predicates=(str(E.code),),
            checks=checks(),
        ),
        **options,
    )


def walk(report: Report) -> Iterator[Report]:
    """Visit ordinary required report children without changing native truth."""
    yield report
    for child in report.children:
        yield from walk(child)


def codes(report: Report) -> set[str]:
    """Collect complete report-tree findings for public behavior assertions."""
    return {issue.code for child in walk(report) for issue in child.issues}


def test_multilingual_shared_xl_and_ordered_roundtrip(tmp_path: Path) -> None:
    """Independent parsing retains shared labels, exact forms and every list node."""
    original = prepare_rdf(fixture())
    result = compile_skos_publication(original, specification())
    assert result.report.verified, result.report.to_dict()
    assert result.source is not None
    exported = export_migration_plan(result, accept=True, format="turtle")
    assert exported.report.verified, exported.report.to_dict()
    assert exported.content is not None
    parsed = prepare_rdf(exported.content, format="turtle")
    for check in checks():
        assert check.check(parsed).verified
    graph = parsed.graph()
    assert graph.value(E.a, XL.prefLabel) == graph.value(E.b, XL.prefLabel)
    assert len(set(graph.objects(E.a, XL.altLabel))) == 2
    assert (E.empty, SKOS.memberList, RDF.nil) in graph
    for collection in (E.ordered, E.nested, E.empty):
        assert (collection, XL.prefLabel, E.collection_label) in graph
    assert (
        E.collection_label,
        XL.literalForm,
        RdfLiteral("Collection", lang="en"),
    ) in graph
    assert str(graph.value(E.a, E.code)) == "01"
    assert compare_rdf(original, prepare_rdf(fixture())).verified
    destination = tmp_path / "publication.ttl"
    write_export(exported, destination)
    assert destination.read_bytes() == exported.content


def test_excluded_collection_member_and_missing_tail_never_complete() -> None:
    """A selected collection cannot silently lose a Concept or a malformed tail."""
    source = prepare_rdf(fixture())
    result = compile_skos_publication(source, specification(concepts=(str(E.a),)))
    assert result.source is None
    assert "skos.publication.collection_member_excluded" in codes(result.report)
    assert source.graph().value(E.b, SKOS.inScheme) == E.scheme
    graph = fixture()
    graph.remove((E.named_head, RDF.rest, None))
    malformed = compile_skos_publication(prepare_rdf(graph), specification())
    assert malformed.source is None
    assert "terms.collection_list_malformed" in codes(malformed.report)
    graph = fixture()
    label = graph.value(E.a, XL.prefLabel)
    graph.remove((label, XL.literalForm, None))
    missing_form = compile_skos_publication(
        prepare_rdf(graph),
        specification(
            checks=(
                MigrationCheck(
                    "actual selected assertions",
                    lambda source: Report(
                        "actual assertions",
                        len(source.graph()),
                    ),
                ),
            )
        ),
    )
    assert missing_form.source is None
    assert "skos.xl_form_unresolved" in codes(missing_form.report)


def test_mapping_boundaries_preserve_or_omit_exact_authored_assertions() -> None:
    """Reference, inclusion and omission publish different explicit finite scopes."""
    graph = fixture()
    graph.add((E.external, RDF.type, SKOS.Concept))
    graph.add((E.external, SKOS.inScheme, E.external_scheme))
    graph.add((E.external_scheme, RDF.type, SKOS.ConceptScheme))
    graph.add((E.external, SKOS.prefLabel, RdfLiteral("Outside", lang="en")))
    graph.add((E.external, SKOS.closeMatch, E.a))
    attachment = BNode()
    graph.add((E.a, E.metadata, attachment))
    graph.add((attachment, E.note, RdfLiteral("Original annotation")))
    graph.add((attachment, SKOS.closeMatch, E.metadata_target))
    graph.add((E.metadata_target, RDF.type, SKOS.Concept))
    graph.add((E.target_scheme, RDF.type, SKOS.ConceptScheme))
    graph.add((E.target_scheme, SKOS.hasTopConcept, E.metadata_target))
    graph.add((E.scheme, SKOS.hasTopConcept, E.metadata_target))
    graph.add((E.target_scheme, SKOS.prefLabel, RdfLiteral("Target scheme", lang="en")))
    graph.add(
        (E.metadata_target, SKOS.prefLabel, RdfLiteral("Nested target", lang="en"))
    )
    source = prepare_rdf(graph)
    for policy in ("retain-reference", "include-target", "omit"):
        result = compile_skos_publication(
            source,
            specification(
                mapping_boundary=policy,
                concepts=(str(E.a), str(E.b)),
                annotation_predicates=(str(E.code), str(E.metadata)),
            ),
        )
        assert result.report.verified, result.report.to_dict()
        assert result.source is not None
        published = result.source.graph()
        assert ((E.external, SKOS.closeMatch, E.a) in published) == (policy != "omit")
        assert ((E.external, RDF.type, SKOS.Concept) in published) == (
            policy == "include-target"
        )
        assert bool(
            tuple(published.triples((None, SKOS.closeMatch, E.metadata_target)))
        ) == (policy != "omit")
        assert ((E.metadata_target, RDF.type, SKOS.Concept) in published) == (
            policy == "include-target"
        )
        if policy == "include-target":
            assert (E.external, SKOS.inScheme, E.external_scheme) in published
            assert (E.target_scheme, SKOS.hasTopConcept, E.metadata_target) in published
            assert (E.scheme, SKOS.hasTopConcept, E.metadata_target) in published
            assert (E.target_scheme, RDF.type, SKOS.ConceptScheme) in published
            assert not tuple(
                published.triples((E.metadata_target, SKOS.inScheme, None))
            )
        else:
            assert any(child.exclusions for child in walk(result.report))
    graph.remove((E.external, RDF.type, SKOS.Concept))
    missing = compile_skos_publication(
        prepare_rdf(graph), specification(mapping_boundary="include-target")
    )
    assert missing.source is None
    assert "skos.publication.dependencies_unverified" in codes(missing.report)


def test_pinned_scope_and_unsupported_dataset_union() -> None:
    """Hidden graph assertions cannot satisfy selected publication dependencies."""
    dataset = Dataset()
    for triple in fixture():
        dataset.graph(E.g).add(triple)
    dataset.graph(E.h).add((E.hidden, SKOS.inScheme, E.scheme))
    dataset.graph(E.h).add((E.hidden, RDF.type, SKOS.Concept))
    source = prepare_rdf(RdfSource(dataset, graph_name=str(E.g)))
    result = compile_skos_publication(source, specification())
    assert result.report.verified, result.report.to_dict()
    assert result.source is not None and result.source.graph_name == str(E.g)
    assert not tuple(result.source.graph().triples((E.hidden, None, None)))
    for unsupported in (
        prepare_rdf(dataset),
        prepare_rdf(RdfSource(dataset, union=True)),
    ):
        result = compile_skos_publication(unsupported, specification())
        assert not result.report.verified and result.source is None


def test_required_checks_cancellation_metadata_and_retained_failure() -> None:
    """Unexercised requirements and exhausted work never authorize publication."""
    source = prepare_rdf(fixture())
    no_checks = compile_skos_publication(source, specification(checks=()))
    assert no_checks.source is None
    assert "skos.publication.requirements_unverified" in codes(no_checks.report)
    failed = compile_skos_publication(
        source,
        specification(
            checks=(
                MigrationCheck(
                    "actual rejected candidate",
                    lambda candidate: Report(
                        "consumer",
                        1,
                        (
                            Issue(
                                "consumer.rejected",
                                "Actual candidate does not meet policy",
                            ),
                        ),
                    ),
                ),
            )
        ),
    )
    assert failed.source is None and "consumer.rejected" in codes(failed.report)
    assert export_migration_plan(failed, accept=True, format="turtle").content is None
    cancel = threading.Event()
    cancel.set()
    stopped = compile_skos_publication(source, specification(), cancel=cancel)
    assert stopped.report.outcome == "exhausted" and stopped.source is None
    huge = specification(annotation_predicates=("urn:" + "x" * 1000,))
    bounded = compile_skos_publication(
        source, huge, budget=Budget(Limits(max_bytes=100))
    )
    assert bounded.report.outcome == "exhausted" and bounded.source is None
    spent = Budget(Limits(max_issues=1))
    rejected = compile_skos_publication(
        source, specification(concepts=(str(E.a),)), budget=spent
    )
    assert rejected.source is None
    assert "skos.publication.collection_member_excluded" in codes(rejected.report)
    with pytest.raises(InputError):
        SkosPublication(str(E.scheme), cast(Any, "guess"))
    with pytest.raises(InputError):
        specification(
            mapping_boundary="omit", annotation_predicates=(str(SKOS.closeMatch),)
        )
