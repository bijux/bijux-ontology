"""Exact group conservation, occurrence identity and dangerous omission controls."""

from dataclasses import replace
from fractions import Fraction
from threading import Event
from typing import Literal as Choice

from rdflib import BNode, Dataset, Graph, Literal, URIRef
from rdflib.namespace import XSD

from bijux_ontology.data.aggregates import AggregateGroup, check_aggregates
from bijux_ontology.data.keys import KeyRecord
from bijux_ontology.declarations.aggregates import AggregateContract
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.sources import RdfSource

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
