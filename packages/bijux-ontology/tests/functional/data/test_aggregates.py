"""Exact group conservation, occurrence identity and dangerous omission controls."""

import sys
from dataclasses import replace
from fractions import Fraction
from itertools import permutations
from threading import Event
from types import FrameType
from typing import Literal as Choice

import pytest
from rdflib import BNode, Dataset, Graph, Literal, URIRef
from rdflib.namespace import XSD

from bijux_ontology.data.aggregates import AggregateGroup, check_aggregates
from bijux_ontology.data.keys import KeyRecord
from bijux_ontology.declarations.aggregates import AggregateContract
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.types import RdfSource

VALUE, UNIT, KG, NAME = (URIRef("urn:" + n) for n in ("value", "unit", "kg", "name"))


def records(*values: str) -> tuple[KeyRecord, ...]:
    """Own actual source terms, preserving repeated lexical and datatype identity."""
    text = "\n".join(
        f'<urn:r{i}> <urn:value> "{value}"^^<{XSD.decimal}>; <urn:unit> <urn:kg>.'
        for i, value in enumerate(values)
    )
    source = prepare_rdf(RdfSource(text, format="turtle"))
    return tuple(
        KeyRecord(str(i), source, URIRef(f"urn:r{i}")) for i in range(len(values))
    )


def group(
    rows: tuple[KeyRecord, ...], expected: str, *, complete: bool = True
) -> AggregateGroup:
    """Bind one caller-declared total to the complete selected member population."""
    return AggregateGroup(
        "urn:dataset",
        rows,
        Literal(expected, datatype=XSD.decimal, normalize=False),
        complete,
    )


def numeric(function: Choice["sum", "min", "max"] = "sum") -> AggregateContract:
    """Select a dimensioned exact quantity with independently checked unit paths."""
    return AggregateContract(
        "mass", function, "entities", VALUE, unit=KG, unit_path=UNIT
    )


def test_actual_count_and_exact_quantity_conservation_with_precise_mismatches() -> None:
    """Deletion and changed components cannot pass an unchanged claimed total."""
    rows = records("0.1", "0.2", "123456789012345678901234567890.0001")
    expected = "123456789012345678901234567890.3001"
    valid = check_aggregates((group(rows, expected),), numeric())
    assert valid.report.verified and valid.groups[0].actual == Fraction(expected)
    assert len(valid.groups[0].contributions) == 3
    for changed in (
        rows[:-1],
        records("0.1", "0.3", "123456789012345678901234567890.0001"),
    ):
        bad = check_aggregates((group(changed, expected),), numeric())
        assert bad.report.outcome == "disagreement"
        issue = next(i for i in bad.report if i.code == "data.aggregate_mismatch")
        assert issue.location == "urn:dataset" and issue.expected and issue.actual
    count = AggregateContract("members", "count", "entities")
    claimed = AggregateGroup("dataset", rows, Literal(3), True)
    assert check_aggregates((claimed,), count).report.verified
    assert (
        check_aggregates((replace(claimed, members=rows[:-1]),), count).report.outcome
        == "disagreement"
    )


def test_entity_occurrence_and_rdf_term_distinctness_are_separate_populations() -> None:
    """A repeated description is one entity, two occurrences and one exact value."""
    row = records("01.00")[0]
    again = replace(row, identifier="other-description")
    source2 = prepare_rdf(
        RdfSource(
            '<urn:r0> <urn:value> "1.00"^^<'
            + str(XSD.decimal)
            + ">; <urn:unit> <urn:kg>.",
            format="turtle",
        )
    )
    elsewhere = KeyRecord("other-document", source2, row.subject)
    inputs = (row, again, elsewhere)
    entity = check_aggregates((group(inputs, "1"),), numeric())
    occurrence = check_aggregates(
        (group(inputs, "3"),), replace(numeric(), counting="occurrences")
    )
    assert entity.report.verified and occurrence.report.verified
    assert len(entity.groups[0].contributions) == 3
    counts: tuple[tuple[Choice["entities", "occurrences"], int], ...] = (
        ("entities", 1),
        ("occurrences", 3),
    )
    for mode, expected in counts:
        count = replace(
            AggregateContract("descriptions", "count", "entities"),
            counting=mode,
        )
        assert check_aggregates(
            (AggregateGroup("g", inputs, Literal(expected), True),),
            count,
        ).report.verified
    exact = AggregateContract("distinct", "distinct_count", "occurrences", VALUE)
    assert check_aggregates(
        (AggregateGroup("g", inputs, Literal(2), True),), exact
    ).report.verified
    graph = Graph()
    triple = (URIRef("urn:r"), NAME, Literal("same"))
    graph.add(triple).add(triple)
    prepared = prepare_rdf(graph)
    count = AggregateContract("triples-are-sets", "distinct_count", "entities", NAME)
    assert check_aggregates(
        (
            AggregateGroup(
                "g", (KeyRecord("r", prepared, triple[0]),), Literal(1), True
            ),
        ),
        count,
    ).report.verified
    conflict = replace(elsewhere, source=records("2")[0].source)
    assert (
        check_aggregates((group((row, conflict), "1"),), numeric()).report.outcome
        == "unverified"
    )


def test_missing_partitions_values_and_ambiguous_units_stay_unknown() -> None:
    """Successful partial arithmetic cannot certify a complete selected total."""
    rows = records("1", "2")
    partial = check_aggregates((group(rows, "3", complete=False),), numeric())
    assert partial.report.outcome == "unverified" and partial.groups[0].actual is None
    for replacement in (
        "<urn:r0> <urn:unit> <urn:kg>.",
        "<urn:r0> <urn:value> 1; <urn:unit> <urn:kg>, <urn:g>.",
        "<urn:r0> <urn:value> 1; <urn:unit> <urn:g>.",
        '<urn:r0> <urn:value> "1"^^<' + str(XSD.double) + ">; <urn:unit> <urn:kg>.",
        "<urn:r0> <urn:value> 1, 2; <urn:unit> <urn:kg>.",
    ):
        prepared = prepare_rdf(RdfSource(replacement, format="turtle"))
        answer = check_aggregates(
            (group((KeyRecord("r", prepared, URIRef("urn:r0")),), "1"),), numeric()
        )
        assert answer.report.outcome == "unverified" and answer.groups[0].actual is None
    missing = KeyRecord(
        "missing",
        prepare_rdf(Graph().add((URIRef("urn:r0"), UNIT, KG))),
        URIRef("urn:r0"),
    )
    rejected = check_aggregates(
        (group((missing,), "0"),), replace(numeric(), missing="reject", empty="zero")
    )
    assert (
        rejected.report.outcome == "disagreement" and rejected.groups[0].actual is None
    )
    excluded = check_aggregates(
        (group((rows[0], missing), "1"),), replace(numeric(), missing="exclude")
    )
    assert excluded.report.verified and excluded.groups[0].excluded == ("missing",)
    no_unit = KeyRecord(
        "unqualified-unit",
        prepare_rdf(Graph().add((URIRef("urn:r0"), VALUE, Literal(1)))),
        URIRef("urn:r0"),
    )
    unknown = check_aggregates(
        (group((no_unit,), "0"),),
        replace(numeric(), missing="exclude", empty="zero"),
    )
    assert unknown.report.outcome == "unverified" and not unknown.groups[0].excluded
    assert unknown.groups[0].actual is None and unknown.groups[0].contributions


def test_min_max_bounds_and_explicit_empty_group_policy() -> None:
    """Exact supported functions and selected bounds have independent controls."""
    rows = records("-1.2", "0", "2.3")
    cases: tuple[tuple[Choice["min", "max"], str], ...] = (
        ("min", "-1.2"),
        ("max", "2.3"),
    )
    for function, expected in cases:
        assert check_aggregates(
            (group(rows, expected),), numeric(function)
        ).report.verified
    assert check_aggregates(
        (group(rows, "2"),), replace(numeric(), comparison="at_most")
    ).report.verified
    assert (
        check_aggregates(
            (group(rows, "2"),), replace(numeric(), comparison="at_least")
        ).report.outcome
        == "disagreement"
    )
    assert check_aggregates((group((), "0"),), numeric()).report.outcome == "unverified"
    assert check_aggregates(
        (group((), "0"),), replace(numeric(), empty="zero")
    ).report.verified
    assert (
        check_aggregates(
            (group((), "0"),), replace(numeric("min"), empty="zero")
        ).report.outcome
        == "unverified"
    )


def test_scopes_blanks_duplicate_occurrences_and_unknown_graph_coverage() -> None:
    """Blank labels from another scope do not select an existing physical record."""
    data = Dataset()
    a, b = URIRef("urn:a"), URIRef("urn:b")
    subject = BNode("record")
    data.graph(a).add((subject, VALUE, Literal(1)))
    data.graph(b).add((subject, VALUE, Literal(2)))
    prepared = prepare_rdf(data)
    owned = next(prepared.graph(a).subjects())
    assert isinstance(owned, BNode)
    row = KeyRecord("a", prepared, owned, graph_name=a)
    spec = AggregateContract("one-graph", "sum", "entities", VALUE)
    assert check_aggregates((group((row,), "1"),), spec).report.verified
    foreign = replace(row, subject=BNode(str(owned)))
    assert (
        check_aggregates((group((foreign,), "1"),), spec).report.outcome == "unverified"
    )
    assert (
        check_aggregates((group((row, row), "2"),), spec).report.outcome
        == "input_error"
    )
    prepared.graph_fidelity = "nonempty-only"
    assert check_aggregates((group((row,), "1"),), spec).report.outcome == "unverified"


def test_cumulative_limits_cancellation_and_paid_completed_group_retention() -> None:
    """Late exhaustion keeps a real earlier mismatch and never accepts a suffix."""
    rows = records("1", "2")
    first = group(rows, "4")
    second = replace(first, identifier="second", expected=Literal(3))
    pilot = Budget()
    baseline = check_aggregates((first,), numeric(), budget=pilot)
    assert baseline.report.outcome == "disagreement"
    for dimension in ("states", "work", "bytes"):
        key = "max_total_bytes" if dimension == "bytes" else "max_" + dimension
        budget = Budget(replace(Limits(), **{key: pilot._counts[dimension] + 16}))
        stopped = check_aggregates((first, second), numeric(), budget=budget)
        assert stopped.report.outcome == "exhausted" and not stopped.report.complete
        assert any(i.code == "data.aggregate_mismatch" for i in stopped.report)
        assert budget._counts.get(dimension, 0) <= getattr(budget.limits, key)
    event = Event()
    event.set()
    stopped = check_aggregates((first,), numeric(), cancel=event)
    assert stopped.report.outcome == "exhausted" and not stopped.groups
    assert check_aggregates((group(rows, "3"),), numeric()).report.verified


@pytest.mark.parametrize("scope", ["default", "named", "union"])
def test_record_scope_and_owned_blank_corpus_keep_consumer_policies_distinct(
    scope: str,
) -> None:
    """One scope never borrows hidden values, and shared facts choose no policy."""
    from bijux_ontology.data.keys import check_record_keys
    from bijux_ontology.data.records.reading import read_records
    from bijux_ontology.data.topology import TopologyRecord, check_record_topology
    from bijux_ontology.declarations.keys import KeyComponent, KeyContract
    from bijux_ontology.declarations.topology import TopologyContract, TopologyRelation
    from bijux_ontology.diagnostics.errors import InputError

    link, primary, hidden = (
        URIRef("urn:" + name) for name in ("link", "primary", "hidden")
    )
    root, child = BNode("root"), BNode("child")
    lexical = Literal("+01", datatype=XSD.integer, normalize=False)
    data = Dataset()
    for graph in (data.default_graph, data.graph(primary)):
        graph.add((root, VALUE, lexical)).add((child, VALUE, Literal(2))).add(
            (root, link, child)
        )
    data.graph(hidden).add((root, VALUE, Literal(3)))
    source = prepare_rdf(data)
    original = source.graph("@default")
    owned_root = next(original.subjects(link, None))
    owned_child = next(original.objects(owned_root, link))
    assert isinstance(owned_root, BNode) and isinstance(owned_child, BNode)
    original_value = next(original.objects(owned_root, VALUE))
    name = "@default" if scope == "default" else primary if scope == "named" else None
    union = scope == "union"
    rows = tuple(
        KeyRecord(identifier, source, subject, graph_name=name, union=union)
        for identifier, subject in (("root", owned_root), ("child", owned_child))
    )
    key_contract = KeyContract("value", (KeyComponent(VALUE),))
    keys = check_record_keys(rows, key_contract, complete=True)
    numeric_contract = AggregateContract("total", "sum", "entities", VALUE)
    aggregated = check_aggregates(
        (AggregateGroup("records", rows, Literal(3), True),), numeric_contract
    )
    topology_contract = TopologyContract(
        (TopologyRelation("children", link, ownership=True, acyclic=True),),
        min_owners=1,
        max_owners=1,
    )
    focuses = (TopologyRecord("root", owned_root), TopologyRecord("child", owned_child))
    topology = check_record_topology(
        source,
        focuses,
        topology_contract,
        roots=("root",),
        complete=True,
        closure="closed",
        graph_name=name,
        union=union,
    )
    assert topology.verified
    assert keys.verified is (not union) and aggregated.report.verified is (not union)
    if union:
        assert (
            keys.outcome == "disagreement" and aggregated.report.outcome == "unverified"
        )
    else:
        assert aggregated.groups[0].actual == 3
        assert aggregated.groups[0].contributions[0].values[0] is original_value
        assert str(original_value) == "+01"
    foreign = BNode(str(owned_root))
    foreign_rows = (replace(rows[0], subject=foreign), rows[1])
    assert (
        check_record_keys(foreign_rows, key_contract, complete=True).outcome
        == "unverified"
    )
    assert not check_aggregates(
        (AggregateGroup("records", foreign_rows, Literal(3), True),), numeric_contract
    ).report.verified
    assert not check_record_topology(
        source,
        (TopologyRecord("root", foreign), focuses[1]),
        topology_contract,
        roots=("root",),
        complete=True,
        closure="closed",
        graph_name=name,
        union=union,
    ).verified
    foreign_alias = replace(rows[0], identifier="foreign", subject=foreign)
    guarded = check_record_keys((*rows, foreign_alias), key_contract, complete=True)
    assert not guarded.verified and any(
        issue.code == "data.key_binding_unverified" for issue in guarded.issues
    )
    operation = Budget()
    reader = read_records(source, operation, graph_name=name, union=union)
    denied = reader.values(foreign, ((VALUE, False),), operation)
    assert (
        not denied.complete and denied.coverage == "foreign_blank" and not denied.values
    )
    source.graph_name = str(hidden)
    with pytest.raises(InputError, match="declaration changed"):
        reader.values(owned_root, ((VALUE, False),), operation)


def test_object_only_record_is_present_but_has_no_countable_description() -> None:
    """Inverse key paths and topology presence do not imply a record description."""
    from bijux_ontology.data.keys import check_record_keys
    from bijux_ontology.data.topology import TopologyRecord, check_record_topology
    from bijux_ontology.declarations.keys import KeyComponent, KeyContract
    from bijux_ontology.declarations.topology import TopologyContract, TopologyRelation

    root, link = URIRef("urn:root"), URIRef("urn:link")
    source = prepare_rdf(
        Graph().add((root, link, BNode())).add((root, VALUE, Literal(1)))
    )
    child = next(source.graph().objects(root, link))
    assert isinstance(child, BNode)
    row = KeyRecord("child", source, child)
    key = KeyContract("parent-value", (KeyComponent(~link / VALUE),))
    assert check_record_keys((row,), key, complete=True).verified
    counted = check_aggregates(
        (AggregateGroup("children", (row,), Literal(1), True),),
        AggregateContract("descriptions", "count", "entities"),
    )
    assert (
        counted.report.outcome == "unverified" and not counted.groups[0].contributions
    )
    topology = check_record_topology(
        source,
        (TopologyRecord("root", root), TopologyRecord("child", child)),
        TopologyContract((TopologyRelation("children", link),)),
        roots=("root",),
        complete=True,
        closure="closed",
    )
    assert topology.verified


def test_unknown_members_keep_independent_findings_in_every_order() -> None:
    """An unsupported number cannot terminate independent presence or unit checks."""
    unsupported, missing, multiple, wrong = map(
        URIRef,
        (
            "urn:unsupported",
            "urn:missing",
            "urn:multiple",
            "urn:wrong-unit",
        ),
    )
    graph = Graph().add(
        (unsupported, VALUE, Literal("1", datatype=XSD.double, normalize=False))
    )
    graph.add((unsupported, UNIT, KG)).add((missing, NAME, Literal("present")))
    graph.add((multiple, VALUE, Literal(1))).add((multiple, VALUE, Literal(2))).add(
        (multiple, UNIT, KG)
    )
    graph.add((wrong, VALUE, Literal(1))).add((wrong, UNIT, URIRef("urn:gram")))
    source = prepare_rdf(graph)
    rows = tuple(
        KeyRecord(str(node), source, node)
        for node in (unsupported, missing, multiple, wrong)
    )
    selected = replace(numeric(), missing="reject", multiplicity="reject", empty="zero")
    expected = None
    for ordering in permutations(rows):
        answer = check_aggregates((group(ordering, "0"),), selected)
        assert answer.report.outcome == "disagreement" and not answer.report.complete
        assert answer.groups[0].actual is None and answer.groups[0].excluded == ()
        observed = tuple(
            sorted((i.code, i.kind, i.location, i.message) for i in answer.report)
        )
        if expected is None:
            expected = observed
        assert observed == expected
        keys = {(i.code, i.kind, i.location) for i in answer.report}
        assert ("data.key_absent", "finding", str(missing)) in keys
        assert ("data.key_absent", "unverified", str(missing)) in keys
        assert ("data.key_multiple", "finding", str(multiple)) in keys
        assert ("data.aggregate_unverified", "unverified", str(unsupported)) in keys
        assert ("data.aggregate_unverified", "unverified", str(wrong)) in keys
        assert not any(i.code == "data.aggregate_mismatch" for i in answer.report)
    bad = URIRef("urn:invalid-lexical")
    term = Literal("bad", datatype=XSD.decimal, normalize=False)
    source = prepare_rdf(Graph().add((bad, VALUE, term)))
    result = check_aggregates((group((KeyRecord("bad", source, bad),), "0"),), selected)
    assert result.groups[0].actual is None and not result.report.complete
    assert any(
        i.code == "data.key_absent" and i.kind == "unverified" for i in result.report
    )
    assert [
        i.location for i in result.report if i.code == "literal.lexical_invalid"
    ] == ["bad"]


def test_incomplete_records_preserve_siblings_and_only_declared_exclusions() -> None:
    """Native incomplete scope stays unknown; exclusions require actual absence."""
    incomplete = prepare_rdf(
        RdfSource(
            '@base <urn:example:x> . <partial> <urn:value> "1"^^<'
            + str(XSD.decimal)
            + "> .",
            format="trig",
            graph_name="@default",
        )
    )
    assert incomplete.graph_fidelity == "nonempty-only"
    first = KeyRecord("partial", incomplete, URIRef("urn:partial"))
    missing_source = prepare_rdf(
        Graph().add((URIRef("urn:missing"), NAME, Literal("present")))
    )
    missing = KeyRecord("missing", missing_source, URIRef("urn:missing"))
    selected = replace(numeric(), missing="reject", empty="zero")
    for rows in ((first, missing), (missing, first)):
        result = check_aggregates((group(rows, "0"),), selected)
        assert result.report.outcome == "disagreement" and not result.report.complete
        assert result.groups[0].actual is None
        assert any(
            i.code == "data.key_absent" and i.kind == "finding" for i in result.report
        )
    allowed = replace(numeric(), missing="exclude", empty="zero")
    valid = records("1")[0]
    result = check_aggregates((group((missing, valid), "1"),), allowed)
    assert result.report.verified and result.groups[0].actual == 1
    assert result.groups[0].excluded == ("missing",)
    unknown = check_aggregates((group((first, missing), "0"),), allowed)
    assert unknown.report.outcome == "unverified" and unknown.groups[0].actual is None
    assert unknown.groups[0].excluded == ("missing",)
    invalid = check_aggregates((group((valid, valid), "2"),), selected)
    assert invalid.report.outcome == "input_error" and invalid.groups[0].actual is None


def test_member_stops_keep_only_original_paid_findings_and_recover() -> None:
    """A real lexical finding and live cancellation never become unknown totals."""
    subject = URIRef("urn:invalid")
    source = prepare_rdf(
        Graph()
        .add((subject, VALUE, Literal("bad", datatype=XSD.decimal, normalize=False)))
        .add((subject, UNIT, KG))
    )
    selected = replace(numeric(), missing="reject", empty="zero")
    bad = KeyRecord("bad", source, subject)
    capped = check_aggregates(
        (group((bad,), "0"),), selected, budget=Budget(Limits(max_issues=1))
    )
    assert capped.report.outcome == "exhausted" and capped.groups[0].actual is None
    assert [
        i.location for i in capped.report if i.code == "literal.lexical_invalid"
    ] == ["bad"]
    missing_source = prepare_rdf(
        Graph().add((URIRef("urn:missing"), NAME, Literal("present")))
    )
    missing = KeyRecord("missing", missing_source, URIRef("urn:missing"))
    event = Event()
    paid: list[str] = []
    previous = sys.getprofile()

    def observe(
        frame: FrameType,
        kind: Choice["call", "return", "c_call", "c_return", "c_exception"],
        result: object,
    ) -> None:
        if previous is not None:
            previous(frame, kind, result)
        if kind == "return" and frame.f_code is IssueCollector.append.__code__:
            issue = frame.f_locals["issue"]
            collector = frame.f_locals["self"]
            if (
                issue.code == "data.key_absent"
                and issue.kind == "finding"
                and any(item is issue for item in collector)
            ):
                paid.append(issue.location)
                event.set()

    sys.setprofile(observe)
    try:
        stopped = check_aggregates(
            (group((missing, bad), "0"),), selected, cancel=event
        )
    finally:
        sys.setprofile(previous)
    assert paid == ["missing"]
    assert stopped.report.outcome == "exhausted" and stopped.groups[0].actual is None
    assert [i.location for i in stopped.report if i.kind == "finding"] == ["missing"]
    assert not any(i.code == "literal.lexical_invalid" for i in stopped.report)
    assert check_aggregates((group(records("1", "2"), "3"),), numeric()).report.verified


@pytest.mark.parametrize(
    ("reference", "measurements", "expected"),
    [
        ("um", (("a", "1000", "nm"), ("a", "1", "um"), ("b", "0.0005", "mm")), "1.5"),
        ("s", (("a", "2", "ms"), ("b", "3000", "us")), "0.005"),
    ],
)
def test_exact_reference_unit_aggregation_retains_original_rdf_contributions(
    reference: str, measurements: tuple[tuple[str, str, str], ...], expected: str
) -> None:
    """Equivalent entity descriptions agree after native length/time conversion."""
    from bijux_ontology.declarations.aggregates import (
        AggregateUnitConversion,
        RdfUnitBinding,
    )

    conversion = AggregateUnitConversion(
        tuple(
            RdfUnitBinding(URIRef("urn:" + code), native)
            for code, native in (
                ("um", "micrometer"),
                ("nm", "nanometer"),
                ("mm", "millimeter"),
                ("s", "second"),
                ("ms", "millisecond"),
                ("us", "microsecond"),
            )
        )
    )
    rows = []
    for index, (entity, value, unit) in enumerate(measurements):
        subject = URIRef("urn:" + entity)
        graph = (
            Graph()
            .add(
                (subject, VALUE, Literal(value, datatype=XSD.decimal, normalize=False))
            )
            .add((subject, UNIT, URIRef("urn:" + unit)))
        )
        rows.append(KeyRecord(str(index), prepare_rdf(graph), subject))
    selected = AggregateContract(
        "length-time",
        "sum",
        "entities",
        VALUE,
        unit=URIRef("urn:" + reference),
        unit_path=UNIT,
        conversion=conversion,
    )
    result = check_aggregates((group(tuple(rows), expected),), selected)
    assert result.report.verified, result.report.to_dict()
    assert result.groups[0].actual == Fraction(expected)
    assert [
        contribution.record for contribution in result.groups[0].contributions
    ] == rows
    assert [
        tuple(str(v) for v in c.values) for c in result.groups[0].contributions
    ] == [(value, "urn:" + unit) for _, value, unit in measurements]
    # The original exact-unit contract keeps its stricter admission rule.
    exact = check_aggregates(
        (group(tuple(rows), expected),), replace(selected, conversion=None)
    )
    assert exact.groups[0].actual is None and not exact.report.verified
    for bad_value, bad_unit in (
        (Literal("1"), URIRef("urn:" + reference)),
        (Literal(1), Literal("urn:" + reference)),
        (Literal(1), URIRef("urn:unknown")),
        (Literal(1), URIRef("urn:s" if reference == "um" else "urn:um")),
    ):
        bad = URIRef("urn:bad")
        source = prepare_rdf(
            Graph().add((bad, VALUE, bad_value)).add((bad, UNIT, bad_unit))
        )
        blocked = check_aggregates(
            (group((*rows, KeyRecord("bad", source, bad)), expected),), selected
        )
        assert blocked.groups[0].actual is None and not blocked.report.verified
        assert len(blocked.groups[0].contributions) == len(rows) + 1
    temperature = replace(
        selected,
        unit=URIRef("urn:K"),
        conversion=AggregateUnitConversion(
            (RdfUnitBinding(URIRef("urn:K"), "kelvin"),)
        ),
    )
    unsupported = check_aggregates((group(tuple(rows), expected),), temperature)
    assert not unsupported.report.verified and unsupported.groups[0].actual is None
