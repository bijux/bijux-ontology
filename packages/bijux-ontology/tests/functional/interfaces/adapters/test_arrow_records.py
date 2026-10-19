"""Real native columnar batches preserve explicit RDF and source scalar meaning."""

import gc
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from threading import Event
from typing import Any

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.declarations.value_constraints import ValueConstraints
from bijux_ontology.errors import ResourceError
from bijux_ontology.interfaces.adapters.arrow import (
    ArrowColumn,
    ArrowContract,
    open_arrow_records,
    prepare_arrow_contract,
)
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.models.preparation import PreparedModels
from bijux_ontology.rdf.structure import read_rdf_list
from bijux_ontology.rdf.terms import term_key

G = URIRef("urn:graph:records")
NODE = URIRef("urn:example:Node")
KIND = URIRef("urn:example:kind")
A, B = URIRef("urn:kind:a"), URIRef("urn:kind:b")


def _plan(
    *, nan: str = "preserve", null: str = "omit", limits: Limits | None = None
) -> ArrowContract:
    vocabulary = Graph().add((NODE, RDF.type, OWL.Class))
    fields = []
    columns = []
    for name, datatype in (
        ("kind", None),
        ("amount", XSD.decimal),
        ("stamp", XSD.dateTime),
        ("optional", XSD.double),
        ("items", XSD.string),
    ):
        predicate = URIRef("urn:example:" + name)
        vocabulary.add(
            (
                predicate,
                RDF.type,
                OWL.ObjectProperty
                if datatype is None or name == "items"
                else OWL.DatatypeProperty,
            )
        )
        vocabulary.add((predicate, RDFS.domain, NODE))
        if datatype is not None:
            vocabulary.add(
                (predicate, RDFS.range, RDF.List if name == "items" else datatype)
            )
        branch = ValueDeclaration(
            "iri" if datatype is None else "literal",
            datatype=str(datatype) if datatype else None,
            container="@list" if name == "items" else None,
            constraints=ValueConstraints(enumeration=(A, B))
            if name == "kind"
            else None,
        )
        fields.append(
            FieldDeclaration(
                name,
                str(predicate),
                (branch,),
                emission="optional" if name in ("items", "optional") else "guaranteed",
            )
        )
        columns.append(
            ArrowColumn(
                name,
                name,
                predicate,
                "iri" if datatype is None else "literal",
                datatype,
                list=name == "items",
                null="reject" if null == "reject" else "omit",
                nan="reject" if nan == "reject" else "preserve",
                timezone="America/New_York" if name == "stamp" else None,
            )
        )
    model = ModelDeclaration("record", (str(NODE),), tuple(fields))
    compiled = PreparedModels((model,), vocabulary, limits=limits)
    assert compiled.declaration_report.verified, compiled.declaration_report.to_dict()
    return prepare_arrow_contract(
        compiled,
        model="record",
        subject_column="subject",
        columns=columns,
        types=(NODE,),
        graph_name=G,
    )


def _batches(pa: Any, *, bad: bool = False) -> tuple[Any, Any]:
    schema = pa.schema(
        [
            ("subject", pa.string()),
            ("kind", pa.dictionary(pa.int8(), pa.string())),
            ("amount", pa.decimal128(30, 12)),
            ("stamp", pa.timestamp("us", tz="America/New_York")),
            ("optional", pa.float64()),
            ("items", pa.list_(pa.string())),
        ]
    )
    batches = []
    for index in (0, 1):
        dictionary = [str(A), str(B)] if index == 0 else [str(B), str(A)]
        if bad and index == 1:
            dictionary[1] = "urn:kind:outside"
        codes = [0, 1] if index == 0 else [1, 0]
        batches.append(
            pa.record_batch(
                [
                    pa.array([f"urn:record:{index}:0", f"urn:record:{index}:1"]),
                    pa.DictionaryArray.from_arrays(
                        pa.array(codes, type=pa.int8()), pa.array(dictionary)
                    ),
                    pa.array(
                        [
                            Decimal("123456789012345678.123456789012"),
                            Decimal("0.000000000001"),
                        ],
                        type=schema.field("amount").type,
                    ),
                    pa.array(
                        [
                            datetime(2026, 7, 1, tzinfo=UTC),
                            datetime(2026, 1, 1, tzinfo=UTC),
                        ],
                        type=schema.field("stamp").type,
                    ),
                    pa.array([None, float("nan")]),
                    pa.array([["a", "b"], []], type=pa.list_(pa.string())),
                ],
                schema=schema,
            )
        )
    return batches[0], batches[1]


def test_real_two_batches_match_rdf_oracle_and_locate_second_batch_bad_term() -> None:
    pa = pytest.importorskip("pyarrow")
    plan = _plan()
    first, second = _batches(pa)
    with open_arrow_records(
        pa.RecordBatchReader.from_batches(first.schema, (first, second)),
        plan,
        emit_rdf=True,
    ) as stream:
        results = tuple(stream)
    assert stream.report.verified and stream.closed
    assert len(results) == 2 and all(result.report.verified for result in results)
    for index, result in enumerate(results):
        assert result.output is not None
        graph = result.output.graph(G)
        oracle = Graph()
        for row, controlled in enumerate((A, B)):
            subject = URIRef(f"urn:record:{index}:{row}")
            oracle.add((subject, KIND, controlled))
            assert (subject, KIND, controlled) in graph
        assert set(graph.triples((None, KIND, None))) == set(oracle)
        amount = next(
            graph.objects(URIRef(f"urn:record:{index}:0"), URIRef("urn:example:amount"))
        )
        assert str(amount) == "123456789012345678.123456789012"
        stamp = next(
            graph.objects(URIRef(f"urn:record:{index}:0"), URIRef("urn:example:stamp"))
        )
        assert str(stamp) == "2026-06-30T20:00:00.000000-04:00"
        head = next(
            graph.objects(URIRef(f"urn:record:{index}:0"), URIRef("urn:example:items"))
        )
        listed = read_rdf_list(graph, head)
        assert tuple(term_key(n) for n in listed) == tuple(
            term_key(Literal(v)) for v in ("a", "b")
        )
        assert result.rows[0].cells[3].null and result.rows[1].cells[3].nan
    first, second = _batches(pa, bad=True)
    with open_arrow_records(
        pa.RecordBatchReader.from_batches(first.schema, (first, second)), plan
    ) as stream:
        bad_results = tuple(stream)
    assert bad_results[0].report.verified and not bad_results[1].report.verified
    assert all(result.output is None for result in bad_results)
    assert any(
        "arrow/batch/1/row/0" in issue.location
        for issue in bad_results[1].rows[0].report.issues
    )


def test_declared_null_nan_and_no_guessed_output_types() -> None:
    pa = pytest.importorskip("pyarrow")
    first, _ = _batches(pa)
    with open_arrow_records(first, _plan(nan="reject", null="reject")) as stream:
        result = next(stream)
    codes = {issue.code for issue in result.report.issues}
    assert {"arrow.null_rejected", "arrow.nan_rejected"} <= codes
    plan = _plan()
    without_types = prepare_arrow_contract(
        plan.models,
        model=plan.model,
        subject_column=plan.subject_column,
        columns=plan.columns,
        graph_name=G,
    )
    with open_arrow_records(first, without_types) as stream:
        result = next(stream)
    assert not result.report.verified


def test_schema_extension_and_actual_generator_failure_keep_work_incomplete() -> None:
    pa = pytest.importorskip("pyarrow")
    first, second = _batches(pa)
    changed = pa.record_batch([pa.array([1])], names=["subject"])
    with open_arrow_records(
        pa.RecordBatchReader.from_batches(first.schema, (first, changed)), _plan()
    ) as stream:
        results = tuple(stream)
    assert results[0].report.verified and results[1].report.outcome == "unverified"
    assert not stream.report.complete
    metadata = first.replace_schema_metadata(
        {b"ARROW:extension:name": b"unimplemented"}
    )
    with pytest.raises(ResourceError, match="metadata"):
        open_arrow_records(metadata, _plan())

    extension = first.set_column(
        4,
        pa.field("optional", pa.json_()),
        pa.array(["{}", "{}"], type=pa.json_()),
    )
    with pytest.raises(ResourceError, match="extension"):
        open_arrow_records(extension, _plan())

    child_type = pa.list_(
        pa.field(
            "item",
            pa.string(),
            metadata={
                b"ARROW:extension:name": b"unimplemented-required",
            },
        )
    )
    child_metadata = first.set_column(
        5,
        pa.field("items", child_type),
        pa.array([["a", "b"], []], type=child_type),
    )
    with pytest.raises(ResourceError, match="list-child metadata"):
        open_arrow_records(child_metadata, _plan())

    def failing() -> Iterator[Any]:
        yield first
        raise ValueError("real native producer stopped")

    with open_arrow_records(
        pa.RecordBatchReader.from_batches(first.schema, failing()), _plan()
    ) as stream:
        results = tuple(stream)
    assert results[0].report.verified and results[1].report.outcome == "unverified"
    assert not stream.report.complete
    assert second.num_rows == 2


def test_c_stream_survives_original_wrapper_release_and_closes_reader() -> None:
    pa = pytest.importorskip("pyarrow")
    first, second = _batches(pa)
    table = pa.Table.from_batches((first, second))
    stream = open_arrow_records(table, _plan())
    del table, first, second
    gc.collect()
    with stream:
        assert len(tuple(stream)) == 2
    assert stream.report.verified and stream.closed
    first, _ = _batches(pa)
    reader = pa.RecordBatchReader.from_batches(first.schema, (first,))
    with open_arrow_records(reader, _plan()) as stream:
        next(stream)
    with pytest.raises((pa.ArrowInvalid, StopIteration)):
        reader.read_next_batch()
    assert not stream.report.complete
    base = _plan(null="reject")
    caller_columns = tuple(replace(column) for column in base.columns)
    owned = prepare_arrow_contract(
        base.models,
        model=base.model,
        subject_column=base.subject_column,
        columns=caller_columns,
        types=base.types,
        graph_name=base.graph_name,
    )
    object.__setattr__(caller_columns[3], "null", "omit")
    assert owned.columns[3] is not caller_columns[3]
    with open_arrow_records(first, owned) as stream:
        unchanged = next(stream)
    assert any(issue.code == "arrow.null_rejected" for issue in unchanged.report.issues)


def test_cumulative_bounds_cancel_and_unknown_list_elements() -> None:
    pa = pytest.importorskip("pyarrow")
    first, second = _batches(pa)
    plan = _plan()
    with open_arrow_records(
        first, _plan(limits=replace(Limits(), max_work=500000))
    ) as custom:
        assert next(custom).report.verified
    event = Event()
    stream = open_arrow_records(
        pa.RecordBatchReader.from_batches(first.schema, (first, second)),
        plan,
        cancel=event,
    )
    assert next(stream).report.verified
    event.set()
    stopped = next(stream)
    assert stopped.report.outcome == "exhausted" and stream.closed
    assert not stream.report.complete
    with open_arrow_records(
        pa.RecordBatchReader.from_batches(first.schema, (first, second)),
        plan,
        max_rows=3,
    ) as stream:
        results = tuple(stream)
    assert results[0].report.verified and results[1].report.outcome == "exhausted"
    altered = first.set_column(
        5, first.schema.field(5), pa.array([[None], []], type=pa.list_(pa.string()))
    )
    with open_arrow_records(altered, plan) as stream:
        result = next(stream)
    assert result.report.outcome == "unverified"


def test_paid_finding_survives_late_source_failure_and_shared_allowance() -> None:
    pa = pytest.importorskip("pyarrow")
    first, second = _batches(pa, bad=True)
    plan = _plan()
    budget = Budget()
    with open_arrow_records(
        pa.RecordBatchReader.from_batches(first.schema, (second,)), plan, budget=budget
    ) as stream:
        result = next(stream)
        findings = tuple(
            issue for issue in result.report.issues if issue.kind == "finding"
        )
        assert findings and budget._counts.get("work", 0) > 0
        budget.consume("work", budget.remaining("work"))
        stopped = next(stream)
    assert stopped.report.outcome == "exhausted"
    assert all(
        any(issue is retained for retained in stream.report.issues)
        for issue in findings
    )
    malformed_list = first.set_column(
        5, first.schema.field(5), pa.array([[None], []], type=pa.list_(pa.string()))
    )
    with open_arrow_records(malformed_list, _plan(null="reject")) as stream:
        result = next(stream)
    assert not result.report.complete
    assert any(issue.kind == "unverified" for issue in result.report.issues)
    assert any(issue.code == "arrow.null_rejected" for issue in result.report.issues)
