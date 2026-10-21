"""Foreign references correlate actual independent corpora without inventing scope."""

from dataclasses import replace
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
from bijux_ontology.execution.limits import Budget, Limits
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
