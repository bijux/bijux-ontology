"""Actual SSSOM releases and explicit consumer records, without ontology removal."""

import importlib.util
from dataclasses import replace
from typing import Literal, cast

import pytest
from rdflib import Graph, Namespace
from rdflib.namespace import RDF, SKOS

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.mappings import MappingPolicy, MappingRead, read_sssom
from bijux_ontology.vocabulary.mappings.composition.types import MappingOccurrence
from bijux_ontology.workflows.upgrades.mappings.impact import (
    MappingCoverage,
    MappingImpact,
    MappingQualifications,
    MappingUse,
    assess_mapping_impact,
)

E = Namespace("urn:example:")
ID = "urn:impact:release"
LICENSE = "https://creativecommons.org/publicdomain/zero/1.0/"
HEADER = (
    f"# mapping_set_id: {ID}\n# license: {LICENSE}\n"
    '# mapping_set_title: "Original metadata"\n'
    '# curie_map:\n#   ex: "urn:example:"\n'
    "subject_id\tpredicate_id\tobject_id\tmapping_justification\tconfidence\tcomment\n"
)
POLICY = MappingPolicy(
    (str(SKOS.broadMatch), str(SKOS.narrowMatch)),
    subjects=(str(E.A),),
    minimum_targets=1,
    maximum_targets=1,
    maximum_sources=1,
)


def _row(
    subject: str, target: str, predicate: str = "broadMatch", comment: str = "kept"
) -> str:
    """Supply actual standard CURIEs and the real maintained justification enum."""
    return (
        f"ex:{subject}\tskos:{predicate}\tex:{target}\t"
        f"semapv:ManualMappingCuration\t0.900\t{comment}\n"
    )


@pytest.fixture(scope="module")
def releases() -> dict[str, MappingRead]:
    """Every positive table comes from the maintained installed SSSOM factory."""
    if importlib.util.find_spec("sssom") is None:
        pytest.skip("Native positive mapping impact requires bijux-ontology[sssom]")
    bodies = {
        "before": _row("A", "X") + _row("B", "Y"),
        "editorial": _row("A", "X", comment="edited") + _row("B", "Y") + _row("B", "Y"),
        "removed": _row("B", "Y"),
        "direction": _row("A", "X", "narrowMatch") + _row("B", "Y"),
        "endpoint": _row("A", "Y") + _row("B", "Y"),
        "ambiguous": _row("A", "X") + _row("B", "X"),
        "unused": _row("A", "X") + _row("C", "Z"),
        "metadata": _row("A", "X") + _row("B", "Y"),
        "conflict": _row("A", "X") + _row("A", "X", "narrowMatch") + _row("B", "Y"),
        "unrelated_conflict": _row("A", "X")
        + _row("B", "Y")
        + _row("B", "Y", "narrowMatch"),
        "unselected_conflict": _row("A", "X")
        + _row("A", "X", "closeMatch")
        + _row("A", "X", "relatedMatch"),
        "open_baseline": (
            _row("A", "X").rstrip("\n")
            + "\t\n"
            + _row("B", "X").rstrip("\n")
            + "\tcustom:qualifier=uninterpreted\n"
        ),
        "prefix_before": _row("A", "X").rstrip("\n") + "\tctx:scope\n",
        "prefix_after": _row("A", "X").rstrip("\n") + "\tctx:scope\n",
        "unrelated_ambiguity": (
            _row("A", "X") + _row("B", "Y") + _row("C", "A") + _row("D", "A")
        ),
    }
    result = {}
    for name, body in bodies.items():
        header = (
            HEADER.replace("Original metadata", "Revised metadata")
            if name == "metadata"
            else HEADER
        )
        if name == "open_baseline":
            header = header.replace("\tcomment\n", "\tcomment\tother\n")
        if name.startswith("prefix_"):
            target = (
                "urn:context:before:"
                if name == "prefix_before"
                else "urn:context:after:"
            )
            header = header.replace("#   ex:", '#   ctx: "' + target + '"\n#   ex:')
            header = header.replace("\tcomment\n", "\tcomment\tsubject_source\n")
        value = read_sssom((header + body).encode(), mapping_set_id=ID, license=LICENSE)
        assert value.mapping_set is not None and value.report.verified, (
            value.report.to_dict()
        )
        result[name] = value
    return result


def _view() -> VocabularyView:
    """Every endpoint still exists; all breaks concern mappings, not ontology loss."""
    graph = Graph()
    for name in ("A", "B", "C", "D", "X", "Y", "Z"):
        graph.add((E[name], RDF.type, SKOS.Concept))
    return VocabularyView(graph)


def _use(
    source: MappingRead, direction: Literal["forward", "reverse"] = "forward"
) -> MappingUse:
    """Bind the actual first original occurrence, without guessing row IDs."""
    assert source.mapping_set is not None
    return MappingUse(
        "record:one", "/mapping", MappingOccurrence(source.mapping_set, 0), direction
    )


def _check(
    before: MappingRead,
    after: MappingRead,
    *,
    uses: tuple[MappingUse, ...] | None = None,
    coverage: MappingCoverage | None = None,
    qualifications: MappingQualifications | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    policy: MappingPolicy = POLICY,
) -> MappingImpact:
    """Keep supplied record coverage and qualification choices explicit."""
    return assess_mapping_impact(
        before,
        after,
        uses=(_use(before),) if uses is None else uses,
        coverage=MappingCoverage(("record:one",), True)
        if coverage is None
        else coverage,
        policy=policy,
        qualifications=MappingQualifications((), ())
        if qualifications is None
        else qualifications,
        subject_vocabulary=_view(),
        object_vocabulary=_view(),
        limits=limits,
        budget=budget,
    )


def test_unused_editorial_and_duplicate_changes_preserve_directional_obligations(
    releases: dict[str, MappingRead],
) -> None:
    before = releases["before"]
    for name in ("editorial", "unused", "metadata"):
        result = _check(before, releases[name])
        assert result.verdict in ("agreement", "informational")
        assert result.report.verified
        assert result.obligations[0].status == "preserved"
        assert (
            result.obligations[0].surviving[0].mapping_set is releases[name].mapping_set
        )
    reverse = _check(before, before, uses=(_use(before, "reverse"),))
    assert reverse.verdict == "agreement"
    assert reverse.obligations[0].use.direction == "reverse"
    unrelated = _check(before, releases["unrelated_ambiguity"])
    assert unrelated.verdict == "informational"
    assert unrelated.obligations[0].status == "preserved"
    assert unrelated.after_check is not None and not unrelated.after_check.verified


def test_removed_endpoint_and_broad_narrow_changes_identify_actual_record_and_witness(
    releases: dict[str, MappingRead],
) -> None:
    for name in ("removed", "endpoint", "direction"):
        result = _check(releases["before"], releases[name])
        assert result.verdict == "demonstrated-break"
        obligation = result.obligations[0]
        assert obligation.status == "broken" and obligation.use.record == "record:one"
        assert obligation.use.occurrence.mapping_set is releases["before"].mapping_set
        assert obligation.use.occurrence.row.predicate == str(SKOS.broadMatch)
        assert any(
            change.status == "removed" and change.occurrence.index == 0
            for change in result.changes
        )
        assert any(
            issue.actual == "record:one" and issue.location == "/mapping"
            for issue in obligation.failures
        )


def test_new_many_to_one_uses_actual_native_cardinality_and_preserves_prior_failure(
    releases: dict[str, MappingRead],
) -> None:
    result = _check(releases["before"], releases["ambiguous"])
    assert result.verdict == "demonstrated-break"
    assert result.obligations[0].surviving
    assert result.after_check is not None
    native = next(
        issue
        for issue in result.after_check
        if issue.code == "mapping.maximum_exceeded"
    )
    assert native.evidence == {
        "requirement": "maximum_sources",
        "endpoint": str(E.X),
        "limit": 1,
        "observed": 2,
    }
    assert result.obligations[0].failures[0].evidence == native.evidence
    persistent = _check(releases["ambiguous"], releases["ambiguous"])
    assert persistent.verdict == "informational"
    assert persistent.obligations[0].status == "preserved"
    assert persistent.after_check is not None and not persistent.after_check.verified
    uncertain = _check(releases["open_baseline"], releases["ambiguous"])
    assert uncertain.before_check is not None and not uncertain.before_check.complete
    assert uncertain.verdict == "unverified"
    assert uncertain.obligations[0].status == "unverified"
    assert not any(
        issue.code == "upgrade.mapping_cardinality_changed"
        for issue in uncertain.report
    )
    conflict_policy = replace(
        POLICY, conflicts=((str(SKOS.broadMatch), str(SKOS.narrowMatch)),)
    )
    conflict = _check(releases["before"], releases["conflict"], policy=conflict_policy)
    assert conflict.verdict == "demonstrated-break"
    assert conflict.obligations[0].surviving
    assert (
        conflict.obligations[0].failures[0].code == "upgrade.mapping_relation_changed"
    )
    assert conflict.obligations[0].failures[0].evidence is not None
    persistent = _check(
        releases["conflict"], releases["conflict"], policy=conflict_policy
    )
    assert (
        persistent.verdict == "informational"
        and persistent.obligations[0].status == "preserved"
    )
    unrelated = _check(
        releases["before"], releases["unrelated_conflict"], policy=conflict_policy
    )
    assert (
        unrelated.verdict == "informational"
        and unrelated.obligations[0].status == "preserved"
    )
    unrelated_predicate = _check(
        releases["before"],
        releases["unselected_conflict"],
        policy=replace(
            POLICY,
            relations=(*POLICY.relations, str(SKOS.closeMatch), str(SKOS.relatedMatch)),
            conflicts=((str(SKOS.closeMatch), str(SKOS.relatedMatch)),),
        ),
    )
    assert unrelated_predicate.verdict == "informational"
    assert unrelated_predicate.obligations[0].status == "preserved"


def test_selected_qualifications_are_exact_and_unselected_metadata_is_retained(
    releases: dict[str, MappingRead],
) -> None:
    for name, selected in (
        ("editorial", MappingQualifications(("comment",), ())),
        ("metadata", MappingQualifications((), ("mapping_set_title",))),
    ):
        result = _check(releases["before"], releases[name], qualifications=selected)
        assert result.verdict == "demonstrated-break"
        assert result.before is releases["before"].mapping_set
        assert result.after is releases[name].mapping_set
    assert _check(releases["before"], releases["editorial"]).report.verified
    identifier = _check(
        releases["prefix_before"],
        releases["prefix_after"],
        qualifications=MappingQualifications(("subject_source",), ()),
    )
    assert identifier.verdict == "unverified" and not identifier.report.complete
    assert (
        identifier.obligations[0].status == "unverified"
        and not identifier.obligations[0].failures
    )
    selected_prefixes = _check(
        releases["prefix_before"],
        releases["prefix_after"],
        qualifications=MappingQualifications((), ("curie_map",)),
    )
    assert selected_prefixes.verdict == "demonstrated-break"
    assert selected_prefixes.obligations[0].status == "broken"
    assert selected_prefixes.before is not None and selected_prefixes.after is not None
    assert dict(selected_prefixes.before.prefixes)["ctx"] == "urn:context:before:"
    assert dict(selected_prefixes.after.prefixes)["ctx"] == "urn:context:after:"
    for cell in ("comment", "mapping_justification"):
        literal = _check(
            releases["prefix_before"],
            releases["prefix_after"],
            qualifications=MappingQualifications((cell,), ()),
        )
        assert literal.verdict == "agreement" and literal.report.verified


def test_incomplete_empty_and_foreign_consumer_witnesses_cannot_certify_compatibility(
    releases: dict[str, MappingRead],
) -> None:
    source = releases["before"]
    for result in (
        _check(source, source, coverage=MappingCoverage(("record:one",), False)),
        _check(
            source,
            source,
            coverage=MappingCoverage(("record:one", "record:missing"), True),
        ),
        _check(source, source, uses=()),
    ):
        assert result.verdict == "unverified" and not result.report.verified
    foreign = _check(source, source, uses=(_use(releases["editorial"]),))
    assert foreign.report.outcome == "input_error"
    assert source.mapping_set is not None
    negative = replace(
        _use(source), occurrence=MappingOccurrence(source.mapping_set, -1)
    )
    assert _check(source, source, uses=(negative,)).report.outcome == "input_error"
    with pytest.raises(InputError):
        _check(source, source, budget=cast(Budget, object()))


def test_original_input_and_native_reports_are_requalified_under_the_operation_budget(
    releases: dict[str, MappingRead],
) -> None:
    result = _check(
        releases["before"], releases["ambiguous"], limits=Limits(max_total_bytes=100)
    )
    assert result.report.outcome == "exhausted" and result.verdict == "unverified"
    assert result.before is None and not result.obligations
    assert result.before_check is None
    shared = Budget(Limits(max_work=1))
    result = _check(releases["before"], releases["ambiguous"], budget=shared)
    assert result.report.outcome == "exhausted"
    assert shared._counts.get("work", 0) <= 1
