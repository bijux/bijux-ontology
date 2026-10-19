"""Primary outputs, supporting references and complete actual target inventories."""

import threading
from dataclasses import replace
from typing import Literal as Choice

import pytest
from rdflib import BNode, Dataset, Literal, Namespace, URIRef
from rdflib.namespace import RDF, XSD

from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.shacl.partitions import (
    PartitionObligation,
    PartitionPlan,
    check_partitions,
)

EX = Namespace("urn:partition:")
REFERENCE = """@prefix e:<urn:partition:>.@prefix sh:<http://www.w3.org/ns/shacl#>.
e:Root a sh:NodeShape;sh:targetClass e:Record;
 sh:property [sh:path e:ref;sh:minCount 1;sh:class e:Reference;
 sh:node [sh:property [sh:path e:name;sh:minCount 1]]]."""
NAME = """@prefix e:<urn:partition:>.@prefix sh:<http://www.w3.org/ns/shacl#>.
e:Root a sh:NodeShape;sh:targetClass e:Record;
 sh:property [sh:path e:name;sh:minCount 1]."""
UNIQUE = """@prefix e:<urn:partition:>.@prefix sh:<http://www.w3.org/ns/shacl#>.
e:Root a sh:NodeShape;sh:targetClass e:Record;
 sh:property [sh:path (e:code [sh:inversePath e:code]);sh:maxCount 1]."""


def _dataset(
    *,
    reference: bool = True,
    name: bool = False,
    second: bool = False,
    duplicate: bool = False,
) -> Dataset:
    """Independently author actual physical submissions, support and hidden content."""
    data = Dataset()
    primary, support = data.graph(EX.Primary), data.graph(EX.Support)
    primary.add((EX.r, RDF.type, EX.Record))
    primary.add((EX.r, EX.ref, EX.reference))
    primary.add((EX.r, EX.code, EX.code1))
    primary.add(
        (EX.r, EX.lexical, Literal("01", datatype=XSD.integer, normalize=False))
    )
    if name:
        primary.add((EX.r, EX.name, Literal("primary")))
    if second:
        primary.add((EX.r2, RDF.type, EX.Record))
        primary.add((EX.r2, EX.code, EX.code1 if duplicate else EX.code2))
    if reference:
        support.add((EX.reference, RDF.type, EX.Reference))
        support.add((EX.reference, EX.name, Literal("reference")))
    support.add((EX.r, EX.name, Literal("support-only")))
    support.add((EX.unrelated, RDF.type, EX.Record))
    data.graph(EX.Hidden).add((EX.secret, EX.name, Literal("not selected")))
    return data


def _obligation(
    name: str = "references",
    body: str = REFERENCE,
    *,
    graph: Choice["primary", "merged"] = "merged",
    full: bool = False,
) -> PartitionObligation:
    """Select actual authored named shapes and explicit assertion scope."""
    return PartitionObligation(
        name,
        prepare_rdf(body, format="turtle"),
        (EX.Root,),
        graph=graph,
        requires_complete_primary=full,
    )


def _plan(
    *obligations: PartitionObligation,
    focus: tuple[URIRef | BNode, ...] = (EX.r,),
    complete: bool = False,
) -> PartitionPlan:
    """Bind exact finite primary record intent without inferring support submissions."""
    return PartitionPlan(
        str(EX.Primary),
        (str(EX.Support),),
        focus,
        obligations,
        primary_complete=complete,
    )


def test_support_resolves_references_without_becoming_submission() -> None:
    """Support resolves references without targeting unrelated support records."""
    pytest.importorskip("pyshacl")
    source = prepare_rdf(_dataset())
    before = set(source.graph(str(EX.Primary)))
    result = check_partitions(source, _plan(_obligation()))
    assert result.report.verified and len(result.results) == 1
    actual = result.results[0]
    assert actual.results[0].conforms is True
    assert actual.results[0].coverage is not None
    assert actual.scope.graph == "merged" and actual.scope.primary == str(EX.Primary)
    assert actual.scope.support == (str(EX.Support),)
    assert (EX.r, EX.name, None) not in actual.primary.graph()
    assert (EX.r, EX.name, Literal("support-only")) in actual.effective.graph()
    assert (EX.secret, None, None) not in actual.effective.graph()
    assert set(source.graph(str(EX.Primary))) == before
    assert all(
        str(target.focus_node) != str(EX.unrelated)
        for target in actual.results[0].coverage.targets
    )
    missing = check_partitions(
        prepare_rdf(_dataset(reference=False)), _plan(_obligation())
    )
    assert not missing.report.verified
    assert missing.results[0].results[0].conforms is False


def test_primary_output_cannot_borrow_support_field() -> None:
    """A missing primary field passes only an explicitly merged obligation."""
    pytest.importorskip("pyshacl")
    source = prepare_rdf(_dataset())
    result = check_partitions(
        source,
        _plan(
            _obligation("primary-name", NAME, graph="primary"),
            _obligation("merged-name", NAME),
        ),
    )
    assert result.report.outcome == "disagreement"
    assert [item.results[0].conforms for item in result.results] == [False, True]
    assert any(issue.code == "shacl.nonconformance" for issue in result.report.issues)
    exact = Literal("01", datatype=XSD.integer, normalize=False)
    assert (EX.r, EX.lexical, exact) in result.results[0].primary.graph()
    exported = result.results[0].effective.graph()
    exported.add((EX.r, EX.name, Literal("caller mutation")))
    assert (EX.r, EX.name, None) not in result.results[0].effective.graph()


def test_complete_primary_is_actual_full_graph_and_exact_inventory() -> None:
    """Completeness cannot hide unselected records or global duplicates."""
    pytest.importorskip("pyshacl")
    rule = _obligation("unique", UNIQUE, graph="primary", full=True)
    source = prepare_rdf(_dataset(second=True))
    complete = check_partitions(source, _plan(rule, focus=(EX.r, EX.r2), complete=True))
    assert complete.report.verified
    assert complete.results[0].discovery is not None
    assert complete.results[0].discovery.coverage is not None
    assert {
        target.focus_node for target in complete.results[0].discovery.coverage.targets
    } == {EX.r, EX.r2}
    omitted = check_partitions(source, _plan(rule, complete=True))
    assert omitted.results[0].results[0].conforms is True
    assert not omitted.report.verified
    assert any(
        issue.code == "shacl.partition_scope_unverified"
        for issue in omitted.report.issues
    )
    automatic = replace(rule, requires_complete_primary=False)
    automatic_partial = check_partitions(source, _plan(automatic))
    assert not automatic_partial.report.verified
    assert automatic_partial.results[0].scope.requires_complete_primary
    automatic_missing = check_partitions(source, _plan(automatic, complete=True))
    assert not automatic_missing.report.verified
    automatic_complete = check_partitions(
        source, _plan(automatic, focus=(EX.r, EX.r2), complete=True)
    )
    assert automatic_complete.report.verified
    assert automatic_complete.results[0].scope.requires_complete_primary
    nested = _obligation(
        "nested",
        """@prefix e:<urn:partition:>.
      @prefix sh:<http://www.w3.org/ns/shacl#>.
      e:Root a sh:NodeShape;sh:targetClass e:Record;
        sh:property[sh:path e:ref;sh:node e:Child].
      e:Child a sh:NodeShape;sh:property[
        sh:path[sh:alternativePath(e:name [sh:inversePath e:owner])];sh:minCount 0].""",
        graph="primary",
    )
    nested_partial = check_partitions(source, _plan(nested))
    assert not nested_partial.report.verified
    assert nested_partial.results[0].scope.requires_complete_primary
    for path, nested_constraint in (
        ("(e:record e:code)", "sh:maxCount 1"),
        ("e:record", "sh:node [sh:property [sh:path e:code;sh:maxCount 1]]"),
        (
            "e:record",
            "sh:node [sh:property [sh:path e:code;sh:uniqueLang true]]",
        ),
        (
            "e:record",
            "sh:node [sh:property [sh:path e:code;sh:datatype "
            "<http://www.w3.org/1999/02/22-rdf-syntax-ns#langString>]]",
        ),
        (
            "e:record",
            "sh:node [sh:property [sh:path e:code;sh:qualifiedMaxCount 1;"
            "sh:qualifiedValueShape [sh:nodeKind sh:Literal]]]",
        ),
    ):
        aggregate = _obligation(
            "aggregate",
            """@prefix e:<urn:partition:>.
            @prefix sh:<http://www.w3.org/ns/shacl#>.
            e:Root a sh:NodeShape;sh:targetNode e:catalogue;
            sh:property [sh:path """
            + path
            + ";"
            + nested_constraint
            + "].",
            graph="primary",
        )
        data = Dataset()
        primary = data.graph(EX.Primary)
        primary.add((EX.catalogue, EX.record, EX.a))
        primary.add((EX.catalogue, EX.record, EX.b))
        primary.add((EX.a, EX.code, Literal("one", lang="en")))
        focus = (EX.catalogue,)
        partial_aggregate = check_partitions(
            prepare_rdf(data), replace(_plan(aggregate, focus=focus), support=())
        )
        assert not partial_aggregate.report.verified
        assert partial_aggregate.results[0].scope.requires_complete_primary
        assert partial_aggregate.results[0].results[0].conforms is True
        full_aggregate = check_partitions(
            prepare_rdf(data),
            replace(_plan(aggregate, focus=focus, complete=True), support=()),
        )
        assert full_aggregate.report.verified
        extra = (
            Literal("2", datatype=XSD.integer)
            if "datatype" in nested_constraint
            else Literal("two", lang="en")
        )
        primary.add((EX.a, EX.code, extra))
        violated = check_partitions(
            prepare_rdf(data),
            replace(_plan(aggregate, focus=focus, complete=True), support=()),
        )
        assert violated.report.outcome == "disagreement"
        assert violated.results[0].results[0].conforms is False
    for condition in (
        "sh:not [sh:property [sh:path e:code;sh:minCount 2]]",
        "sh:xone ([sh:property [sh:path e:code;sh:minCount 1]] "
        "[sh:property [sh:path e:other;sh:minCount 1]])",
    ):
        rule_negative = _obligation(
            "negative-reference",
            """@prefix e:<urn:partition:>.
            @prefix sh:<http://www.w3.org/ns/shacl#>.
            e:Root a sh:NodeShape;sh:targetNode e:catalogue;
            sh:property [sh:path e:record;sh:node ["""
            + condition
            + "]].",
            graph="primary",
        )
        data = Dataset()
        primary = data.graph(EX.Primary)
        primary.add((EX.catalogue, EX.record, EX.a))
        primary.add((EX.a, EX.code, Literal("one")))
        plan = PartitionPlan(str(EX.Primary), (), (EX.catalogue,), (rule_negative,))
        partial_negative = check_partitions(prepare_rdf(data), plan)
        assert not partial_negative.report.verified
        assert partial_negative.results[0].scope.requires_complete_primary
        assert partial_negative.results[0].results[0].conforms is True
        assert check_partitions(
            prepare_rdf(data), replace(plan, primary_complete=True)
        ).report.verified
        primary.add((EX.a, EX.code, Literal("two")))
        primary.add((EX.a, EX.other, Literal("other")))
        assert (
            check_partitions(
                prepare_rdf(data), replace(plan, primary_complete=True)
            ).report.outcome
            == "disagreement"
        )
    partial = check_partitions(source, _plan(rule, focus=(EX.r, EX.r2)))
    assert not partial.report.verified
    duplicate = check_partitions(
        prepare_rdf(_dataset(second=True, duplicate=True)),
        _plan(rule, focus=(EX.r, EX.r2), complete=True),
    )
    assert duplicate.report.outcome == "disagreement"
    assert duplicate.results[0].results[0].conforms is False
    assert len(duplicate.results[0].results[0].root_findings) == 2


def test_reference_qualification_requires_complete_primary() -> None:
    """Referenced typing and deeper values cannot qualify local absence truth."""
    pytest.importorskip("pyshacl")
    for condition, qualification in (
        (
            "sh:qualifiedMaxCount 1;sh:qualifiedValueShape [sh:class e:Allowed]",
            True,
        ),
        ("sh:node [sh:property [sh:path e:code;sh:class e:Allowed]]", False),
        (
            "sh:node [sh:property [sh:path e:code;"
            "sh:node [sh:property [sh:path e:label;sh:minCount 1]]]]",
            False,
        ),
    ):
        shape = _obligation(
            "reference-scope",
            """@prefix e:<urn:partition:>.
            @prefix sh:<http://www.w3.org/ns/shacl#>.
            e:Root a sh:NodeShape;sh:targetNode e:catalogue;
            sh:property [sh:path e:record;"""
            + condition
            + "].",
            graph="primary",
        )
        data = Dataset()
        graph = data.graph(EX.Primary)
        for node in (EX.a, EX.b):
            graph.add((EX.catalogue, EX.record, node))
        graph.add((EX.a, RDF.type, EX.Allowed))
        graph.add((EX.b, RDF.type, EX.Reference))
        graph.add((EX.a, EX.code, EX.code1))
        graph.add((EX.code1, RDF.type, EX.Allowed))
        graph.add((EX.code1, EX.label, Literal("label")))
        plan = PartitionPlan(str(EX.Primary), (), (EX.catalogue,), (shape,))
        result = check_partitions(prepare_rdf(data), plan)
        assert not result.report.verified
        assert result.results[0].scope.requires_complete_primary
        assert result.results[0].results[0].conforms is True
        complete = replace(plan, primary_complete=True)
        assert check_partitions(prepare_rdf(data), complete).report.verified
        if qualification:
            graph.add((EX.b, RDF.type, EX.Allowed))
        else:
            graph.add((EX.a, EX.code, EX.code2))
        violated = check_partitions(prepare_rdf(data), complete)
        assert violated.report.outcome == "disagreement"
        assert violated.results[0].results[0].conforms is False


def test_merged_global_targets_come_only_from_primary() -> None:
    """Primary discovery is evidence; requested merged checks must pass."""
    pytest.importorskip("pyshacl")
    result = check_partitions(
        prepare_rdf(_dataset()), _plan(_obligation(full=True), complete=True)
    )
    assert result.report.verified
    actual = result.results[0]
    assert actual.discovery is not None and actual.discovery.conforms is False
    assert (
        actual.discovery.coverage is not None
        and actual.discovery.coverage.report.verified
    )
    assert actual.results[0].conforms is True
    assert actual.results[0].coverage is not None
    assert all(
        target.focus_node == EX.r for target in actual.results[0].coverage.targets
    )


def test_owned_blank_links_graphs_and_source_pin_boundaries() -> None:
    """Owned blank links survive partitions; foreign equal labels cannot bind."""
    pytest.importorskip("pyshacl")
    record, reference, graph_name = BNode(), BNode(), BNode()
    data = Dataset()
    data.graph(graph_name).add((record, RDF.type, EX.Record))
    data.graph(graph_name).add((record, EX.ref, reference))
    data.graph(EX.Support).add((reference, RDF.type, EX.Reference))
    data.graph(EX.Support).add((reference, EX.name, Literal("actual")))
    source = prepare_rdf(data)
    owned_graph = next(
        name for name in source.dataset().graphs() if isinstance(name.identifier, BNode)
    ).identifier
    owned_record = next(source.graph(owned_graph).subjects(RDF.type, EX.Record))
    assert isinstance(owned_record, BNode)
    plan = PartitionPlan(
        owned_graph, (str(EX.Support),), (owned_record,), (_obligation(),)
    )
    result = check_partitions(source, plan)
    assert result.report.verified
    global_result = check_partitions(
        source,
        replace(plan, primary_complete=True, obligations=(_obligation(full=True),)),
    )
    assert not global_result.report.verified
    foreign = replace(plan, focus_nodes=(BNode(str(owned_record)),))
    assert check_partitions(source, foreign).report.outcome == "input_error"
    foreign_graph = replace(plan, primary=BNode(str(owned_graph)))
    assert check_partitions(source, foreign_graph).report.outcome == "input_error"
    ordinary = prepare_rdf(_dataset())
    for primary_selector in (EX.Primary, str(EX.Primary)):
        duplicate = PartitionPlan(
            primary_selector, (str(EX.Primary),), (EX.r,), (_obligation(),)
        )
        assert check_partitions(ordinary, duplicate).report.outcome == "input_error"
    pinned = prepare_rdf(RdfSource(_dataset(), graph_name=str(EX.Primary)))
    assert (
        check_partitions(pinned, _plan(_obligation())).report.outcome == "input_error"
    )
    pinned_primary_only = replace(
        _plan(_obligation("primary", NAME, graph="primary")), support=()
    )
    assert (
        check_partitions(pinned, pinned_primary_only).results[0].results[0].conforms
        is False
    )


def test_bounds_cancel_and_late_prefix_keep_required_truth() -> None:
    """Late exhaustion retains actual failures and required pending scopes."""
    pytest.importorskip("pyshacl")
    source = prepare_rdf(_dataset())
    first = _obligation("missing", NAME, graph="primary")
    later = _obligation("x" * 100000)
    bounded = check_partitions(
        source, _plan(first, later), limits=Limits(max_bytes=10000)
    )
    assert bounded.report.outcome == "exhausted" and bounded.report.requested == 2
    assert any(issue.code == "shacl.nonconformance" for issue in bounded.report.issues)
    assert len(bounded.results) == 1
    early = check_partitions(source, _plan(first, later), limits=Limits(max_states=1))
    assert early.report.outcome == "exhausted" and early.report.requested == 2
    for limits in (
        Limits(max_work=10),
        Limits(max_states=10),
        Limits(max_total_bytes=100),
    ):
        assert (
            check_partitions(source, _plan(first), limits=limits).report.outcome
            == "exhausted"
        )
    stopped = threading.Event()
    stopped.set()
    assert (
        check_partitions(source, _plan(first), cancel=stopped).report.outcome
        == "exhausted"
    )
    timed = check_partitions(source, _plan(first), timeout=0.02)
    assert timed.report.outcome == "exhausted"
    assert check_partitions(source, _plan(_obligation())).report.verified
    shared = Budget(Limits(max_issues=3))
    quota = check_partitions(
        source, _plan(first, _obligation("other", NAME, graph="primary")), budget=shared
    )
    assert quota.report.outcome == "exhausted"
    assert any(issue.code == "shacl.nonconformance" for issue in quota.report.issues)
    measured = Budget()
    check_partitions(source, _plan(first), budget=measured)
    states = measured.limits.max_states - measured.remaining("states")
    observed = False
    for cap in range(max(1, states - 30), states + 1):
        operation = Budget(Limits(max_states=cap))
        result = check_partitions(source, _plan(first), budget=operation)
        paid = any(
            issue.code == "shacl.nonconformance"
            for issue in operation._retained_issues.values()
        )
        if paid:
            observed = True
            assert any(
                issue.code == "shacl.nonconformance" for issue in result.report.issues
            )
    assert observed
