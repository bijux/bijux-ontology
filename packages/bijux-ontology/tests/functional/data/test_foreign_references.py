"""Foreign references correlate actual independent corpora without inventing scope."""

import sys
from dataclasses import replace
from types import FrameType
from typing import Any, cast
from typing import Literal as Choice

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, XSD

from bijux_ontology.data.foreign_references import (
    ForeignReferenceContract,
    check_foreign_references,
    prepare_foreign_targets,
)
from bijux_ontology.data.keys.types import KeyRecord
from bijux_ontology.declarations.keys import KeyComponent, KeyContract
from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.rdf.paths.traversal import path_values
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.types import RdfSource

EX = Namespace("urn:references:")


def _contract(**options: Any) -> ForeignReferenceContract:
    """Choose distinct source/target mappings with shared exact comparison intent."""
    return ForeignReferenceContract(
        "instrument",
        KeyContract("submission", (KeyComponent(EX.maker), KeyComponent(EX.serial))),
        KeyContract(
            "reference", (KeyComponent(EX.manufacturer), KeyComponent(EX.number))
        ),
        **options,
    )


def _source(serial: str = "123") -> tuple[KeyRecord, ...]:
    """Prepare actual submitted business references independently of the catalogue."""
    graph = Graph().add((EX.submission, EX.maker, Literal("Acme")))
    graph.add((EX.submission, EX.serial, Literal(serial)))
    return (KeyRecord("submission.json/record", prepare_rdf(graph), EX.submission),)


def _targets(*subjects: URIRef) -> tuple[KeyRecord, ...]:
    """Own one target catalogue with distinct selected record identities."""
    graph = Graph()
    for subject in subjects:
        graph.add((subject, EX.manufacturer, Literal("Acme")))
        graph.add((subject, EX.number, Literal("123")))
        graph.add((subject, RDF.type, EX.Instrument))
    prepared = prepare_rdf(graph)
    return tuple(KeyRecord(str(subject), prepared, subject) for subject in subjects)


def test_compound_resolution_reuses_index_and_checks_actual_target_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Join separately supplied records and reuse actual selected target assertions."""
    contract = _contract(target_model="instrument")
    target = _targets(EX.instrument)
    index = prepare_foreign_targets(target, contract, complete=True)
    import bijux_ontology.data.records.reading as reading

    original = path_values
    visited = []

    def observed(*args: Any, **kwargs: Any) -> Any:
        visited.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(reading, "path_values", observed)
    models = (ModelDeclaration("instrument", (str(EX.Instrument),)),)
    for _ in range(2):
        result = check_foreign_references(
            _source(), index, source_complete=True, models=models
        )
        assert result.report.verified, result.report.to_dict()
        assert result.resolutions[0].source.identifier == "submission.json/record"
        assert result.resolutions[0].targets == target
        assert result.resolutions[0].status == "resolved"
    assert visited == [EX.submission] * 4  # Target extraction is reused.
    wrong = check_foreign_references(
        _source(),
        index,
        source_complete=True,
        models=(ModelDeclaration("instrument", (str(EX.Other),)),),
    )
    assert not wrong.report.verified
    assert any(i.code == "emission.types_mismatch" for i in wrong.report.issues)


def test_missing_ambiguous_and_explicit_multi_target_counts_are_distinct() -> None:
    """Actual source records and original key values remain correlated in failures."""
    missing = check_foreign_references(
        _source("404"),
        prepare_foreign_targets(_targets(EX.one), _contract(), complete=True),
        source_complete=True,
    )
    assert missing.resolutions[0].status == "missing"
    issue = next(i for i in missing.report.issues if i.code == "data.foreign_missing")
    assert issue.location == "submission.json/record"
    assert (
        cast(dict[str, Any], issue.evidence)["records"][0]["record"]
        == "submission.json/record"
    )
    targets = _targets(EX.one, EX.two)
    duplicate = prepare_foreign_targets(targets, _contract(), complete=True)
    assert duplicate.duplicates == ((targets[0], targets[1]),)
    ambiguous = check_foreign_references(_source(), duplicate, source_complete=True)
    assert ambiguous.resolutions[0].status == "ambiguous"
    assert {i.code for i in ambiguous.report.issues} >= {
        "data.foreign_target_duplicate",
        "data.foreign_ambiguous",
    }
    allowed = prepare_foreign_targets(
        targets, _contract(maximum_targets=2), complete=True
    )
    assert check_foreign_references(
        _source(), allowed, source_complete=True
    ).report.verified
    with pytest.raises(InputError, match="unique"):
        prepare_foreign_targets((targets[0], targets[0]), _contract(), complete=True)


def test_partial_open_and_pinned_graph_lookup_never_manufacture_missing() -> None:
    """A hidden target and unprovided partitions cannot become lookup evidence."""
    dataset = Dataset()
    dataset.graph(EX.visible).add((EX.one, EX.manufacturer, Literal("Acme")))
    dataset.graph(EX.visible).add((EX.one, EX.number, Literal("different")))
    dataset.graph(EX.hidden).add((EX.one, EX.manufacturer, Literal("Acme")))
    dataset.graph(EX.hidden).add((EX.one, EX.number, Literal("123")))
    selected = prepare_rdf(RdfSource(dataset, graph_name=str(EX.visible)))
    records = (KeyRecord("selected", selected, EX.one),)
    closed = prepare_foreign_targets(records, _contract(), complete=True)
    assert (
        check_foreign_references(_source(), closed, source_complete=True)
        .resolutions[0]
        .status
        == "missing"
    )
    for index in (
        prepare_foreign_targets(records, _contract(), complete=False),
        prepare_foreign_targets(records, _contract(lookup="open"), complete=True),
    ):
        result = check_foreign_references(_source(), index, source_complete=True)
        assert result.resolutions[0].status == "unverified"
        assert not any(i.code == "data.foreign_missing" for i in result.report.issues)
    partial = prepare_foreign_targets(
        _targets(EX.one, EX.two), _contract(), complete=False
    )
    result = check_foreign_references(_source(), partial, source_complete=True)
    assert result.resolutions[0].status == "ambiguous"  # Known maximum survives.


def test_numeric_values_require_explicit_regime_and_blank_keys_remain_owned() -> None:
    """RDF lexical identity and qualified value identity are independent choices."""
    a = prepare_rdf(
        Graph().add(
            (
                EX.submission,
                EX.key,
                Literal("01", datatype=XSD.integer, normalize=False),
            )
        )
    )
    b = prepare_rdf(
        Graph().add(
            (EX.one, EX.key, Literal("1.0", datatype=XSD.decimal, normalize=False))
        )
    )
    source = (KeyRecord("numeric-source", a, EX.submission),)
    target = (KeyRecord("numeric-target", b, EX.one),)
    policies: tuple[tuple[Choice["rdf-term", "owl2-datatype-map"], str], ...] = (
        ("rdf-term", "missing"),
        ("owl2-datatype-map", "resolved"),
    )
    for regime, expected in policies:
        key = KeyContract("numeric", (KeyComponent(EX.key, equality=regime),))
        contract = ForeignReferenceContract("numeric", key, key)
        result = check_foreign_references(
            source,
            prepare_foreign_targets(target, contract, complete=True),
            source_complete=True,
        )
        assert result.resolutions[0].status == expected
    blank = BNode("same")
    a = prepare_rdf(Graph().add((EX.submission, EX.key, blank)))
    b = prepare_rdf(Graph().add((EX.one, EX.key, blank)))
    key = KeyContract("blank", (KeyComponent(EX.key),))
    index = prepare_foreign_targets(
        (KeyRecord("b", b, EX.one),),
        ForeignReferenceContract("blank", key, key),
        complete=True,
    )
    result = check_foreign_references(
        (KeyRecord("a", a, EX.submission),), index, source_complete=True
    )
    assert result.resolutions[0].status == "unverified"


def test_late_limits_preserve_completed_source_failures_and_owned_index() -> None:
    """A later selected record cannot erase an earlier proved missing reference."""
    index = prepare_foreign_targets(_targets(EX.one), _contract(), complete=True)
    first = _source("missing")[0]
    later = replace(first, identifier="x" * 10000, subject=EX.other)
    result = check_foreign_references(
        (first, later), index, source_complete=True, limits=Limits(max_bytes=5000)
    )
    assert result.report.outcome == "exhausted"
    assert any(i.code == "data.foreign_missing" for i in result.report.issues)
    assert result.resolutions[0].status == "missing"
    again = check_foreign_references(_source(), index, source_complete=True)
    assert again.report.verified
    budget = Budget(Limits(max_issues=1))
    second = replace(first, identifier="other", subject=EX.other)
    result = check_foreign_references(
        (first, second), index, source_complete=True, budget=budget
    )
    assert result.report.outcome == "exhausted"
    assert any(i.code == "data.foreign_missing" for i in result.report.issues)
    assert not check_foreign_references(
        _source(), index, source_complete=False
    ).report.verified

    excluding = replace(
        index.contract, source=replace(index.contract.source, absence="exclude")
    )
    targets = prepare_foreign_targets(_targets(EX.one), excluding, complete=True)
    excluded = replace(first, identifier="excluded", subject=EX.other)
    empty = check_foreign_references((excluded,), targets, source_complete=True)
    assert not empty.report.verified and not empty.resolutions
    assert empty.report.exclusions == ("excluded",)
    assert any(i.code == "data.foreign_source_unverified" for i in empty.report.issues)
    mixed = check_foreign_references(
        (*_source(), excluded), targets, source_complete=True
    )
    assert mixed.report.verified and len(mixed.resolutions) == 1
    assert mixed.report.exclusions == ("excluded",)

    astral = replace(first, identifier="😀" * 1000)
    escaped = check_foreign_references(
        (astral,), index, source_complete=True, limits=Limits(max_bytes=10000)
    )
    assert escaped.report.outcome == "exhausted"
    assert all(
        len((issue.evidence_json or "").encode()) <= 10000
        for issue in escaped.report.issues
    )
    duplicate_records = tuple(
        replace(record, identifier=("😀" if i == 0 else "😃") * 500)
        for i, record in enumerate(_targets(EX.one, EX.two))
    )
    duplicate = prepare_foreign_targets(
        duplicate_records, _contract(), complete=True, limits=Limits(max_bytes=10000)
    )
    assert duplicate.report.outcome == "exhausted"
    assert all(
        len((issue.evidence_json or "").encode()) <= 10000
        for issue in duplicate.report.issues
    )


def _repeated_sources(count: int = 4) -> tuple[KeyRecord, ...]:
    """Select distinct real source subjects that reference one actual target key."""
    graph = Graph()
    for number in range(count):
        subject = EX[f"submission/{number}"]
        graph.add((subject, EX.maker, Literal("Acme")))
        graph.add((subject, EX.serial, Literal("123")))
    source = prepare_rdf(graph)
    return tuple(
        KeyRecord(f"submission/{number}", source, EX[f"submission/{number}"])
        for number in range(count)
    )


def test_repeated_target_failure_retains_all_correlations_and_one_actual_check() -> (
    None
):
    target_records = _targets(EX.instrument)
    contract = _contract(target_model="instrument")
    index = prepare_foreign_targets(target_records, contract, complete=True)
    partial = prepare_foreign_targets(target_records, contract, complete=False)
    records = _repeated_sources()
    native_calls = []

    def observe_native(frame: FrameType, event: str, value: object) -> None:
        if event == "call" and frame.f_code is check_rdf_emission.__code__:
            native_calls.append(frame.f_locals["usages"])

    previous = sys.getprofile()
    sys.setprofile(observe_native)
    try:
        failed = check_foreign_references(
            records,
            index,
            source_complete=True,
            models=(ModelDeclaration("instrument", (str(EX.Other),)),),
        )
        accepted = check_foreign_references(
            records,
            index,
            source_complete=True,
            models=(ModelDeclaration("instrument", (str(EX.Instrument),)),),
        )
        incomplete = check_foreign_references(
            records,
            partial,
            source_complete=True,
            models=(ModelDeclaration("instrument", (str(EX.Other),)),),
        )
    finally:
        sys.setprofile(previous)
    assert len(native_calls) == 2  # One check for each independent actual model policy.
    assert accepted.report.verified
    assert len(incomplete.resolutions) == len(records)
    assert all(row.status == "unverified" for row in incomplete.resolutions)
    assert not any(issue.code == "data.foreign_missing" for issue in incomplete.report)
    assert [row.source for row in failed.resolutions] == list(records)
    assert all(row.status == "resolved" for row in failed.resolutions)
    reports = failed.report.children[1:]
    assert len(reports) == len(records) and all(
        child is reports[0] for child in reports
    )
    assert all(row.targets == target_records for row in failed.resolutions)
    assert [issue.code for issue in failed.report.issues] == [
        "emission.types_mismatch"
    ] * len(records)
    assert all(issue.location == str(EX.instrument) for issue in failed.report.issues)


@pytest.mark.parametrize("selection", ["named", "blank-graphs", "blank-subjects"])
def test_target_obligations_distinguish_physical_graphs_and_owned_blanks(
    selection: str,
) -> None:
    records = []
    if selection == "blank-subjects":
        for number in range(2):
            supplied = BNode("same-looking")
            graph = Graph().add((supplied, EX.manufacturer, Literal("Acme")))
            graph.add((supplied, EX.number, Literal("123")))
            graph.add((supplied, RDF.type, EX.Instrument if number == 0 else EX.Other))
            source = prepare_rdf(graph)
            owned = next(source.graph().subjects(EX.manufacturer, Literal("Acme")))
            assert isinstance(owned, BNode)
            records.append(KeyRecord(f"target/{number}", source, owned))
    else:
        dataset = Dataset()
        names = (EX.one, EX.two) if selection == "named" else (BNode(), BNode())
        for number, name in enumerate(names):
            graph = dataset.graph(name)
            graph.add((EX.instrument, EX.manufacturer, Literal("Acme")))
            graph.add((EX.instrument, EX.number, Literal("123")))
            graph.add(
                (EX.instrument, RDF.type, EX.Instrument if number == 0 else EX.Other)
            )
        source = prepare_rdf(dataset)
        exported = source.dataset()
        owned_names = tuple(
            graph.identifier
            for graph in exported.graphs()
            if isinstance(graph.identifier, URIRef | BNode)
            and (EX.instrument, EX.manufacturer, Literal("Acme")) in graph
        )
        records.extend(
            KeyRecord(f"target/{number}", source, EX.instrument, graph_name=name)
            for number, name in enumerate(owned_names)
        )
    index = prepare_foreign_targets(
        tuple(records),
        _contract(maximum_targets=2, target_model="instrument"),
        complete=True,
    )
    assert index.complete
    calls = []

    def observe_native(frame: FrameType, event: str, value: object) -> None:
        if event == "call" and frame.f_code is check_rdf_emission.__code__:
            calls.append(frame.f_locals["source"])

    previous = sys.getprofile()
    sys.setprofile(observe_native)
    try:
        result = check_foreign_references(
            _repeated_sources(2),
            index,
            source_complete=True,
            models=(ModelDeclaration("instrument", (str(EX.Instrument),)),),
        )
    finally:
        sys.setprofile(previous)
    assert len(calls) == 2
    assert len(result.resolutions) == 2
    assert all(len(row.targets) == 2 for row in result.resolutions)
    children = result.report.children[1:]
    assert len(children) == 4
    assert children[0] is children[2] and children[1] is children[3]
    assert children[0] is not children[1]
    assert sorted(child.verified for child in children[:2]) == [False, True]


@pytest.mark.parametrize(
    "attribute", ["document_iri", "literal_fidelity", "graph_name"]
)
def test_target_index_cannot_reuse_changed_origin_fidelity_or_declared_scope(
    attribute: str,
) -> None:
    targets = _targets(EX.instrument)
    index = prepare_foreign_targets(
        targets, _contract(target_model="instrument"), complete=True
    )
    source = targets[0].source
    setattr(source, attribute, "urn:changed")
    with pytest.raises(InputError, match="changed"):
        check_foreign_references(
            _source(),
            index,
            source_complete=True,
            models=(ModelDeclaration("instrument", (str(EX.Instrument),)),),
        )


def test_paid_target_failure_survives_later_parent_work_exhaustion() -> None:
    index = prepare_foreign_targets(
        _targets(EX.instrument), _contract(target_model="instrument"), complete=True
    )
    budget = Budget()
    paid: list[Issue] = []

    def observe_paid(frame: FrameType, event: str, value: object) -> None:
        if event != "return" or frame.f_code is not IssueCollector.append.__code__:
            return
        issue = frame.f_locals.get("issue")
        if (
            not isinstance(issue, Issue)
            or issue.code != "emission.types_mismatch"
            or paid
        ):
            return
        paid.append(issue)
        budget.consume("work", budget.remaining("work"))

    previous = sys.getprofile()
    sys.setprofile(observe_paid)
    try:
        result = check_foreign_references(
            _repeated_sources(3),
            index,
            source_complete=True,
            models=(ModelDeclaration("instrument", (str(EX.Other),)),),
            budget=budget,
        )
    finally:
        sys.setprofile(previous)
    assert len(paid) == 1
    assert result.report.outcome == "exhausted"
    assert any(issue is paid[0] for issue in result.report.issues)
    assert len(result.resolutions) == 1
    assert result.resolutions[0].source.identifier == "submission/0"
    assert {issue.code for issue in result.report.issues} == {
        "emission.types_mismatch",
        "execution.limit_exceeded",
    }


def test_mixed_repeated_operands_share_native_setup_without_skipping_pairs() -> None:
    """A finite join pays every pair while admitting each builtin operand once."""
    from importlib.metadata import version

    import bijux_ontology.rdf.literals.checking as literals
    import bijux_ontology.rdf.value_comparison.observations as scalar

    def records(role: str) -> tuple[KeyRecord, ...]:
        graph = Graph()
        for index in range(8):
            subject = EX[f"{role}{index}"]
            graph.add(
                (
                    subject,
                    EX.number,
                    Literal("1.0", datatype=XSD.float, normalize=False),
                )
            )
            graph.add(
                (
                    subject,
                    EX.date,
                    Literal(
                        f"2020-01-{index + 1:02}T00:00:00Z",
                        datatype=XSD.dateTime,
                        normalize=False,
                    ),
                )
            )
        source = prepare_rdf(graph)
        return tuple(
            KeyRecord(f"{role}{index}", source, EX[f"{role}{index}"])
            for index in range(8)
        )

    pytest.importorskip("elementpath")
    key = KeyContract(
        "mixed",
        (
            KeyComponent(EX.number, equality="sparql-1.1"),
            KeyComponent(EX.date, equality="sparql-1.1"),
        ),
    )
    contract = ForeignReferenceContract("mixed", key, key)
    targets = records("target")
    index = prepare_foreign_targets(targets, contract, complete=True)
    assert index.complete, index.report.to_dict()
    sources = records("source")
    counts = {
        "pairs": 0,
        "queries": 0,
        "stores": 0,
        "calendars": 0,
        "admissions": 0,
        "release": 0,
    }
    original_profile = sys.getprofile()

    def observed(
        frame: FrameType,
        event: Choice["call", "return", "c_call", "c_return", "c_exception"],
        arg: object,
    ) -> None:
        if original_profile is not None:
            original_profile(frame, event, arg)
        if event != "call":
            return
        if frame.f_code is scalar.ScalarComparisons.compare.__code__:
            counts["pairs"] += 1
        elif frame.f_code is scalar.ScalarComparisons._promoted_equal.__code__:
            counts["queries"] += 1
            if frame.f_locals["self"]._store is None:
                counts["stores"] += 1
        elif frame.f_code is scalar.ScalarComparisons._calendar_value.__code__:
            counts["calendars"] += 1
        elif frame.f_code is literals.check_literal.__code__:
            counts["admissions"] += 1
        elif (
            frame.f_code is version.__code__
            and frame.f_back is not None
            and frame.f_back.f_code
            is scalar.ScalarComparisons._calendar_backend.__code__
        ):
            counts["release"] += 1

    budget = Budget()
    try:
        sys.setprofile(observed)
        result = check_foreign_references(
            sources, index, source_complete=True, budget=budget
        )
    finally:
        sys.setprofile(original_profile)
    assert result.report.verified, result.report.to_dict()
    assert [
        (row.source.identifier, row.status, row.targets) for row in result.resolutions
    ] == [(f"source{i}", "resolved", (targets[i],)) for i in range(8)]
    assert counts == {
        "pairs": 128,
        "queries": 64,
        "stores": 1,
        "calendars": 8,
        "admissions": 9,
        "release": 1,
    }
    assert budget.remaining("resources") == budget.limits.max_resources - 3
    # The prepared index retains no scalar context or authorization for a new call.
    assert check_foreign_references(
        sources, index, source_complete=True
    ).report.verified
