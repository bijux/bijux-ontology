"""Actual catalog profile choices, reference scopes and maintained Core execution."""

import importlib.util
from dataclasses import replace
from typing import cast

import pytest
from rdflib import Dataset, Graph, Literal, Namespace
from rdflib.namespace import DCTERMS, FOAF, RDF, XSD

from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.interfaces.adapters.dcat import (
    CatalogBinding,
    CatalogRole,
    Dcat3Profile,
    check_catalog,
    prepare_catalog_profile,
)
from bijux_ontology.limits import Limits
from bijux_ontology.rdf.loading import prepare_rdf

E = Namespace("https://example.org/catalog/")
D = Namespace("http://www.w3.org/ns/dcat#")
NATIVE = pytest.mark.skipif(
    importlib.util.find_spec("pyshacl") is None, reason="actual optional SHACL backend"
)


def _catalog() -> tuple[Graph, tuple[CatalogBinding, ...], Dcat3Profile]:
    """Two representations preserve series/service roles and shared distributions."""
    selections = (
        (E.catalog, "catalog", D.Catalog),
        (E.dataset, "dataset", D.Dataset),
        (E.old, "dataset", D.Dataset),
        (E.series, "series", D.DatasetSeries),
        (E.csv, "distribution", D.Distribution),
        (E.api, "distribution", D.Distribution),
        (E.service, "service", D.DataService),
        (E.record, "record", D.CatalogRecord),
        (E.period, "period", DCTERMS.PeriodOfTime),
    )
    graph = Graph()
    for node, _, kind in selections:
        graph.add((node, RDF.type, kind))
    triples = (
        (E.catalog, D.dataset, E.dataset),
        (E.catalog, D.dataset, E.series),
        (E.catalog, D.record, E.record),
        (E.dataset, D.inSeries, E.series),
        (E.old, D.inSeries, E.series),
        (E.dataset, D.distribution, E.csv),
        (E.dataset, D.distribution, E.api),
        (E.old, D.distribution, E.csv),
        (E.csv, D.accessURL, E.csvURL),
        (E.api, D.accessService, E.service),
        (E.service, D.endpointURL, E.endpoint),
        (E.service, D.servesDataset, E.dataset),
        (E.record, FOAF.primaryTopic, E.dataset),
        (E.dataset, D.theme, E.theme),
        (E.dataset, DCTERMS.license, E.license),
        (E.dataset, DCTERMS.temporal, E.period),
        (E.dataset, D.previousVersion, E.old),
        (E.period, D.startDate, Literal("2024-01-01", datatype=XSD.date)),
    )
    for triple in triples:
        graph.add(triple)
    bindings = tuple(
        CatalogBinding(node, cast(CatalogRole, role)) for node, role, _ in selections
    )
    policy = Dcat3Profile(
        themes=ValueEnumeration((E.theme,)), licenses=ValueEnumeration((E.license,))
    )
    return graph, bindings, policy


@NATIVE
def test_selected_catalog_series_representations_service_and_reusable_profile() -> None:
    graph, bindings, policy = _catalog()
    profile = prepare_catalog_profile(policy)
    result = check_catalog(graph, bindings, profile=profile)
    assert result.report.verified, result.report.to_dict()
    assert result.source is not None and result.profile is profile
    assert not tuple(graph.triples((None, D.downloadURL, None)))
    assert result.report.checked == len(bindings)
    assert "unselected-catalog-records" in result.report.exclusions


@NATIVE
def test_missing_selected_type_member_and_forbidden_theme_are_located() -> None:
    graph, bindings, policy = _catalog()
    graph.remove((E.dataset, D.distribution, None))
    missing = check_catalog(graph, bindings, profile=policy)
    assert missing.report.outcome == "disagreement"
    assert any(
        issue.location == str(E.dataset) or str(E.dataset) in str(issue.details)
        for issue in missing.report.issues
    )
    graph, bindings, policy = _catalog()
    graph.remove((E.csv, RDF.type, D.Distribution))
    assert not check_catalog(graph, bindings, profile=policy).report.verified
    graph, bindings, policy = _catalog()
    graph.set((E.dataset, D.theme, E.forbidden))
    forbidden = check_catalog(graph, bindings, profile=policy)
    assert forbidden.report.outcome == "disagreement"
    assert any(
        str(E.dataset) in issue.location or str(E.dataset) in str(issue.details)
        for issue in forbidden.report.issues
    )


@NATIVE
def test_permitted_external_service_and_explicit_population_coverage() -> None:
    graph, bindings, policy = _catalog()
    graph.remove((E.service, None, None))
    bindings = tuple(binding for binding in bindings if binding.role != "service")
    allowed = replace(policy, external_services=(E.service,))
    result = check_catalog(graph, bindings, profile=allowed)
    assert result.report.verified, result.report.to_dict()
    unresolved = check_catalog(graph, bindings, profile=policy)
    assert not unresolved.report.verified
    assert any(
        issue.code == "catalog.reference_unverified" and issue.location == str(E.api)
        for issue in unresolved.report.issues
    )


@NATIVE
def test_historical_graphs_are_explicit_and_required_extensions_never_disappear() -> (
    None
):
    graph, bindings, policy = _catalog()
    dataset = Dataset()
    for triple in graph:
        dataset.graph(E.current).add(triple)
    dataset.graph(E.history).add((E.dataset, D.theme, E.forbidden))
    source = prepare_rdf(dataset)
    assert check_catalog(
        source, bindings, profile=policy, graph_name=E.current
    ).report.verified
    assert not check_catalog(source, bindings, profile=policy).report.verified
    unsupported = check_catalog(
        source,
        bindings,
        profile=replace(policy, required_extensions=(D.next,)),
        graph_name=E.current,
    )
    assert not unsupported.report.verified and not unsupported.report.complete
    assert any(
        issue.code == "catalog.extension_unverified"
        for issue in unsupported.report.issues
    )


def test_bounds_and_actual_backend_absence_do_not_establish_profile_conformance() -> (
    None
):
    graph, bindings, policy = _catalog()
    result = check_catalog(graph, bindings, profile=policy, limits=Limits(max_work=1))
    assert result.report.outcome == "exhausted" and not result.report.complete
    if importlib.util.find_spec("pyshacl") is None:
        absent = check_catalog(graph, bindings, profile=policy)
        assert not absent.report.verified and any(
            issue.kind == "unverified" for issue in absent.report.issues
        )
