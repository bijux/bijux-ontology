"""Installed OAK consumers over real prepared Bijux source facts and failures."""

from __future__ import annotations

from collections.abc import Callable
from itertools import repeat
from typing import TYPE_CHECKING, cast

import pytest
from rdflib import Dataset, Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.interfaces.adapters.oak import OakOperationError, as_oak
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.registry.types import (
    IdentifierRegistry,
    NamespaceContribution,
)
from bijux_ontology.vocabulary.restrictions import project_restrictions
from bijux_ontology.vocabulary.sources.loading import load_vocabulary

BasicOntologyInterface = pytest.importorskip("oaklib.interfaces").BasicOntologyInterface

if TYPE_CHECKING:
    from bijux_ontology.interfaces.adapters.oak.adapter import BijuxBasicOntology

_SOURCE = """@prefix ex: <https://example.invalid/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix oio: <http://www.geneontology.org/formats/oboInOwl#> .
@prefix iao: <http://purl.obolibrary.org/obo/IAO_> .
ex:A a owl:Class ; rdfs:label "Alpha"@en, "Alfa"@de ;
 iao:0000115 "Definition"@en ; oio:hasExactSynonym "Shared name"@en ;
 oio:hasBroadSynonym "Shared name"@en ; rdfs:subClassOf ex:B,
 [ a owl:Restriction ; owl:onProperty ex:part_of ; owl:someValuesFrom ex:C ] ;
 oio:inSubset ex:slim .
[ a owl:Axiom ; owl:annotatedSource ex:A ; owl:annotatedProperty iao:0000115 ;
 owl:annotatedTarget "Definition"@en ; oio:hasDbXref "PMID:123" ] .
ex:B a owl:Class ; rdfs:label "Beta"@en .
ex:C a owl:Class ; rdfs:label "Gamma"@en .
ex:Child a owl:Class ; rdfs:subClassOf ex:A .
ex:Old a owl:Class ; owl:deprecated true .
ex:part_of a owl:ObjectProperty .
ex:slim a owl:AnnotationProperty ; rdfs:subPropertyOf oio:SubsetProperty ;
 rdfs:label "Example slim"@en .
ex:i a owl:NamedIndividual, ex:A ; ex:part_of ex:j .
ex:j a owl:NamedIndividual .
owl:Thing a owl:Class .
"""


def _registry() -> IdentifierRegistry:
    return IdentifierRegistry(
        tuple(
            NamespaceContribution(namespace, prefix, "consumer")
            for prefix, namespace in (
                ("EX", "https://example.invalid/"),
                ("rdfs", str(RDFS)),
                ("owl", str(OWL)),
                ("rdf", str(RDF)),
                ("oio", "http://www.geneontology.org/formats/oboInOwl#"),
            )
        )
    )


def _view(extra: str = "") -> VocabularyView:
    return VocabularyView(
        load_vocabulary(Graph().parse(data=_SOURCE + extra, format="turtle"))
    )


def _adapter(
    view: VocabularyView | None = None,
    *,
    closed_subsets: bool = True,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> BijuxBasicOntology:
    prepared = as_oak(
        view if view is not None else _view(),
        registry=_registry(),
        language="en",
        closed_subsets=closed_subsets,
        limits=limits,
        budget=budget,
    )
    assert prepared.report.verified and prepared.adapter is not None
    assert issubclass(type(prepared.adapter), BasicOntologyInterface)
    return prepared.adapter


def test_installed_basic_consumers_match_scoped_metadata_and_membership() -> None:
    adapter = _adapter()
    assert adapter.curie_to_uri("EX:A") == "https://example.invalid/A"
    assert adapter.uri_to_curie("https://example.invalid/A") == "EX:A"
    assert adapter.curie_to_uri("UNKNOWN:A") is None
    assert list(adapter.labels(("EX:A", "EX:B"))) == [
        ("EX:A", "Alpha"),
        ("EX:B", "Beta"),
    ]
    # Real installed convenience consumers call the concrete provider.
    assert set(BasicOntologyInterface.entity_aliases(adapter, "EX:A")) == {
        "Alpha",
        "Alfa",
        "Shared name",
    }
    assert set(BasicOntologyInterface.outgoing_relationships(adapter, "EX:A")) == {
        ("EX:part_of", "EX:C"),
        ("rdfs:subClassOf", "EX:B"),
    }
    aliases = adapter.entity_alias_map("EX:A")
    assert (
        aliases["oio:hasExactSynonym"]
        == aliases["oio:hasBroadSynonym"]
        == ["Shared name"]
    )
    assert list(adapter.definitions(("EX:A",), include_metadata=True)) == [
        ("EX:A", "Definition", {"oio:hasDbXref": ["PMID:123"]})
    ]
    assert list(adapter.subsets()) == ["EX:slim"]
    assert list(adapter.subset_members("EX:slim")) == ["EX:A"]
    assert list(adapter.terms_subsets(("EX:A", "EX:B"))) == [("EX:A", "EX:slim")]
    assert list(adapter.obsoletes()) == ["EX:Old"]
    open_subsets = _adapter(closed_subsets=False)
    assert open_subsets.label("EX:A") == "Alpha"
    with pytest.raises(OakOperationError) as caught:
        list(open_subsets.subset_members("EX:slim"))
    assert caught.value.report.outcome == "unverified"
    assert "EX:Old" not in adapter.entities()
    assert "owl:Thing" not in adapter.entities()
    assert "EX:Old" in adapter.entities(filter_obsoletes=False)
    assert ("EX:A", "owl:Class") in adapter.owl_types(("EX:A",))
    evidence = project_restrictions(adapter.view, "https://example.invalid/A")
    assert evidence.report.verified and evidence.items[0].authored_owner == URIRef(
        "https://example.invalid/A"
    )


def test_batch_language_and_ambiguous_scalars_do_not_use_native_shortcuts() -> None:
    adapter = _adapter()
    assert adapter.label("EX:A", lang="de") == "Alfa"
    assert list(adapter.labels(("EX:A",), lang="de")) == [("EX:A", "Alfa")]
    assert list(adapter.definitions(("EX:A",), lang="de", include_missing=True)) == [
        ("EX:A", None, {})
    ]
    assert adapter.label("EX:Child") is None
    with pytest.raises(KeyError):
        adapter.label("EX:Missing")
    ambiguous = _adapter(_view('ex:A rdfs:label "Alternative"@en .'))
    with pytest.raises(OakOperationError) as caught:
        ambiguous.labels(("EX:B", "EX:A"))
    assert caught.value.report.outcome == "unverified"


def test_tbox_abox_and_authored_existential_meanings_remain_distinct() -> None:
    adapter = _adapter(
        _view(
            "ex:Direct a owl:Class ; ex:part_of ex:C . "
            "ex:Implicit rdfs:subClassOf ex:C . ex:participant a ex:Implicit ."
        )
    )
    tbox = set(adapter.relationships(include_abox=False))
    assert ("EX:A", "EX:part_of", "EX:C") in tbox
    assert ("EX:Direct", "EX:part_of", "EX:C") not in tbox
    assert ("EX:Child", "EX:part_of", "EX:C") not in tbox
    abox = set(adapter.relationships(include_tbox=False))
    assert ("EX:i", "EX:part_of", "EX:j") in abox
    assert ("EX:participant", "rdf:type", "EX:Implicit") in abox
    assert ("EX:participant", "rdf:type", "EX:Implicit") not in tbox
    assert list(adapter.owl_types(("EX:participant",))) == []
    assert ("EX:A", "EX:part_of", "EX:C") not in abox
    assert set(adapter.relationships(subjects=(), include_tbox=False)) == set()
    assert set(
        adapter.relationships(
            subjects=("EX:C",), predicates=("EX:part_of",), invert=True
        )
    ) == {("EX:C", "EX:part_of", "EX:A"), ("EX:C", "EX:part_of", "EX:Direct")}
    for entailed, exclude_blank in ((True, True), (False, False)):
        with pytest.raises(OakOperationError) as caught:
            list(
                adapter.relationships(
                    include_entailed=entailed, exclude_blank=exclude_blank
                )
            )
        assert caught.value.report.outcome == "unverified"


def test_universal_and_unsupported_constructors_never_become_existential_edges() -> (
    None
):
    universal = _adapter(
        _view(
            "ex:Universal a owl:Class ; rdfs:subClassOf [ a owl:Restriction ; "
            "owl:onProperty ex:part_of ; owl:allValuesFrom ex:C ] ."
        )
    )
    assert (
        list(
            universal.relationships(
                subjects=("EX:Universal",), predicates=("EX:part_of",)
            )
        )
        == []
    )
    unsupported = _adapter(
        _view(
            "ex:Unsupported a owl:Class ; rdfs:subClassOf [ a owl:Restriction ; "
            "owl:onProperty ex:part_of ; "
            "owl:someValuesFrom [ owl:unionOf ( ex:B ex:C ) ] ] ."
        )
    )
    with pytest.raises(OakOperationError) as caught:
        list(unsupported.relationships(subjects=("EX:Unsupported",)))
    assert not caught.value.report.verified
    # A different exact relation scope does not require an existential projection.
    assert (
        list(
            unsupported.relationships(
                subjects=("EX:Unsupported",), predicates=("rdfs:subClassOf",)
            )
        )
        == []
    )


def test_selected_dataset_scope_excludes_other_metadata_and_subset_members() -> None:
    dataset = Dataset()
    dataset.graph(URIRef("urn:graph:selected")).parse(data=_SOURCE, format="turtle")
    dataset.graph(URIRef("urn:graph:excluded")).parse(
        data="@prefix ex: <https://example.invalid/> . "
        "@prefix oio: <http://www.geneontology.org/formats/oboInOwl#> . "
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> . "
        'ex:A rdfs:label "Excluded"@en . ex:B oio:inSubset ex:slim .',
        format="turtle",
    )
    view = VocabularyView(
        load_vocabulary(dataset, graph_name=URIRef("urn:graph:selected"))
    )
    adapter = _adapter(view)
    assert adapter.label("EX:A") == "Alpha"
    assert list(adapter.subset_members("EX:slim")) == ["EX:A"]
    with pytest.raises(OakOperationError):
        _adapter(VocabularyView(load_vocabulary(dataset, union=True))).label("EX:A")


def test_data_integrity_and_budget_failures_cannot_return_empty_success() -> None:
    invalid = object()
    with pytest.raises(InputError):
        as_oak(_view(), registry=_registry(), budget=cast(Budget, invalid))
    with pytest.raises(InputError):
        as_oak(_view(), registry=_registry(), limits=cast(Limits, invalid))
    integrity_cases: tuple[tuple[str, Callable[[BijuxBasicOntology], object]], ...] = (
        ("ex:A oio:inSubset ex:undefined .", lambda adapter: list(adapter.subsets())),
        ("ex:Old owl:deprecated false .", lambda adapter: list(adapter.obsoletes())),
        (
            "[ a owl:Axiom ; owl:annotatedSource ex:A ; "
            "owl:annotatedProperty iao:0000115 ; "
            'owl:annotatedTarget "Definition"@en ; oio:hasDbXref "PMID:456" ] .',
            lambda adapter: list(adapter.definitions(("EX:A",), include_metadata=True)),
        ),
    )
    for extra, operation in integrity_cases:
        with pytest.raises(OakOperationError) as caught:
            operation(_adapter(_view(extra)))
        assert not caught.value.report.verified
    adapter = _adapter(limits=Limits(max_states=30))
    with pytest.raises(OakOperationError) as caught:
        adapter.labels(repeat("EX:A"))
    assert caught.value.report.outcome == "exhausted"
    shared = Budget(Limits(max_work=5000))
    adapter = _adapter(budget=shared)
    with pytest.raises(OakOperationError) as caught:
        for _ in range(100):
            adapter.label("EX:A")
    assert caught.value.report.outcome == "exhausted"
    prepared = as_oak(_view(), registry=_registry(), limits=Limits(max_work=1))
    assert prepared.adapter is None and prepared.report.outcome == "exhausted"
    tiny = as_oak(
        _view(), registry=IdentifierRegistry(()), limits=Limits(max_bytes=1, max_work=1)
    ).adapter
    assert tiny is not None
    tiny_calls: tuple[Callable[[], object], ...] = (
        lambda: list(tiny.relationships(include_entailed=True)),
        lambda: tiny.set_label("EX:A", "Changed"),
    )
    for invoke in tiny_calls:
        with pytest.raises(OakOperationError) as caught:
            invoke()
        assert caught.value.report.outcome == "exhausted"
        assert all(issue.kind != "unverified" for issue in caught.value.report.issues)
    malformed = VocabularyView(
        load_vocabulary(
            Graph().parse(
                data="<urn:ex:A> a <http://www.w3.org/2002/07/owl#Class> ; "
                "<http://purl.obolibrary.org/obo/IAO_0000115> "
                "<urn:bad-definition>, <urn:another-bad-definition> .",
                format="turtle",
            )
        )
    )
    # The second malformed definition stops diagnostics after a paid first finding.
    bounded = as_oak(
        malformed, registry=IdentifierRegistry(()), limits=Limits(max_issues=1)
    ).adapter
    assert bounded is not None
    with pytest.raises(OakOperationError) as caught:
        bounded.definition("urn:ex:A")
    assert caught.value.report.scope == "obo.records"
    assert {"finding", "exhausted"} <= {
        issue.kind for issue in caught.value.report.issues
    }
    assert caught.value.report.issues[0].code == "obo.value_kind"


def test_mutation_and_inference_attempts_preserve_the_owned_source() -> None:
    adapter = _adapter()
    before = adapter.view.all_statements().items
    for operation in (
        lambda: adapter.set_label("EX:A", "Changed"),
        lambda: adapter.create_entity("EX:Created"),
        lambda: adapter.delete_entity("EX:A"),
        lambda: adapter.add_missing_property_values("EX:A", {}),
        lambda: adapter.save(),
        lambda: adapter.clone("https://example.invalid/remote"),
        lambda: adapter.query("SELECT ?x WHERE {?x ?p ?o}"),
        lambda: adapter.entailed_edge_index,
        lambda: list(adapter.obsoletes(include_merged=False)),
    ):
        with pytest.raises(OakOperationError) as caught:
            operation()
        assert caught.value.report.outcome == "unverified"
    assert adapter.view.all_statements().items == before
    assert adapter.label("EX:A") == "Alpha"
