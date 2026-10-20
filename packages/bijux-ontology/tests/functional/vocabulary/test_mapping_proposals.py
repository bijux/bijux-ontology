"""Public lexical candidates remain advisory until explicitly reviewed."""

import importlib.util
from dataclasses import replace
from threading import Event
from typing import cast

import pytest
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.lexical import LexicalIndex, prepare_lexical_index
from bijux_ontology.vocabulary.mappings import (
    MappingPolicy,
    MappingSet,
    check_mappings,
    export_sssom,
)
from bijux_ontology.vocabulary.mappings.proposals import (
    MappingApproval,
    MappingProposals,
    approve_mapping_proposals,
    propose_lexical_mappings,
)
from bijux_ontology.vocabulary.registry import IdentifierRegistry, NamespaceContribution

SOURCE = Namespace("urn:source:")
TARGET = Namespace("urn:target:")
EXACT = URIRef("http://www.geneontology.org/formats/oboInOwl#hasExactSynonym")
BROAD = URIRef("http://www.geneontology.org/formats/oboInOwl#hasBroadSynonym")
JUSTIFICATION = "https://w3id.org/semapv/vocab/ManualMappingCuration"
LICENSE = "https://creativecommons.org/publicdomain/zero/1.0/"


def _graph(namespace: Namespace, *, source: bool) -> Graph:
    """Independently author homonyms and language/role-specific lexical evidence."""
    graph = Graph()
    values = (
        (("A", "Cardiac", EXACT), ("B", "Shared", BROAD))
        if source
        else (
            ("C", "Cardiac", RDFS.label),
            ("D", "Shared", RDFS.label),
            ("E", "Shared", RDFS.label),
            ("French", "Cardiac", RDFS.label),
        )
    )
    for name, value, predicate in values:
        graph.add((namespace[name], RDF.type, OWL.Class)).add(
            (
                namespace[name],
                predicate,
                Literal(value, lang="fr" if name == "French" else "en"),
            )
        )
    return graph


def _index(graph: Graph, *, fold: bool = False) -> LexicalIndex:
    """Use ordinary generic literal roles; no OBO adapter dependency is needed."""
    result = prepare_lexical_index(
        inspect_vocabulary(graph),
        fields=(str(RDFS.label), str(EXACT), str(BROAD)),
        casefold=fold,
    )
    assert result.index is not None
    return result.index


def _registry() -> IdentifierRegistry:
    """Give every serialized identifier an exact caller-owned prefix binding."""
    return IdentifierRegistry(
        tuple(
            NamespaceContribution(namespace, prefix, "consumer")
            for prefix, namespace in (
                ("source", str(SOURCE)),
                ("target", str(TARGET)),
                ("skos", str(SKOS)),
                ("semapv", "https://w3id.org/semapv/vocab/"),
            )
        )
    )


def test_exact_synonym_homonyms_languages_and_roles_are_only_candidates() -> None:
    """Full ambiguity and original role evidence survive an indexed lexical join."""
    source, target = (
        _index(_graph(SOURCE, source=True)),
        _index(_graph(TARGET, source=False)),
    )
    result = propose_lexical_mappings(source, target)
    assert result.report.verified and len(result.items) == 3
    exact = next(item for item in result.items if item.source.field == str(EXACT))
    assert exact.target.term.iri == str(TARGET.C) and exact.comparison == "exact"
    assert exact.source.scope == "local" and exact.source.language == "en"
    assert {
        item.target.term.iri for item in result.items if item.source.field == str(BROAD)
    } == {str(TARGET.D), str(TARGET.E)}
    assert len(propose_lexical_mappings(source, target, language="any").items) == 4
    with pytest.raises(InputError):
        MappingProposals()
    with pytest.raises(TypeError):
        replace(result, items=())
    assert (
        check_mappings(
            cast(MappingSet, result),
            subject_vocabulary=inspect_vocabulary(Graph()),
            object_vocabulary=inspect_vocabulary(Graph()),
            policy=MappingPolicy((str(SKOS.exactMatch),)),
        ).report.outcome
        == "input_error"
    )


def test_explicit_casefold_ranking_empty_and_shared_cancellation() -> None:
    """Casefold is selected before preparation; interrupted groups are not truth."""
    first, second = Graph(), Graph()
    for graph, term, label in (
        (first, SOURCE.A, "Straße"),
        (second, TARGET.A, "STRASSE"),
        (second, TARGET.B, "Straße"),
    ):
        graph.add((term, RDF.type, OWL.Class)).add(
            (term, RDFS.label, Literal(label, lang="EN" if graph is first else "en"))
        )
    result = propose_lexical_mappings(
        _index(first, fold=True), _index(second, fold=True)
    )
    assert result.report.verified and [item.comparison for item in result.items] == [
        "exact",
        "casefold",
    ]
    assert [item.rank[0] for item in result.items] == [0, 1]
    assert all(
        item.source.language == "EN" and item.target.language == "en"
        for item in result.items
    )
    assert len(propose_lexical_mappings(_index(first), _index(second)).items) == 1
    with pytest.raises(InputError):
        propose_lexical_mappings(_index(first), _index(second, fold=True))
    empty = propose_lexical_mappings(_index(Graph()), _index(second))
    assert empty.report.complete and not empty.items
    event = Event()
    event.set()
    assert (
        propose_lexical_mappings(
            _index(first), _index(second), cancel=event
        ).report.outcome
        == "exhausted"
    )
    limited = propose_lexical_mappings(
        _index(first), _index(second), budget=Budget(Limits(max_total_bytes=64))
    )
    assert limited.report.outcome == "exhausted" and not limited.items


def test_reviewed_native_rows_preserve_explicit_predicate_and_qualifications() -> None:
    """Only approved pairs enter maintained SSSOM; consumer policy stays separate."""
    if importlib.util.find_spec("sssom") is None:
        pytest.skip("Native mapping approval requires the sssom extra")
    source_graph, target_graph = (
        _graph(SOURCE, source=True),
        _graph(TARGET, source=False),
    )
    proposals = propose_lexical_mappings(_index(source_graph), _index(target_graph))
    chosen = next(item for item in proposals.items if item.source.field == str(EXACT))
    approved = MappingApproval(
        chosen,
        str(SKOS.closeMatch),
        JUSTIFICATION,
        (("comment", "reviewed exact synonym"),),
    )
    result = approve_mapping_proposals(
        proposals,
        (approved,),
        registry=_registry(),
        mapping_set_id="urn:review:mappings",
        license=LICENSE,
    )
    assert result.report.verified, result.report.to_dict()
    assert result.mapping_set is not None and len(result.mapping_set.rows) == 1
    row = result.mapping_set.rows[0]
    assert (row.subject, row.predicate, row.object) == (
        str(SOURCE.A),
        str(SKOS.closeMatch),
        str(TARGET.C),
    )
    assert dict(row.cells)[
        "comment"
    ] == "reviewed exact synonym" and "confidence" not in dict(row.cells)
    assert export_sssom(result.mapping_set).report.verified
    checked = check_mappings(
        result.mapping_set,
        subject_vocabulary=inspect_vocabulary(source_graph),
        object_vocabulary=inspect_vocabulary(target_graph),
        policy=MappingPolicy((str(SKOS.closeMatch),)),
    )
    assert checked.report.verified
    for approvals in (
        (),
        (replace(approved, proposal=replace(chosen)),),
        (replace(approved, qualifications=(("subject_id", "target:E"),)),),
    ):
        invalid = approve_mapping_proposals(
            proposals,
            approvals,
            registry=_registry(),
            mapping_set_id="urn:review:mappings",
            license=LICENSE,
        )
        assert invalid.mapping_set is None and invalid.report.outcome == "input_error"
    budget = Budget(Limits(max_work=100))
    exhausted = approve_mapping_proposals(
        proposals,
        (approved,),
        registry=_registry(),
        mapping_set_id="urn:review:mappings",
        license=LICENSE,
        budget=budget,
    )
    assert exhausted.mapping_set is None and exhausted.report.outcome == "exhausted"


def test_native_absence_is_honest_and_advisory_generation_still_works() -> None:
    """Core-only consumers can review candidates without an installed SSSOM engine."""
    if importlib.util.find_spec("sssom") is not None:
        return
    proposals = propose_lexical_mappings(
        _index(_graph(SOURCE, source=True)), _index(_graph(TARGET, source=False))
    )
    assert proposals.report.verified
    result = approve_mapping_proposals(
        proposals,
        (MappingApproval(proposals.items[0], str(SKOS.closeMatch), JUSTIFICATION, ()),),
        registry=_registry(),
        mapping_set_id="urn:review:mappings",
        license=LICENSE,
    )
    assert result.mapping_set is None and result.report.outcome == "unverified"
