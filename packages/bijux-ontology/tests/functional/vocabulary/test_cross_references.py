"""Native OBO references keep attachment, syntax and catalogue truth separate."""

from collections.abc import Sequence
from dataclasses import replace
from typing import Literal as Choice
from typing import TypedDict, Unpack

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.vocabulary.accessions.types import AccessionRule
from bijux_ontology.vocabulary.browsing.inspection import (
    VocabularyView,
    inspect_vocabulary,
)
from bijux_ontology.vocabulary.obo.cross_references import (
    CrossReferenceCheck,
    LocalCatalogue,
    check_cross_references,
)
from bijux_ontology.vocabulary.registry.types import (
    IdentifierRegistry,
    NamespaceContribution,
)
from bijux_ontology.vocabulary.sources.loading import load_vocabulary

E = Namespace("urn:owner:")
DB = Namespace("https://example.org/database/")
OIO = Namespace("http://www.geneontology.org/formats/oboInOwl#")
DEFINITION = URIRef("http://purl.obolibrary.org/obo/IAO_0000115")
REGISTRY = IdentifierRegistry(
    (NamespaceContribution(str(DB), "DB", "caller", ("Alias",)),)
)
RULES = (AccessionRule(str(DB), "[0-9]+"),)


def native() -> None:
    pytest.importorskip("re2")


def annotate(graph: Graph, predicate: URIRef, lexical: str, xref: str) -> BNode:
    """Supply the actual OBO-in-OWL assertion and independent axiom attachment."""
    node = BNode()
    target = Literal(lexical, lang="en")
    for triple in (
        (E.Term, predicate, target),
        (node, RDF.type, OWL.Axiom),
        (node, OWL.annotatedSource, E.Term),
        (node, OWL.annotatedProperty, predicate),
        (node, OWL.annotatedTarget, target),
        (node, OIO.hasDbXref, Literal(xref)),
    ):
        graph.add(triple)
    return node


def catalogue(*identifiers: str, closed: bool = True) -> LocalCatalogue:
    graph = Graph()
    for identifier in identifiers:
        graph.add((DB[identifier], RDF.type, OWL.Class))
    return LocalCatalogue(str(DB), inspect_vocabulary(graph), closed)


class _Arguments(TypedDict, total=False):
    catalogues: Sequence[LocalCatalogue]
    literal_mode: Choice["curie", "iri"]
    unknown_namespaces: Choice["unverified", "exclude"]
    require_resolution: bool
    limits: Limits
    budget: Budget


def check(view: VocabularyView, **kwargs: Unpack[_Arguments]) -> CrossReferenceCheck:
    return check_cross_references(view, [E.Term], REGISTRY, rules=RULES, **kwargs)


def test_term_definition_and_equal_synonym_text_keep_separate_native_attachment() -> (
    None
):
    native()
    graph = Graph().add((E.Term, OIO.hasDbXref, Literal("DB:1")))
    annotate(graph, DEFINITION, "definition", "DB:2")
    annotate(graph, OIO.hasExactSynonym, "same", "DB:3")
    annotate(graph, OIO.hasBroadSynonym, "same", "DB:4")
    term_qualifier = BNode()
    for triple in (
        (term_qualifier, RDF.type, OWL.Axiom),
        (term_qualifier, OWL.annotatedSource, E.Term),
        (term_qualifier, OWL.annotatedProperty, OIO.hasDbXref),
        (term_qualifier, OWL.annotatedTarget, Literal("DB:1")),
        (term_qualifier, RDFS.comment, Literal("curated reference")),
    ):
        graph.add(triple)
    before = set(graph)
    result = check(
        inspect_vocabulary(graph), catalogues=[catalogue("1", "2", "3", "4")]
    )
    assert result.report.verified and len(result.observations) == 4
    by_value = {str(o.reference.assertion.object): o for o in result.observations}
    assert by_value["DB:1"].reference.attachment is None
    qualification = by_value["DB:1"].reference.annotations
    assert qualification is not None and qualification.report.verified
    assert len(qualification.attachments) == 1
    assert qualification.attachments[0].qualifiers[0].value == Literal(
        "curated reference"
    )

    for value, role in (
        ("DB:2", "definition"),
        ("DB:3", "exact"),
        ("DB:4", "broad"),
    ):
        ref = by_value[value].reference
        assert ref.attachment is not None
        assert ref.record is not None and ref.record.role == role
        assert ref.attachment.status == "attached"
        assert by_value[value].resolution == "resolved"
    assert by_value["DB:3"].reference.record is not None
    assert by_value["DB:4"].reference.record is not None
    assert (
        by_value["DB:3"].reference.record.literal
        == by_value["DB:4"].reference.record.literal
    )
    assert set(graph) == before
    assert not any(p in (OWL.sameAs, OWL.equivalentClass) for _, p, _ in graph)


def test_closed_local_dangling_open_absence_and_unsupplied_external_are_distinct() -> (
    None
):
    native()
    view = inspect_vocabulary(Graph().add((E.Term, OIO.hasDbXref, Literal("DB:9"))))
    closed = check(view, catalogues=[catalogue("1")])
    assert closed.report.outcome == "disagreement"
    assert closed.observations[0].resolution == "dangling"
    opened = check(view, catalogues=[catalogue("1", closed=False)])
    assert not opened.report.verified and opened.observations[0].resolution == "unknown"
    external = check(view)
    assert (
        external.report.verified and external.observations[0].resolution == "external"
    )
    assert external.observations[0].accession is not None
    assert external.observations[0].accession.status == "valid"
    required = check(view, require_resolution=True)
    assert (
        not required.report.verified
        and required.observations[0].resolution == "external"
    )
    # A subject-based catalogue can establish an actual record without inventing
    # an RDF declaration; an object-only mention cannot satisfy that contract.
    subjects = inspect_vocabulary(Graph().add((DB["9"], RDFS.label, Literal("record"))))
    local = LocalCatalogue(str(DB), subjects, True, "subject")
    resolved = check(view, catalogues=[local])
    assert resolved.report.verified and resolved.observations[0].assertions
    object_only = inspect_vocabulary(Graph().add((E.other, E.ref, DB["9"])))
    assert (
        check(view, catalogues=[replace(local, view=object_only)])
        .observations[0]
        .resolution
        == "dangling"
    )


def test_malformed_values_and_ambiguous_attachment_never_resolve_arbitrarily() -> None:
    native()
    graph = Graph().add((E.Term, OIO.hasDbXref, Literal("DB:not-digits")))
    graph.add((E.Term, OIO.hasDbXref, Literal("DB:1", lang="en")))
    graph.add((E.Term, OIO.hasDbXref, Literal(1)))
    graph.add((E.Term, OIO.hasDbXref, BNode()))
    attachment = annotate(graph, DEFINITION, "definition", "DB:1")
    graph.add((attachment, OWL.annotatedTarget, Literal("other")))
    result = check(inspect_vocabulary(graph), catalogues=[catalogue("1")])
    assert not result.report.verified
    assert sum(o.resolution == "invalid" for o in result.observations) == 4
    attached = next(
        o for o in result.observations if o.reference.attachment is not None
    )
    assert attached.reference.attachment is not None
    assert attached.reference.attachment.status == "malformed"
    assert attached.reference.record is None and attached.accession is None
    assert attached.resolution == "unknown"


def test_aliases_explicit_iri_intent_and_namespace_exclusion_preserve_originals() -> (
    None
):
    native()
    graph = Graph().add((E.Term, OIO.hasDbXref, Literal("Alias:1")))
    graph.add((E.Term, OIO.hasDbXref, DB["1"]))
    result = check(inspect_vocabulary(graph), catalogues=[catalogue("1")])
    assert result.report.verified
    assert {o.iri for o in result.observations} == {DB["1"]}
    assert {o.reference.assertion.object for o in result.observations} == {
        Literal("Alias:1"),
        DB["1"],
    }
    iri = inspect_vocabulary(
        Graph().add((E.Term, OIO.hasDbXref, Literal(str(DB["1"]))))
    )
    assert check(iri, literal_mode="iri", catalogues=[catalogue("1")]).report.verified
    assert not check(iri).report.verified
    unknown = inspect_vocabulary(
        Graph().add((E.Term, OIO.hasDbXref, Literal("Unknown:1")))
    )
    assert not check(unknown).report.verified
    excluded = check(unknown, unknown_namespaces="exclude")
    assert excluded.observations[0].resolution == "excluded"
    assert excluded.accessions is not None
    assert excluded.accessions.report.exclusions


def test_native_source_scope_and_selected_owner_prevent_accidental_widening() -> None:
    native()
    local = Graph().add((E.Term, OIO.hasDbXref, Literal("DB:1")))
    other = Graph().add((E.Term, OIO.hasDbXref, Literal("DB:bad")))
    vocabulary = load_vocabulary(local, lookup=[other])
    selected = check(inspect_vocabulary(vocabulary, scopes=("local",)))
    assert selected.report.verified and len(selected.references) == 1
    widened = check(inspect_vocabulary(vocabulary, scopes=("local", "lookup")))
    assert widened.report.outcome == "disagreement" and len(widened.references) == 2
    graph = local.add((E.Other, OIO.hasDbXref, Literal("DB:bad")))
    assert check(inspect_vocabulary(graph)).report.verified
    dataset = Dataset()
    dataset.graph(E.selected).add((E.Term, OIO.hasDbXref, Literal("DB:1")))
    dataset.graph(E.hidden).add((E.Term, OIO.hasDbXref, Literal("DB:bad")))
    pinned = load_vocabulary(dataset, graph_name=str(E.selected))
    assert check(inspect_vocabulary(pinned)).report.verified


def test_shared_budget_exhaustion_keeps_raw_prefix_without_false_coverage() -> None:
    native()
    graph = Graph().add((E.Term, OIO.hasDbXref, Literal("DB:1")))
    view = inspect_vocabulary(graph)
    budget = Budget()
    assert check(view, budget=budget).report.verified
    assert budget.remaining("work") < budget.limits.max_work
    capped = check(view, limits=Limits(max_work=1))
    assert capped.report.outcome == "exhausted" and not capped.observations
    huge = inspect_vocabulary(
        Graph().add((E.Term, OIO.hasDbXref, Literal("DB:" + "9" * 10000)))
    )
    assert check(huge, limits=Limits(max_bytes=512)).report.outcome == "exhausted"
    duplicate = check_cross_references(view, [E.Term, E.Term], REGISTRY, rules=RULES)
    assert duplicate.report.outcome == "input_error"


def test_catalogue_longest_iri_prefix_covers_registered_nested_namespaces() -> None:
    native()
    nested = str(DB) + "nested/"
    registry = IdentifierRegistry(
        (
            NamespaceContribution(str(DB), "DB", "caller"),
            NamespaceContribution(nested, "Nested", "caller"),
        )
    )
    rules = (*RULES, AccessionRule(nested, "[0-9]+"))
    view = inspect_vocabulary(Graph().add((E.Term, OIO.hasDbXref, Literal("Nested:1"))))
    supplied = inspect_vocabulary(
        Graph().add((URIRef(nested + "1"), RDF.type, OWL.Class))
    )
    parent = LocalCatalogue(str(DB), supplied, True)
    existing = check_cross_references(
        view, [E.Term], registry, rules=rules, catalogues=[parent]
    )
    assert existing.report.verified
    assert existing.observations[0].resolution == "resolved"
    absent = replace(parent, view=inspect_vocabulary(Graph()))
    missing = check_cross_references(
        view, [E.Term], registry, rules=rules, catalogues=[absent]
    )
    assert missing.report.outcome == "disagreement"
    assert missing.observations[0].resolution == "dangling"
    # An explicitly narrower open catalogue owns its scope, in either order.
    child = LocalCatalogue(nested, inspect_vocabulary(Graph()), False)
    for catalogues in ([parent, child], [child, parent]):
        chosen = check_cross_references(
            view, [E.Term], registry, rules=rules, catalogues=catalogues
        )
        assert not chosen.report.verified
        assert chosen.observations[0].catalogue is child
        assert chosen.observations[0].resolution == "unknown"


def test_valid_unsupported_scope_is_unknown_and_invalid_metadata_is_input() -> None:
    native()
    dataset = Dataset()
    dataset.graph(E.graph).add((E.Term, OIO.hasDbXref, Literal("DB:1")))
    union = inspect_vocabulary(load_vocabulary(dataset, union=True))
    result = check(union)
    assert result.report.outcome == "unverified" and not result.report.verified
    assert not result.observations
    source = prepare_source(Graph().add((E.Term, OIO.hasDbXref, Literal("DB:1"))))
    source.graph_fidelity = "nonempty-only"
    limited = check(inspect_vocabulary(load_vocabulary(source)))
    assert limited.report.outcome == "unverified"
    # Public descriptive metadata still requires its declared scalar type.
    source.graph_fidelity = False  # type: ignore[assignment]
    invalid = check(inspect_vocabulary(load_vocabulary(source)))
    assert invalid.report.outcome == "input_error"


def test_unrelated_annotation_validity_is_observational_to_xref_policy() -> None:
    native()
    graph = Graph().add((E.Term, OIO.hasDbXref, Literal("DB:1")))
    graph.add((E.Term, DEFINITION, E.NonLiteralDefinition))
    result = check(inspect_vocabulary(graph))
    assert result.report.verified
    assert result.observations[0].resolution == "external"
    assert result.obo_records
    assert any(
        issue.code == "obo.value_kind"
        for native_records in result.obo_records
        for issue in native_records.report.issues
    )
    # Known attachment flaws remain required even beside unrelated bad text.
    attachment = annotate(graph, DEFINITION, "definition", "DB:1")
    graph.add((attachment, OWL.annotatedTarget, Literal("ambiguous")))
    malformed = check(inspect_vocabulary(graph))
    assert not malformed.report.verified
    assert any(o.resolution == "unknown" for o in malformed.observations)


def test_alternate_iri_resolves_only_its_exact_supplied_representation() -> None:
    native()
    pytest.importorskip("uri_template")
    from bijux_ontology.vocabulary.templates.expansion import IdentifierTemplate
    from bijux_ontology.vocabulary.templates.types import TemplateKey

    alternate_namespace = "https://resolver.example/record/"
    alternate = URIRef(alternate_namespace + "1")
    rules = (
        AccessionRule(
            str(DB),
            "[0-9]+",
            alternates=(
                IdentifierTemplate(
                    alternate_namespace + "{local}", (TemplateKey("local"),)
                ),
            ),
        ),
    )
    view = inspect_vocabulary(Graph().add((E.Term, OIO.hasDbXref, alternate)))
    exact = LocalCatalogue(
        alternate_namespace,
        inspect_vocabulary(Graph().add((alternate, RDF.type, OWL.Class))),
        True,
    )
    result = check_cross_references(
        view, [E.Term], REGISTRY, rules=rules, catalogues=[exact]
    )
    assert result.report.verified and result.observations[0].resolution == "resolved"
    assert result.observations[0].iri == alternate
    assert result.references[0].assertion.object == alternate
    canonical_only = check_cross_references(
        view, [E.Term], REGISTRY, rules=rules, catalogues=[catalogue("1")]
    )
    assert canonical_only.report.verified
    assert canonical_only.observations[0].resolution == "external"
    assert canonical_only.observations[0].iri == alternate
