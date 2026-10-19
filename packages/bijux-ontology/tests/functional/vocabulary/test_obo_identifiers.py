"""Alternate accessions are explicit assertions, never replacement guesses."""

import pytest
from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.declarations import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.declarations.value_constraints import ValueConstraints
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.obo import resolve_alternative_ids
from bijux_ontology.vocabulary.registry import IdentifierRegistry, NamespaceContribution
from bijux_ontology.vocabulary.sources.loading import load_vocabulary

E = Namespace("urn:accession:")
ALIAS = URIRef("http://www.geneontology.org/formats/oboInOwl#hasAlternativeId")


def test_original_aliases_preserve_batches_state_and_actual_field_choices() -> None:
    graph = Graph().add((E.Current, RDF.type, OWL.Class))
    graph.add((E.Current, ALIAS, Literal("EX:old")))
    graph.add((E.Current, OWL.deprecated, Literal(False)))
    graph.add((E.Old, RDF.type, OWL.Class)).add((E.Old, OWL.deprecated, Literal(True)))
    graph.add((E.Old, E.replacedBy, E.Current))
    graph.add((E.Current, RDFS.label, Literal("same label")))
    view = inspect_vocabulary(graph)
    resolved = resolve_alternative_ids(view, ("EX:old", "EX:old"))
    assert resolved.report.verified
    assert [r.identifier for r in resolved.items] == ["EX:old", "EX:old"]
    assert [r.status for r in resolved.items] == ["unique", "unique"]
    candidate = resolved.items[0].candidates[0]
    assert candidate.term.iri == str(E.Current)
    assert candidate.deprecation.state == "active"
    assert candidate.assertions[0].object == Literal("EX:old")
    assert (
        resolve_alternative_ids(view, (str(E.Old), "same label")).report.outcome
        == "unverified"
    )
    assert resolve_alternative_ids(view, ("ex:old",)).items[0].status == "missing"
    model = ModelDeclaration(
        "record",
        (str(E.Record),),
        (
            FieldDeclaration(
                "kind",
                str(E.kind),
                (
                    ValueDeclaration(
                        "iri", constraints=ValueConstraints(enumeration=(E.Current,))
                    ),
                ),
            ),
        ),
    )
    for value, accepted in ((URIRef(candidate.term.iri), True), (E.Old, False)):
        source = prepare_rdf(
            Graph().add((E.record, RDF.type, E.Record)).add((E.record, E.kind, value))
        )
        report = check_rdf_emission(
            source, (model,), (RdfModelUsage("record", E.record),)
        )
        assert report.verified is accepted
    assert (E.Current, ALIAS, Literal("EX:old")) in graph


def test_distinct_canonical_targets_scopes_and_explicit_identifier_intent() -> None:
    local = (
        Graph().add((E.A, RDF.type, OWL.Class)).add((E.A, ALIAS, Literal("urn:old")))
    )
    lookup = (
        Graph().add((E.B, RDF.type, OWL.Class)).add((E.B, ALIAS, Literal("urn:old")))
    )
    vocabulary = load_vocabulary(local, lookup=[lookup])
    local_only = inspect_vocabulary(vocabulary)
    assert resolve_alternative_ids(local_only, ("urn:old",)).items[0].status == "unique"
    both = inspect_vocabulary(vocabulary, scopes=("local", "lookup"))
    ambiguous = resolve_alternative_ids(both, ("urn:old",))
    assert ambiguous.items[0].status == "ambiguous"
    assert len(ambiguous.items[0].candidates) == 2
    assert any(i.code == "obo.alternative_id_ambiguous" for i in ambiguous.report)
    duplicate = inspect_vocabulary(
        load_vocabulary(local, lookup=[local]), scopes=("local", "lookup")
    )
    one = resolve_alternative_ids(duplicate, ("urn:old",)).items[0]
    assert one.status == "unique" and len(one.candidates[0].assertions) == 2
    assert one.candidates[0].deprecation.state == "absent"
    registry = IdentifierRegistry(
        (NamespaceContribution("https://example.org/id/", "urn", "consumer", ("ex",)),)
    )
    assert (
        resolve_alternative_ids(
            local_only, ("ex:old",), mode="curie", registry=registry
        )
        .items[0]
        .status
        == "unique"
    )
    assert (
        resolve_alternative_ids(local_only, ("urn:old",), mode="iri").items[0].status
        == "unique"
    )
    assert (
        resolve_alternative_ids(local_only, ("https://example.org/id/old",), mode="iri")
        .items[0]
        .status
        == "missing"
    )
    with pytest.raises(InputError):
        resolve_alternative_ids(local_only, ("urn:old",), mode="curie")
    with pytest.raises(InputError):
        resolve_alternative_ids(local_only, ("urn:old",), registry=registry)


def test_malformed_aliases_and_exhausted_collision_tails_cannot_prove_uniqueness() -> (
    None
):
    graph = Graph().add((E.A, RDF.type, OWL.Class)).add((E.A, ALIAS, Literal("EX:old")))
    graph.add((E.B, RDF.type, OWL.Class)).add((E.B, ALIAS, Literal("EX:old")))
    view = inspect_vocabulary(graph)
    outcomes = set()
    for work in range(1, 1600, 19):
        result = resolve_alternative_ids(
            view, ("EX:old", "EX:old"), budget=Budget(Limits(max_work=work))
        )
        outcomes.add(result.report.outcome)
        assert not result.report.verified
        if not result.report.complete:
            assert all(item.status == "incomplete" for item in result.items)
        else:
            assert all(item.status == "ambiguous" for item in result.items)
    assert outcomes == {"exhausted", "disagreement"}
    for subject, value in (
        (BNode(), Literal("EX:old")),
        (E.Other, Literal("EX:old", lang="en")),
        (E.Other, URIRef("EX:old")),
    ):
        malformed = inspect_vocabulary(Graph().add((subject, ALIAS, value)))
        assert (
            resolve_alternative_ids(malformed, ("EX:old",)).report.outcome
            == "unverified"
        )
    unknown_prefix = inspect_vocabulary(graph.add((E.C, ALIAS, Literal("OTHER:old"))))
    registry = IdentifierRegistry(
        (NamespaceContribution("urn:example:", "EX", "consumer"),)
    )
    assert not resolve_alternative_ids(
        unknown_prefix, ("EX:old",), mode="curie", registry=registry
    ).report.verified
    assert not resolve_alternative_ids(view, ()).report.verified


def test_curie_expansion_bounds_utf8_and_retains_asserted_alias_exhaustion() -> None:
    for namespace, suffix in (
        ("urn:" + "😀" * 150, "😀" * 150),
        ("urn:" + "a" * 600, "a" * 600),
    ):
        registry = IdentifierRegistry(
            (NamespaceContribution(namespace, "EX", "consumer"),)
        )
        query = inspect_vocabulary(Graph().add((E.A, RDF.type, OWL.Class)))
        result = resolve_alternative_ids(
            query,
            ("EX:" + suffix,),
            mode="curie",
            registry=registry,
            budget=Budget(Limits(max_bytes=1000)),
        )
        assert result.report.outcome == "exhausted"
        assert all(issue.actual is None for issue in result.report)
        source = inspect_vocabulary(
            Graph()
            .add((E.A, RDF.type, OWL.Class))
            .add((E.A, ALIAS, Literal("EX:" + suffix)))
        )
        selected = resolve_alternative_ids(
            source,
            ("EX:small",),
            mode="curie",
            registry=registry,
            budget=Budget(Limits(max_bytes=1000)),
        )
        assert selected.report.outcome == "exhausted"
        assert all(item.status == "incomplete" for item in selected.items)


def test_completed_candidate_prefix_survives_later_state_exhaustion() -> None:
    graph = Graph().add((E.A, RDF.type, OWL.Class)).add((E.A, ALIAS, Literal("EX:old")))
    view = inspect_vocabulary(graph)
    graph.add((E.B, RDF.type, OWL.Class)).add((E.B, ALIAS, Literal("EX:other")))
    view = inspect_vocabulary(graph)
    retained = []
    for states in range(1, 100):
        result = resolve_alternative_ids(
            view, ("EX:old", "EX:other"), budget=Budget(Limits(max_states=states))
        )
        if (
            result.report.outcome == "exhausted"
            and result.items
            and result.items[0].candidates
        ):
            retained.append(result)
            assert result.items[0].status == "incomplete"
            assert result.items[0].candidates[0].term.iri == str(E.A)
    assert retained
