"""Authoring hierarchy bases and composed mapping conditions keep distinct scope."""

from typing import Any
from typing import Literal as PolicyLiteral

import pytest
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, SKOS

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.limits import Limits
from bijux_ontology.skos.checking import check_skos

EX = Namespace("https://policy.example/")


def _schemes() -> Graph:
    return Graph().add((EX.A, SKOS.inScheme, EX.One)).add((EX.B, SKOS.inScheme, EX.Two))


def test_mapping_hierarchy_and_authored_scheme_policy_have_explicit_bases() -> None:
    graph = _schemes().add((EX.A, SKOS.broadMatch, EX.B))
    before = frozenset(graph)
    authored = check_skos(
        graph, rules=("same-scheme-hierarchy",), hierarchy_basis="asserted"
    )
    assert authored.verified
    assert "hierarchy-policy:asserted" in authored.families
    effective = check_skos(graph, rules=("same-scheme-hierarchy",))
    assert [i.code for i in effective.issues] == ["skos.policy.cross_scheme_hierarchy"]
    assert "hierarchy-policy:effective" in effective.families
    assert check_skos(graph).verified and frozenset(graph) == before
    for predicate, subject, target in (
        (SKOS.broader, EX.A, EX.B),
        (SKOS.narrower, EX.B, EX.A),
    ):
        authored_graph = _schemes().add((subject, predicate, target))
        assert (
            check_skos(
                authored_graph,
                rules=("same-scheme-hierarchy",),
                hierarchy_basis="asserted",
            ).outcome
            == "disagreement"
        )


def test_semantic_mapping_consequences_keep_both_policy_bases() -> None:
    graph = Graph().add((EX.A, SKOS.broadMatch, EX.B)).add((EX.A, SKOS.related, EX.B))
    bases: tuple[PolicyLiteral["asserted", "effective"], ...] = (
        "asserted",
        "effective",
    )
    for basis in bases:
        report = check_skos(graph, rules=("relation-integrity",), hierarchy_basis=basis)
        assert [i.code for i in report.issues] == ["skos.related_hierarchy_conflict"]
    assert check_skos(graph).outcome == "disagreement"


def test_cycle_and_isolation_policies_use_the_selected_hierarchy_basis() -> None:
    graph = (
        Graph().add((EX.A, SKOS.broadMatch, EX.B)).add((EX.B, SKOS.broadMatch, EX.A))
    )
    assert check_skos(graph).verified
    assert check_skos(graph, rules=("acyclic",), hierarchy_basis="asserted").verified
    assert check_skos(graph, rules=("acyclic",)).outcome == "disagreement"
    assert (
        check_skos(graph, rules=("isolated",), hierarchy_basis="asserted").outcome
        == "disagreement"
    )
    assert check_skos(graph, rules=("isolated",)).verified
    graph.add((EX.A, SKOS.broaderTransitive, EX.B))
    graph.add((EX.B, SKOS.narrowerTransitive, EX.A))
    assert check_skos(graph, rules=("acyclic",), hierarchy_basis="asserted").verified
    graph.add((EX.B, SKOS.broaderTransitive, EX.A))
    assert (
        check_skos(graph, rules=("acyclic",), hierarchy_basis="asserted").outcome
        == "disagreement"
    )


@pytest.mark.parametrize(
    "first,second,code",
    (
        (SKOS.broadMatch, SKOS.relatedMatch, "skos.related_hierarchy_conflict"),
        (SKOS.exactMatch, SKOS.broadMatch, "skos.mapping_conflict"),
        (SKOS.exactMatch, SKOS.relatedMatch, "skos.mapping_conflict"),
    ),
)
def test_complete_mapping_profile_expands_conditions_and_anchors_incoming_clashes(
    first: URIRef, second: URIRef, code: str
) -> None:
    graph = Graph().add((EX.A, first, EX.B)).add((EX.A, second, EX.B))
    report = check_skos(graph, profile="mapping-integrity", concepts=(str(EX.B),))
    findings = [i for i in report.issues if i.code == code]
    assert len(findings) == 1 and findings[0].location == str(EX.B)
    assert set(report.families) == {
        "resource-integrity",
        "relation-integrity",
        "mapping-integrity",
        "mapping-endpoints",
        "profile:mapping-integrity",
    }
    assert check_skos(graph).outcome == "disagreement"
    if first == SKOS.broadMatch:
        assert check_skos(graph, rules=("mapping-integrity",)).verified


@pytest.mark.parametrize("incoming", (False, True))
@pytest.mark.parametrize("role", (SKOS.ConceptScheme, SKOS.Collection))
def test_profile_checks_incident_endpoint_roles_without_broadening_publication_scope(
    incoming: bool, role: URIRef
) -> None:
    graph = Graph().add((EX.A, SKOS.prefLabel, Literal("A", lang="en")))
    graph.add(
        (EX.B, SKOS.exactMatch, EX.A) if incoming else (EX.A, SKOS.exactMatch, EX.B)
    )
    graph.add((EX.B, RDF.type, role))
    graph.add((EX.Unrelated, RDF.type, SKOS.ConceptScheme))
    graph.add((EX.Unrelated, SKOS.exactMatch, EX.Other))
    report = check_skos(
        graph,
        profile="mapping-integrity",
        rules=("language-coverage",),
        languages=("en",),
        concepts=(str(EX.A),),
    )
    assert len(report.issues) == 1
    finding = report.issues[0]
    assert finding.code == "skos.resource_role_conflict"
    assert finding.location == str(EX.A) and finding.actual == str(EX.B)


def test_mapping_endpoints_require_resources_without_external_declarations() -> None:
    for predicate in (SKOS.mappingRelation, SKOS.exactMatch):
        graph = Graph().add((EX.A, predicate, EX.External))
        assert check_skos(
            graph, profile="mapping-integrity", concepts=(str(EX.A),)
        ).verified
        graph.add((EX.A, predicate, Literal("malformed")))
        report = check_skos(graph, profile="mapping-integrity", concepts=(str(EX.A),))
        assert len(report.issues) == 1 and report.outcome == "unverified"
        assert (
            report.issues[0].location == str(EX.A)
            and report.issues[0].actual == "malformed"
        )
    explicit = check_skos(
        Graph()
        .add((EX.A, SKOS.exactMatch, EX.B))
        .add((EX.B, RDF.type, SKOS.Collection)),
        rules=("mapping-endpoints",),
        concepts=(str(EX.A),),
    )
    assert explicit.outcome == "disagreement"


def test_profiles_preserve_core_defaults_and_reject_unknown_or_empty_selections() -> (
    None
):
    graph = (
        Graph()
        .add((EX.A, SKOS.prefLabel, Literal("A", lang="en")))
        .add((EX.A, SKOS.altLabel, Literal("A", lang="en")))
    )
    assert check_skos(graph).outcome == "disagreement"
    assert check_skos(graph, profile="core-integrity").outcome == "disagreement"
    assert check_skos(graph, profile="mapping-integrity", rules=()).verified
    invalid: tuple[dict[str, Any], ...] = (
        {"profile": "unknown"},
        {"hierarchy_basis": "unknown"},
        {"rules": ()},
        {"profile": "mapping-integrity", "rules": ""},
    )
    for kwargs in invalid:
        with pytest.raises(InputError):
            check_skos(graph, **kwargs)
    limited = check_skos(graph, profile="mapping-integrity", limits=Limits(max_work=1))
    assert limited.outcome == "exhausted" and not limited.complete
