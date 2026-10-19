"""Selected RDF becomes immutable input only after every read contract qualifies."""

import time
from dataclasses import dataclass, replace
from decimal import Decimal
from threading import Event, Timer

import pytest
from rdflib import BNode, Graph, Literal, Namespace
from rdflib.namespace import RDF, XSD

from bijux_ontology.data.projection import (
    ProjectedRecord,
    ProjectionRead,
    ReadBinding,
    ReadField,
    ReferenceHandle,
    construct_projected,
    project_records,
)
from bijux_ontology.declarations.types import SubjectIdentity
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.limits import Budget, BudgetKind, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.snapshots import PreparedSource

EX = Namespace("urn:projection:")


def _source() -> PreparedSource:
    return prepare_rdf(
        b"""@prefix e: <urn:projection:> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
e:g { e:parent a e:Parent; e:child _:child;
 e:amount "+00012.3400"^^xsd:decimal; e:items ("a" "b" "a") .
 _:child a e:Child; e:name "Child" . }
e:other { e:parent e:amount "999"^^xsd:decimal . }
""",
        format="trig",
        require_complete_graphs=True,
    )


def _bindings(source: PreparedSource) -> tuple[ReadBinding, ...]:
    child = next(source.graph(EX.g).objects(EX.parent, EX.child))
    assert isinstance(child, BNode)
    return (
        ReadBinding(
            "parent",
            EX.parent,
            (
                ReadField("amount", EX.amount, decoder="decimal"),
                ReadField("children", EX.child, decoder="reference"),
                ReadField(
                    "items", EX.items, mode="list", decoder="string", minimum_members=3
                ),
            ),
            types=(str(EX.Parent),),
            identity=SubjectIdentity(str(EX.parent)),
        ),
        ReadBinding(
            "child",
            child,
            (ReadField("name", EX.name, decoder="string"),),
            types=(str(EX.Child),),
        ),
    )


def _withheld(result: ProjectionRead) -> None:
    called = []
    with pytest.raises(InputError):
        construct_projected(result, lambda rows: called.append(rows))
    assert not called


def test_linked_records_list_decimal_exact_terms_and_trusted_factory() -> None:
    source = _source()
    result = project_records(source, _bindings(source), graph_name=EX.g)
    assert result.report.verified, result.report.to_dict()
    parent, child = result.records
    amount, link, items = parent.fields
    assert amount.values == (Decimal("12.3400"),)
    assert (
        isinstance(amount.values[0], Decimal)
        and amount.values[0].as_tuple().exponent == -4
    )
    assert str(amount.terms[0]) == "+00012.3400"
    assert link.values == (child.reference,)
    assert isinstance(link.values[0], ReferenceHandle)
    assert link.values[0].subject is child.reference.subject
    assert child.reference.source is source and child.reference.graph == EX.g
    assert (
        items.values == ("a", "b", "a") and tuple(map(str, items.terms)) == items.values
    )
    assert len(items.endpoints) == 1 and child.fields[0].values == ("Child",)

    @dataclass(frozen=True)
    class Consumer:
        amount: Decimal
        child: ReferenceHandle
        labels: tuple[object, ...]

    calls = []

    def factory(rows: tuple[ProjectedRecord, ...]) -> Consumer:
        calls.append(rows)
        assert rows is result.records
        return Consumer(
            Decimal(str(rows[0].fields[0].values[0])), child.reference, items.values
        )

    output = construct_projected(result, factory)
    assert output.amount == Decimal("12.3400") and len(calls) == 1
    source.graph(EX.g).remove((None, None, None))
    assert project_records(source, _bindings(source), graph_name=EX.g).report.verified
    inverse = project_records(
        source,
        (
            ReadBinding(
                "child",
                child.reference.subject,
                (
                    ReadField(
                        "parent", ~EX.child, decoder="reference", references="external"
                    ),
                ),
            ),
        ),
        graph_name=EX.g,
    )
    inverse_reference = inverse.records[0].fields[0].values[0]
    assert inverse.report.verified and isinstance(inverse_reference, ReferenceHandle)
    assert inverse_reference.subject == EX.parent


def test_ambiguity_custom_conversion_and_raw_retention_before_factory() -> None:
    graph = (
        Graph()
        .add(
            (
                EX.record,
                EX.amount,
                Literal("01.00", datatype=XSD.decimal, normalize=False),
            )
        )
        .add(
            (
                EX.record,
                EX.amount,
                Literal("2.00", datatype=XSD.decimal, normalize=False),
            )
        )
    )
    binding = ReadBinding(
        "record", EX.record, (ReadField("amount", EX.amount, decoder="decimal"),)
    )
    ambiguous = project_records(graph, (binding,))
    assert (
        ambiguous.report.outcome == "disagreement"
        and ambiguous.records[0].fields[0].state == "ambiguous"
    )
    assert len(ambiguous.records[0].fields[0].terms) == 2
    _withheld(ambiguous)
    many = project_records(
        graph, (replace(binding, fields=(replace(binding.fields[0], mode="many"),)),)
    )
    assert many.report.verified and set(many.records[0].fields[0].values) == {
        Decimal("1.00"),
        Decimal("2.00"),
    }
    raw = Literal("unchanged+01", datatype=EX.Custom, normalize=False)
    custom = Graph().add((EX.record, EX.amount, raw))
    unknown = project_records(custom, (binding,))
    assert unknown.report.outcome == "unverified" and unknown.records[0].fields[
        0
    ].terms == (raw,)
    _withheld(unknown)
    retained = project_records(
        custom, (replace(binding, fields=(replace(binding.fields[0], decoder="raw"),)),)
    )
    assert retained.report.verified and retained.records[0].fields[0].state == "raw"
    assert retained.records[0].fields[0].values == (raw,)
    invalid = project_records(
        Graph().add(
            (
                EX.record,
                EX.amount,
                Literal("not-decimal", datatype=XSD.decimal, normalize=False),
            )
        ),
        (binding,),
    )
    assert invalid.report.outcome == "disagreement" and all(
        issue.location == "record/amount/0" for issue in invalid.report.issues
    )
    _withheld(invalid)


def test_broken_lists_empty_lists_and_member_conversion() -> None:
    source = _source()
    owned = source.graph(EX.g)
    head = next(owned.objects(EX.parent, EX.items))
    binding = ReadBinding(
        "parent",
        EX.parent,
        (ReadField("items", EX.items, mode="list", decoder="string"),),
    )
    for kind in ("missing", "cycle", "branch"):
        graph = source.graph(EX.g)
        if kind == "missing":
            graph.remove((head, RDF.rest, None))
        elif kind == "cycle":
            graph.set((head, RDF.rest, head))
        else:
            graph.add((head, RDF.first, Literal("other")))
        failed = project_records(graph, (binding,))
        assert (
            failed.report.outcome == "disagreement"
            and failed.records[0].fields[0].state == "invalid"
        )
        _withheld(failed)
    graph = Graph().add((EX.parent, EX.items, RDF.nil))
    empty = project_records(graph, (binding,))
    assert empty.report.verified and empty.records[0].fields[0].endpoints == (RDF.nil,)
    assert empty.records[0].fields[0].terms == empty.records[0].fields[0].values == ()
    bad_member = project_records(
        source,
        (replace(binding, fields=(replace(binding.fields[0], decoder="decimal"),)),),
        graph_name=EX.g,
    )
    assert bad_member.report.outcome == "unverified"
    _withheld(bad_member)
    missing = project_records(Graph().add((EX.parent, RDF.type, EX.Parent)), (binding,))
    assert (
        missing.report.outcome == "disagreement"
        and missing.records[0].fields[0].state == "missing"
    )
    optional = project_records(
        Graph().add((EX.parent, RDF.type, EX.Parent)),
        (replace(binding, fields=(replace(binding.fields[0], minimum=0),)),),
    )
    assert optional.report.verified


def test_exact_graph_blank_and_reference_ownership() -> None:
    source = _source()
    bindings = _bindings(source)
    foreign = replace(bindings[1], subject=BNode(str(bindings[1].subject)))
    failed = project_records(source, (bindings[0], foreign), graph_name=EX.g)
    assert failed.report.outcome == "input_error"
    _withheld(failed)
    missing_child = project_records(source, (bindings[0],), graph_name=EX.g)
    assert missing_child.report.outcome == "unverified"
    _withheld(missing_child)
    external = replace(
        bindings[0], fields=(replace(bindings[0].fields[1], references="external"),)
    )
    assert project_records(source, (external,), graph_name=EX.g).report.verified
    wrong_graph = project_records(source, (bindings[0],), graph_name=EX.other)
    assert not wrong_graph.report.verified
    _withheld(wrong_graph)
    assert (
        project_records(source, bindings, graph_name=Literal(str(EX.g))).report.outcome
        == "input_error"
    )
    unselected = project_records(source, bindings)
    assert unselected.report.outcome == "input_error"
    _withheld(unselected)
    data = source.dataset()
    name = BNode()
    data.add_graph(name)
    data.graph(name).add((EX.record, EX.name, Literal("named blank graph")))
    blank_source = prepare_rdf(data)
    exact_name = next(
        item.identifier
        for item in blank_source.dataset().graphs()
        if isinstance(item.identifier, BNode)
    )
    record = ReadBinding("record", EX.record, (ReadField("name", EX.name),))
    assert project_records(
        blank_source, (record,), graph_name=exact_name
    ).report.verified
    assert (
        project_records(
            blank_source, (record,), graph_name=BNode(str(exact_name))
        ).report.outcome
        == "input_error"
    )


def test_narrow_python_decoders_preserve_distinct_literal_terms() -> None:
    graph = (
        Graph()
        .add(
            (
                EX.record,
                EX.number,
                Literal("+001", datatype=XSD.integer, normalize=False),
            )
        )
        .add(
            (EX.record, EX.number, Literal("1", datatype=XSD.integer, normalize=False))
        )
        .add((EX.record, EX.flag, Literal("0", datatype=XSD.boolean, normalize=False)))
        .add((EX.record, EX.name, Literal("Café", lang="fr")))
    )
    binding = ReadBinding(
        "record",
        EX.record,
        (
            ReadField("numbers", EX.number, mode="many", decoder="integer"),
            ReadField("flag", EX.flag, decoder="boolean"),
            ReadField("name", EX.name, decoder="string"),
        ),
    )
    result = project_records(graph, (binding,))
    assert result.report.verified
    numbers, flag, name = result.records[0].fields
    assert numbers.values == (1, 1) and {str(term) for term in numbers.terms} == {
        "+001",
        "1",
    }
    assert flag.values == (False,) and str(flag.terms[0]) == "0"
    assert name.values == ("Café",) and name.terms[0].language == "fr"  # type: ignore[attr-defined]
    multilingual = (
        Graph()
        .add((EX.record, EX.name, Literal("same")))
        .add((EX.record, EX.name, Literal("same", lang="en")))
    )
    assert project_records(
        multilingual,
        (ReadBinding("mixed", EX.record, (ReadField("names", EX.name, mode="many"),)),),
    ).report.verified
    unsupported = project_records(
        graph, (replace(binding, fields=(ReadField("path", EX.name | EX.number),)),)
    )
    assert unsupported.report.outcome == "unverified"
    _withheld(unsupported)


def test_shared_budget_paid_prefix_cancel_and_invalid_allowance() -> None:
    source = prepare_rdf(
        Graph()
        .add((EX.record, EX.value, Literal("one")))
        .add((EX.record, EX.value, Literal("two")))
        .add((EX.record, EX.name, Literal("name")))
    )
    binding = ReadBinding(
        "record",
        EX.record,
        (ReadField("ambiguous", EX.value), ReadField("name", EX.name)),
    )

    class Recording(Budget):
        def __init__(self) -> None:
            super().__init__()
            self.cuts: list[tuple[str, int, int, int]] = []

        def consume(self, kind: BudgetKind, amount: int = 1) -> None:
            self.cuts.append(
                (kind, self._counts.get(kind, 0), amount, len(self._retained_issues))
            )
            super().consume(kind, amount)

    def reachable(report: Report) -> set[int]:
        return {id(issue) for issue in report.issues}.union(
            *(reachable(child) for child in report.children)
        )

    base = Recording()
    original = project_records(source, (binding,), budget=base)
    assert original.report.outcome == "disagreement"
    for kind, field in (
        ("work", "max_work"),
        ("bytes", "max_total_bytes"),
        ("states", "max_states"),
    ):
        cuts = [
            old + amount - 1
            for actual, old, amount, paid in base.cuts
            if actual == kind and amount and paid
        ]
        assert cuts
        cap = cuts[0]
        budget = Budget(replace(Limits(), **{field: cap}))
        result = project_records(source, (binding,), budget=budget)
        assert result.report.outcome == "exhausted" and not result.report.verified
        assert not set(budget._retained_issues) - reachable(result.report)
        assert any(
            issue.code == "projection.ambiguous" for issue in result.report.issues
        )
        _withheld(result)
    cancel = Event()
    cancel.set()
    result = project_records(source, (binding,), cancel=cancel)
    assert result.report.outcome == "exhausted"
    _withheld(result)
    for bad in ({"limits": object()}, {"budget": object()}):
        with pytest.raises(InputError):
            project_records(source, (binding,), **bad)  # type: ignore[arg-type]


def test_actual_deadline_active_cancellation_and_fresh_recovery() -> None:
    graph = Graph()
    for index in range(1000):
        graph.add((EX[str(index)], EX.name, Literal("record")))
    source = prepare_rdf(graph)
    binding = ReadBinding("record", EX["0"], (ReadField("name", EX.name),))
    start = time.monotonic()
    expired = project_records(source, (binding,), timeout=0.001)
    assert expired.report.outcome == "exhausted" and time.monotonic() - start < 5
    _withheld(expired)
    cancel = Event()
    timer = Timer(0.001, cancel.set)
    timer.start()
    start = time.monotonic()
    try:
        cancelled = project_records(source, (binding,), cancel=cancel)
    finally:
        timer.join()
    assert cancelled.report.outcome == "exhausted" and time.monotonic() - start < 5
    _withheld(cancelled)
    assert project_records(source, (binding,)).report.verified


def test_rdf_list_string_aliases_are_one_exact_first_term() -> None:
    """Native RDFLib spelling aliases must not manufacture a branched RDF list."""
    head = BNode()
    graph = (
        Graph()
        .add((EX.record, EX.items, head))
        .add((head, RDF.first, Literal("same")))
        .add((head, RDF.first, Literal("same", datatype=XSD.string)))
        .add((head, RDF.rest, RDF.nil))
    )
    result = project_records(
        graph,
        (
            ReadBinding(
                "record",
                EX.record,
                (ReadField("items", EX.items, mode="list", decoder="string"),),
            ),
        ),
    )
    assert result.report.verified, result.report.to_dict()
    assert result.records[0].fields[0].values == ("same",)
