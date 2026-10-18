"""Actual SSSOM releases require explicit conflict choices and final revalidation."""

import importlib.util
import tracemalloc
from dataclasses import replace

import pytest
from rdflib import Graph, Namespace
from rdflib.namespace import RDF, SKOS

from bijux_ontology.limits import Budget, Limits
from bijux_ontology.vocabulary.inspection import VocabularyView
from bijux_ontology.vocabulary.mappings import (
    MappingPolicy,
    MappingSet,
    export_sssom,
    read_sssom,
)
from bijux_ontology.workflows.upgrades.mappings.reconciliation import (
    MappingReconciliation,
    MappingResolution,
    apply_mapping_resolution,
    reconcile_mappings,
)

EX = Namespace("urn:reconciliation:")
LICENSE = "https://creativecommons.org/publicdomain/zero/1.0/"


def _source(
    identifier: str,
    rows: tuple[tuple[str, str, str, str], ...],
    context: str = "",
    qualifier: bool = False,
    qualifier_value: str = "",
) -> MappingSet:
    if importlib.util.find_spec("sssom") is None:
        pytest.skip("Actual maintained SSSOM parser requires the sssom extra")
    header = (
        f"# mapping_set_id: urn:{identifier}\n# license: {LICENSE}\n"
        f'# creator_id: ["ex:{identifier}"]\n{context}# curie_map:\n'
        '#   ex: "urn:reconciliation:"\n'
    )
    columns = (
        "subject_id\tpredicate_id\tobject_id\t"
        "mapping_justification\tconfidence\tcomment"
    )
    if qualifier:
        columns += "\tsubject_source"
    body = ""
    for target, predicate, confidence, comment in rows:
        body += (
            f"ex:A\tskos:{predicate}\tex:{target}\t"
            f"semapv:ManualMappingCuration\t{confidence}\t{comment}"
        )
        if qualifier:
            body += "\tex:" + (qualifier_value or comment)
        body += "\n"
    result = read_sssom(
        (header + columns + "\n" + body).encode(),
        mapping_set_id="urn:" + identifier,
        license=LICENSE,
    )
    assert result.report.verified and result.mapping_set is not None, (
        result.report.to_dict()
    )
    return result.mapping_set


def _view() -> VocabularyView:
    graph = Graph()
    for term in (EX.A, EX.B, EX.C):
        graph.add((term, RDF.type, SKOS.Concept))
    return VocabularyView(graph)


@pytest.fixture(scope="module")
def releases() -> tuple[MappingSet, MappingSet]:
    duplicate = ("B", "closeMatch", "0.100", "same assertion")
    return _source("left-release", (duplicate, duplicate)), _source(
        "right-release", (duplicate, ("C", "closeMatch", "0.999", "alternative"))
    )


def _plan(
    releases: tuple[MappingSet, MappingSet], *, budget: Budget | None = None
) -> MappingReconciliation:
    return reconcile_mappings(
        releases,
        subject_vocabulary=_view(),
        object_vocabulary=_view(),
        policy=MappingPolicy(
            (str(SKOS.closeMatch), str(SKOS.broadMatch)),
            maximum_targets=1,
            conflicts=((str(SKOS.closeMatch), str(SKOS.broadMatch)),),
        ),
        mapping_set_id="urn:merged:mappings",
        license=LICENSE,
        budget=budget,
    )


def test_review_groups_all_duplicate_witnesses_and_explicit_choice_rechecks(
    releases: tuple[MappingSet, MappingSet],
) -> None:
    plan = _plan(releases)
    assert plan.inventory_complete and plan.report.outcome == "disagreement", (
        plan.report.to_dict()
    )
    assert len(plan.groups) == 2 and [len(g.occurrences) for g in plan.groups] == [3, 1]
    assert plan.groups[0].occurrences[0].mapping_set is releases[0]
    assert plan.groups[0].occurrences[-1].mapping_set is releases[1]
    assert plan.groups[1].occurrences[0].row.object == str(EX.C)
    assert dict(plan.groups[1].occurrences[0].row.cells)["confidence"] == "0.999"
    resolution = MappingResolution(
        plan, (0,), "domain-owner", "retain the reviewed target"
    )
    result = apply_mapping_resolution(
        plan, resolution, mapping_set_id="urn:approved:mappings", license=LICENSE
    )
    assert result.report.verified and result.mapping_set is not None, (
        result.report.to_dict()
    )
    assert [row.object for row in result.mapping_set.rows] == [str(EX.B)]
    assert result.reconciliation is plan and result.resolution is resolution
    exported = export_sssom(result.mapping_set)
    assert exported.report.verified and exported.content is not None
    reread = read_sssom(
        exported.content, mapping_set_id="urn:approved:mappings", license=LICENSE
    )
    assert reread.report.verified and reread.mapping_set is not None
    assert reread.mapping_set.rows == result.mapping_set.rows
    rejected = apply_mapping_resolution(
        plan,
        replace(resolution, retain=(0, 1)),
        mapping_set_id="urn:rejected:mappings",
        license=LICENSE,
    )
    assert rejected.report.outcome == "disagreement" and rejected.mapping_set is None


def test_context_qualified_rows_and_direction_conflicts_are_not_collapsed() -> None:
    left = _source(
        "qualified-left", (("B", "closeMatch", "0.900", "context-one"),), qualifier=True
    )
    right = _source(
        "qualified-right",
        (("B", "broadMatch", "0.900", "same"),),
        qualifier=True,
        qualifier_value="context-two",
    )
    plan = _plan((left, right))
    assert len(plan.groups) == 2 and plan.report.outcome == "disagreement"
    assert {
        dict(g.occurrences[0].row.cells)["subject_source"] for g in plan.groups
    } == {"ex:context-one", "ex:context-two"}
    assert {g.occurrences[0].row.predicate for g in plan.groups} == {
        str(SKOS.closeMatch),
        str(SKOS.broadMatch),
    }
    kept = apply_mapping_resolution(
        plan,
        MappingResolution(plan, (1,), "reviewer", "retain explicit broader direction"),
        mapping_set_id="urn:broader:mappings",
        license=LICENSE,
    )
    assert kept.report.verified and kept.mapping_set is not None
    assert kept.mapping_set.rows[0].predicate == str(SKOS.broadMatch)
    # Distinct contextual qualifiers keep otherwise identical assertions separate.
    same_relation = _source(
        "qualified-other",
        (("B", "closeMatch", "0.900", "same"),),
        qualifier=True,
        qualifier_value="context-two",
    )
    alternatives = _plan((left, same_relation))
    assert alternatives.report.verified and len(alternatives.groups) == 2
    assert (
        alternatives.production is not None
        and alternatives.production.mapping_set is not None
    )
    assert len(alternatives.production.mapping_set.rows) == 2


def test_incompatible_defaults_and_unsupported_qualifiers_remain_unknown() -> None:
    row = (("B", "closeMatch", "0.900", "same"),)
    left = _source("plain", row)
    right = _source("modified", row, "# other: context=uninterpreted\n")
    plan = _plan((left, right))
    assert (
        plan.inventory_complete and len(plan.groups) == 2 and not plan.report.verified
    )
    unresolved = apply_mapping_resolution(
        plan,
        MappingResolution(plan, (0, 1), "reviewer", "cannot flatten context"),
        mapping_set_id="urn:context:mappings",
        license=LICENSE,
    )
    assert unresolved.mapping_set is None and unresolved.report.outcome == "unverified"
    unsupported = apply_mapping_resolution(
        plan,
        MappingResolution(
            plan, (1,), "reviewer", "approval is not semantic verification"
        ),
        mapping_set_id="urn:negated:mappings",
        license=LICENSE,
    )
    assert (
        unsupported.mapping_set is None and unsupported.report.outcome == "unverified"
    )


def test_actual_budget_and_invalid_choices_cannot_publish(
    releases: tuple[MappingSet, MappingSet],
) -> None:
    budget = Budget(Limits(max_work=20))
    exhausted = _plan(releases, budget=budget)
    assert exhausted.report.outcome == "exhausted" and not exhausted.inventory_complete
    assert budget._counts.get("work", 0) <= 20
    plan = _plan(releases)
    other = _plan((releases[1], releases[0]))
    stale = apply_mapping_resolution(
        other,
        MappingResolution(plan, (0,), "reviewer", "bound to original review"),
        mapping_set_id="urn:stale:mappings",
        license=LICENSE,
    )
    assert stale.mapping_set is None and stale.report.outcome == "input_error"
    invalid = apply_mapping_resolution(
        plan,
        MappingResolution(plan, (0, 0), "reviewer", "duplicate choice"),
        mapping_set_id="urn:invalid:mappings",
        license=LICENSE,
    )
    assert invalid.report.outcome == "input_error" and invalid.mapping_set is None
    limited = apply_mapping_resolution(
        plan,
        MappingResolution(plan, (0,), "reviewer", "explicit"),
        mapping_set_id="urn:limited:mappings",
        license=LICENSE,
        budget=Budget(Limits(max_work=20)),
    )
    assert limited.report.outcome == "exhausted" and limited.mapping_set is None
    authority = " " + "a" * 1_000_000 + " "
    tiny = Budget(Limits(max_bytes=1, max_total_bytes=64, max_work=1))
    tracing = tracemalloc.is_tracing()
    if not tracing:
        tracemalloc.start()
    current, _ = tracemalloc.get_traced_memory()
    tracemalloc.reset_peak()
    try:
        bounded = apply_mapping_resolution(
            plan,
            MappingResolution(plan, (0,), authority, "review"),
            mapping_set_id="urn:bounded:mappings",
            license=LICENSE,
            budget=tiny,
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        if not tracing:
            tracemalloc.stop()
    assert bounded.mapping_set is None and bounded.report.outcome == "exhausted"
    assert not tiny._counts and peak - current < len(authority) // 4
