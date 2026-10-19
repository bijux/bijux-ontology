"""Actual CSVW metadata and cell parsing produce owned explicitly checked RDF."""

from decimal import InvalidOperation, Rounded, getcontext, localcontext
from typing import Any

import pytest
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.data.compatibility import check_data_compatibility
from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    OutputRequirement,
    ValueDeclaration,
)
from bijux_ontology.declarations.value_constraints import ValueConstraints
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.interfaces.adapters.csvw import CsvwCheck, extract_csvw
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.rdf.sources.snapshots import PreparedSource

pytest.importorskip("csvw")
EX = Namespace("https://csvw.example/")
URL = str(EX["table.csv"])
BASE = str(EX["metadata.json"])


def description() -> dict[str, Any]:
    """Keep all RDF intent explicit in original standard CSVW metadata."""
    return {
        "@context": "http://www.w3.org/ns/csvw",
        "url": "table.csv",
        "tableSchema": {
            "aboutUrl": str(EX) + "row/{id}",
            "primaryKey": "id",
            "columns": [
                {"name": "id", "suppressOutput": True},
                {
                    "name": "amount",
                    "datatype": "decimal",
                    "propertyUrl": str(EX.amount),
                },
                {
                    "name": "parent",
                    "valueUrl": str(EX) + "row/{parent}",
                    "propertyUrl": str(EX.parent),
                },
                {
                    "name": "tags",
                    "datatype": "string",
                    "separator": "|",
                    "null": "NA",
                    "propertyUrl": str(EX.tag),
                },
                {
                    "name": "active",
                    "datatype": "boolean",
                    "propertyUrl": str(EX.active),
                },
            ],
        },
    }


DATA = (
    "id,amount,parent,tags,active\n"
    "a,12345678901234567890.0123456789,b,one|two,true\n"
    "b,2,a,NA,0\n"
)


def test_native_values_and_selected_real_checks_preserve_mapping_and_precision() -> (
    None
):
    """Declared foreign IRIs and exact Decimal values reach ordinary RDF checks."""
    vocabulary = Graph()
    for predicate, datatype in (
        (EX.amount, XSD.decimal),
        (EX.tag, XSD.string),
        (EX.active, XSD.boolean),
    ):
        vocabulary.add((predicate, RDF.type, OWL.DatatypeProperty))
        vocabulary.add((predicate, RDFS.range, datatype))
    vocabulary.add((EX.parent, RDF.type, OWL.ObjectProperty))
    calls: list[PreparedSource] = []

    def native(source: PreparedSource, budget: Budget) -> Report:
        """Use existing named ontology obligations on actual produced data."""
        calls.append(source)
        return check_data_compatibility(source, vocabulary, budget=budget)

    def field(source: PreparedSource, budget: Budget) -> Report:
        """Check actual emitted foreign identifiers with ordinary application rules."""
        model = ModelDeclaration(
            "row",
            (),
            (
                FieldDeclaration(
                    "parent",
                    str(EX.parent),
                    (
                        ValueDeclaration(
                            "iri",
                            constraints=ValueConstraints(enumeration=(EX["row/b"],)),
                        ),
                    ),
                    output=OutputRequirement(
                        allow_null=True,
                        allow_empty=True,
                        minimum_values=1,
                        maximum_values=1,
                    ),
                ),
            ),
        )
        return check_rdf_emission(
            source, (model,), (RdfModelUsage("row", EX["row/a"]),), budget=budget
        )

    metadata, tables = description(), {URL: DATA}
    precision = getcontext().prec
    result = extract_csvw(
        metadata,
        tables,
        base=BASE,
        checks=(CsvwCheck("native", native), CsvwCheck("field", field)),
    )
    assert result.report.verified and all(r.verified for r in result.check_reports)
    assert calls == [result.source] and getcontext().prec == precision
    assert result.source is not None
    graph = result.source.graph()
    decimal = next(graph.objects(EX["row/a"], EX.amount))
    assert str(decimal) == "12345678901234567890.0123456789"
    assert isinstance(decimal, Literal) and decimal.datatype == XSD.decimal
    assert (EX["row/a"], EX.parent, EX["row/b"]) in graph
    assert set(graph.objects(EX["row/a"], EX.tag)) == {
        Literal("one", datatype=XSD.string),
        Literal("two", datatype=XSD.string),
    }
    assert not tuple(graph.objects(EX["row/b"], EX.tag))
    amount = result.cells[1]
    assert (amount.row, amount.line, amount.column, amount.text) == (
        1,
        2,
        2,
        "12345678901234567890.0123456789",
    )
    assert amount.statements[0][2] == decimal
    metadata["tableSchema"]["columns"][1]["propertyUrl"] = str(EX.other)
    tables[URL] = "changed"
    graph.remove((None, None, None))
    assert len(result.source.graph()) > 0 and result.cells[1] == amount


def test_mutated_cells_and_declared_keys_fail_while_shared_subjects_remain_legal() -> (
    None
):
    """Physical row/cell evidence stays original rather than inferred from triples."""
    invalid = extract_csvw(
        description(),
        {URL: DATA.replace("b,2,a,NA,0", "b,not-a-number,a,NA,0")},
        base=BASE,
    )
    issue = next(i for i in invalid.report.issues if i.code == "csvw.cell_invalid")
    assert invalid.source is None and invalid.report.outcome == "disagreement"
    assert issue.actual == "not-a-number" and issue.details is not None
    assert issue.details.source == URL
    assert issue.details.path == ("rows", 2, "columns", 2)
    assert invalid.cells[6].text == "not-a-number"
    repeated = DATA.replace("b,2,a,NA,0", "a,2,a,NA,0")
    duplicate = extract_csvw(description(), {URL: repeated}, base=BASE)
    assert duplicate.source is None
    assert any(i.code == "csvw.duplicate_key" for i in duplicate.report.issues)
    shared = description()
    shared["tableSchema"].pop("primaryKey")
    legal = extract_csvw(shared, {URL: repeated}, base=BASE)
    assert legal.report.verified and legal.source is not None
    assert len(tuple(legal.source.graph().objects(EX["row/a"], EX.amount))) == 2


def test_offline_metadata_boundaries_never_invent_mappings_or_fetch(
    monkeypatch: Any,
) -> None:
    """Unknown resources and rules prevent complete extraction before native IO."""

    def denied(*args: object, **kwargs: object) -> object:
        raise AssertionError("CSVW adapter attempted external IO")

    monkeypatch.setattr("urllib.request.urlopen", denied)
    monkeypatch.setattr("socket.socket.connect", denied)
    missing = extract_csvw(description(), {}, base=BASE)
    assert missing.source is None and missing.report.outcome == "unverified"
    for mutation in ("schema", "dialect", "context", "mapping", "ordered", "datatype"):
        value = description()
        column = value["tableSchema"]["columns"][1]
        if mutation == "schema":
            value["tableSchema"] = "https://unavailable.example/schema.json"
        elif mutation == "dialect":
            value["dialect"] = "https://unavailable.example/dialect.json"
        elif mutation == "context":
            value["@context"] = "https://unavailable.example/context"
        elif mutation == "mapping":
            column.pop("propertyUrl")
        elif mutation == "ordered":
            column["ordered"] = True
        else:
            column["datatype"] = "double"
        result = extract_csvw(value, {URL: DATA}, base=BASE)
        assert result.source is None and result.report.outcome == "unverified"
    literal_url = description()
    literal_url["tableSchema"]["columns"][3].pop("separator")
    result = extract_csvw(
        literal_url, {URL: DATA.replace("one|two", "https://plain.example/")}, base=BASE
    )
    assert result.source is not None
    term = next(result.source.graph().objects(EX["row/a"], EX.tag))
    assert isinstance(term, Literal) and str(term) == "https://plain.example/"


def test_dialect_null_default_integer_and_multiline_cells_use_native_metadata() -> None:
    """Keep physical column order and actual multiline coordinates."""
    value = description()
    value["dialect"] = {"delimiter": ";", "header": False}
    columns = value["tableSchema"]["columns"]
    columns[1]["datatype"] = "integer"
    columns[1]["default"] = "01"
    data = 'a;;b;"first\nsecond";true\nb;1.0;a;NA;false\n'
    result = extract_csvw(value, {URL: data}, base=BASE)
    assert result.report.verified and result.source is not None
    numbers = list(result.source.graph().objects(None, EX.amount))
    assert all(isinstance(v, Literal) and v.datatype == XSD.integer for v in numbers)
    assert {str(v) for v in numbers} == {"1"}
    assert result.cells[3].text == "first\nsecond"
    assert result.cells[0].line == 1 and result.cells[5].line == 3
    defaulted = description()
    long_default = "123456789012345678901234567890.123456789"
    defaulted["tableSchema"]["columns"][1].update(
        default=long_default, required=True, null="NA"
    )
    with localcontext() as ambient:
        ambient.prec = 7
        ambient.Emax, ambient.Emin, ambient.clamp = 2, -2, 1
        ambient.traps[InvalidOperation] = False
        ambient.traps[Rounded] = True
        original = str(ambient)
        flags = ambient.flags.copy()
        exact = extract_csvw(
            defaulted, {URL: "id,amount,parent,tags,active\na,,b,NA,true\n"}, base=BASE
        )
        malformed = extract_csvw(
            defaulted,
            {URL: "id,amount,parent,tags,active\na,not-a-number,b,NA,true\n"},
            base=BASE,
        )
        assert malformed.report.outcome == "disagreement"
        assert str(ambient) == original and dict(ambient.flags) == flags
    assert exact.report.verified and exact.source is not None
    assert str(next(exact.source.graph().objects(None, EX.amount))) == long_default
    assert exact.cells[1].text == ""
    large_integer = description()
    large_integer["tableSchema"]["columns"][1]["datatype"] = "integer"
    for subject in (str(EX) + "row/{id}", str(EX) + "row/{amount}"):
        large_integer["tableSchema"]["aboutUrl"] = subject
        unavailable = extract_csvw(
            large_integer,
            {URL: "id,amount,parent,tags,active\na," + "1" * 4400 + ",b,NA,true\n"},
            base=BASE,
            limits=Limits(max_work=2_000_000),
        )
        assert unavailable.source is None and unavailable.report.outcome == "unverified"
        assert all(issue.kind != "input_error" for issue in unavailable.report.issues)
    explicit_null = extract_csvw(
        defaulted, {URL: "id,amount,parent,tags,active\na,NA,b,NA,true\n"}, base=BASE
    )
    assert any(i.code == "csvw.cell_invalid" for i in explicit_null.report.issues)
    required = description()
    required["tableSchema"]["columns"][1]["required"] = True
    absent = extract_csvw(required, {URL: DATA.replace("b,2,a", "b,,a")}, base=BASE)
    assert any(i.code == "csvw.cell_invalid" for i in absent.report.issues)


def test_callback_adoption_and_late_limits_retain_paid_native_findings(
    monkeypatch: Any,
) -> None:
    """A later failed callback cannot erase an earlier paid ordinary finding."""
    calls: list[str] = []

    def finding(source: PreparedSource, budget: Budget) -> Report:
        """Retain an actual selected ordinary data-check result under shared bounds."""
        calls.append("finding")
        model = ModelDeclaration(
            "row",
            (),
            (
                FieldDeclaration(
                    "parent",
                    str(EX.parent),
                    (
                        ValueDeclaration(
                            "iri",
                            constraints=ValueConstraints(enumeration=(EX.absent,)),
                        ),
                    ),
                    output=OutputRequirement(
                        allow_null=True, allow_empty=True, minimum_values=1
                    ),
                ),
            ),
        )
        return check_rdf_emission(
            source, (model,), (RdfModelUsage("row", EX["row/a"]),), budget=budget
        )

    def stop(source: PreparedSource, budget: Budget) -> Report:
        """Use the actual exhausted shared counter after that checked finding."""
        calls.append("stop")
        budget.consume("work", budget.remaining("work") + 1)
        return Report("unreachable", 1)

    result = extract_csvw(
        description(),
        {URL: DATA},
        base=BASE,
        checks=(CsvwCheck("finding", finding), CsvwCheck("stop", stop)),
    )
    assert calls == ["finding", "stop"]
    assert result.source is not None and result.report.outcome == "exhausted"
    assert any(i.kind == "finding" for i in result.report.issues)
    assert len(result.check_reports) == 1
    oversized = description()
    oversized["tableSchema"]["columns"][1]["propertyUrl"] = "x" * 10_000
    bounded = extract_csvw(
        oversized, {URL: DATA}, base=BASE, limits=Limits(max_bytes=500)
    )
    assert bounded.report.outcome == "exhausted" and bounded.source is None
    invalid = DATA.replace(
        "a,12345678901234567890.0123456789,b,one|two,true", "a,bad,b,one|two,bad"
    )
    quota = extract_csvw(
        description(), {URL: invalid}, base=BASE, limits=Limits(max_issues=1)
    )
    assert quota.report.outcome == "exhausted"
    assert sum(i.code == "csvw.cell_invalid" for i in quota.report.issues) == 1
    library = pytest.importorskip("csvw")
    original_read = library.Column.read
    observed: list[str] = []

    def observe(column: Any, value: str, strict: bool = True) -> Any:
        """Observe whether the maintained eager cell parser was actually entered."""
        if column.name == "value":
            observed.append(value)
        return original_read(column, value, strict=strict)

    monkeypatch.setattr(library.Column, "read", observe)
    amplified = {
        "@context": "http://www.w3.org/ns/csvw",
        "url": URL,
        "tableSchema": {
            "aboutUrl": str(EX) + "row/{id}",
            "columns": [
                {"name": "id", "suppressOutput": True},
                {
                    "name": "value",
                    "propertyUrl": str(EX.amount),
                    "datatype": "decimal",
                    "separator": "|",
                    "default": "1" * 10_000,
                },
            ],
        },
    }
    stopped = extract_csvw(
        amplified,
        {URL: "id,value\na,bad\nb," + "|" * 200 + "\n"},
        base=BASE,
        limits=Limits(max_bytes=20_000, max_total_bytes=100_000),
    )
    assert stopped.report.outcome == "exhausted"
    assert observed == ["bad"]
    assert any(i.code == "csvw.cell_invalid" for i in stopped.report.issues)
