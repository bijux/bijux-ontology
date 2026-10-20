"""Finite native scalar interpretation and ordinary compiled RDF model checks."""

import math
from typing import Any

from rdflib import BNode, Dataset, Literal, URIRef
from rdflib.namespace import RDF, XSD
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, IssueCollector, bound_text
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.sources.snapshots import PreparedSource

from .types import ArrowCell, ArrowColumn, ArrowContract, ArrowRowResult


def schema_indices(
    native: Any, schema: Any, contract: ArrowContract, operation: Budget
) -> tuple[int, ...]:
    """Resolve only owned column names, without copying unknown source metadata."""
    count = len(schema)
    operation.consume("states", 8 + 4 * count)
    operation.consume("work", 8 + 4 * count)
    operation.consume("bytes", 512 + 256 * count)
    if not schema.equals(schema.remove_metadata(), check_metadata=True):
        raise ResourceError("Unsupported required Arrow schema metadata")
    for index in range(count):
        field = schema.field(index)
        if not field.equals(field.with_metadata(None), check_metadata=True):
            raise ResourceError("Unsupported required Arrow field/extension metadata")
        if isinstance(field.type, native.BaseExtensionType):
            raise ResourceError("Arrow extension types require an explicit evaluator")
    names = (
        contract.subject_column,
        *(column.column for column in contract.columns),
        *contract.ignored_columns,
    )
    if len(names) != count:
        raise ResourceError("Every physical column requires an explicit binding/ignore")
    indices = tuple(schema.get_field_index(name) for name in names)
    if any(index < 0 for index in indices) or len(set(indices)) != count:
        raise ResourceError("Arrow column names are missing or ambiguous")
    subject_type = schema.field(indices[0]).type
    if not (
        native.types.is_string(subject_type)
        or native.types.is_large_string(subject_type)
    ):
        raise ResourceError("Arrow subjects require ordinary string IRI columns")
    for column, index in zip(contract.columns, indices[1:], strict=False):
        kind = schema.field(index).type
        if column.list:
            if not native.types.is_list(kind):
                raise ResourceError("List bindings require one-dimensional Arrow lists")
            child = kind.value_field
            if not child.equals(child.with_metadata(None), check_metadata=True):
                raise ResourceError("Unsupported required Arrow list-child metadata")
            kind = kind.value_type
        elif native.types.is_list(kind):
            raise ResourceError("An Arrow list requires explicit RDF list policy")
        if native.types.is_dictionary(kind):
            kind = kind.value_type
            if not (native.types.is_string(kind) or native.types.is_large_string(kind)):
                raise ResourceError("Only string dictionary values are qualified")
        expected = column.datatype
        if native.types.is_string(kind) or native.types.is_large_string(kind):
            supported = column.kind == "iri" or expected == XSD.string
        elif native.types.is_integer(kind):
            supported = expected == XSD.integer
        elif native.types.is_boolean(kind):
            supported = expected == XSD.boolean
        elif native.types.is_floating(kind):
            supported = kind.bit_width in (32, 64) and expected == XSD.double
        elif native.types.is_decimal128(kind):
            supported = expected == XSD.decimal and -38 <= kind.scale <= 38
        elif native.types.is_timestamp(kind):
            supported = expected == XSD.dateTime and kind.unit in ("s", "ms", "us")
            if supported:
                supported = kind.equals(native.timestamp(kind.unit, tz=column.timezone))
        else:
            supported = False
        if not supported:
            raise ResourceError(
                "Arrow scalar type and explicit RDF mapping are unsupported"
            )
    return indices[: 1 + len(contract.columns)]


def _term(
    native: Any, scalar: Any, column: ArrowColumn, operation: Budget
) -> tuple[Node, bool]:
    """Decode the actual dictionary value and preserve exact supported scalar value."""
    if native.types.is_dictionary(scalar.type):
        scalar = scalar.value
    if not scalar.is_valid:
        raise ResourceError(
            "A null dictionary/list value has no qualified RDF endpoint"
        )
    try:
        value = scalar.as_py()
    except Exception as error:
        raise ResourceError("Native Arrow scalar decoding did not complete") from error
    nan = type(value) is float and math.isnan(value)
    if column.kind == "iri":
        if type(value) is not str:
            raise ResourceError("Actual IRI column value is not a string")
        bound_text(value, operation)
        absolute_iri(value)
        return URIRef(value), False
    if column.datatype == XSD.string:
        text = value
    elif column.datatype == XSD.integer:
        text = str(value)
    elif column.datatype == XSD.boolean:
        text = "true" if value else "false"
    elif column.datatype == XSD.decimal:
        text = format(value, "f")
    elif column.datatype == XSD.dateTime:
        text = value.isoformat(timespec="microseconds")
    elif column.datatype == XSD.double:
        text = (
            "NaN"
            if nan
            else "INF"
            if value == math.inf
            else "-INF"
            if value == -math.inf
            else repr(value)
        )
    else:
        raise ResourceError("No qualified Arrow literal decoder")
    bound_text(text, operation)
    return Literal(text, datatype=column.datatype, normalize=False), nan


def convert_row(
    native: Any,
    batch: Any,
    row: int,
    index: int,
    indices: tuple[int, ...],
    contract: ArrowContract,
    dataset: Dataset,
    operation: Budget,
    issues: IssueCollector,
) -> tuple[URIRef, tuple[ArrowCell, ...], Report]:
    """Convert one actual row without JSON arrays or dictionary-code substitution."""
    location = f"arrow/batch/{index}/row/{row}"
    start = len(issues)
    scalar = batch.column(indices[0])[row]
    if not scalar.is_valid:
        raise ResourceError("A selected Arrow row lacks its required subject")
    subject_text = scalar.as_py()
    bound_text(subject_text, operation)
    absolute_iri(subject_text)
    subject = URIRef(subject_text)
    graph = (
        dataset.default_context
        if contract.graph_name == "@default"
        else dataset.graph(URIRef(contract.graph_name))
    )
    for kind in contract.types:
        operation.consume("triples")
        graph.add((subject, RDF.type, kind))
    cells = []
    for column, ordinal in zip(contract.columns, indices[1:], strict=True):
        operation.consume("work")
        scalar = batch.column(ordinal)[row]
        effective = (
            scalar.value
            if native.types.is_dictionary(scalar.type) and scalar.is_valid
            else scalar
        )
        null = not effective.is_valid
        nan = False
        terms: list[Node] = []
        if null:
            if column.null == "reject":
                issues.append(
                    Issue(
                        "arrow.null_rejected",
                        "The explicit column policy rejects source null",
                        location + "/column/" + column.column,
                    )
                )
        else:
            values = scalar.values if column.list else (scalar,)
            for value in values:
                operation.consume("states", 2)
                operation.consume("bytes", 256)
                term, is_nan = _term(native, value, column, operation)
                nan |= is_nan
                if is_nan and column.nan == "reject":
                    issues.append(
                        Issue(
                            "arrow.nan_rejected",
                            "The explicit column policy rejects NaN",
                            location + "/column/" + column.column,
                        )
                    )
                terms.append(term)
            if column.list:
                operation.consume("states", len(terms))
                operation.consume("bytes", 128 * len(terms))
                nodes = [BNode() for _ in terms]
                operation.consume("triples", 1 + 2 * len(terms))
                graph.add((subject, column.predicate, nodes[0] if nodes else RDF.nil))
                for offset, term in enumerate(terms):
                    graph.add((nodes[offset], RDF.first, term))
                    graph.add(
                        (
                            nodes[offset],
                            RDF.rest,
                            nodes[offset + 1] if offset + 1 < len(nodes) else RDF.nil,
                        )
                    )
            else:
                operation.consume("triples")
                graph.add((subject, column.predicate, terms[0]))
        operation.consume("states")
        operation.consume("bytes", 128)
        cells.append(
            ArrowCell(
                column.column,
                column.field,
                scalar,
                scalar.type,
                null,
                nan,
                tuple(terms),
            )
        )
    return (
        subject,
        tuple(cells),
        Report("arrow.source_row", 1, tuple(issues[start:]), requested=1),
    )


def check_row(
    source: PreparedSource,
    subject: URIRef,
    row: int,
    index: int,
    cells: tuple[ArrowCell, ...],
    raw: Report,
    contract: ArrowContract,
    operation: Budget,
) -> ArrowRowResult:
    """Use the same public compiled model engine and shared semantic allowance."""
    report = contract.models.check_rdf(
        source,
        (
            RdfModelUsage(
                contract.model, subject, location=f"arrow/batch/{index}/row/{row}"
            ),
        ),
        graph_name=contract.graph_name,
        budget=operation,
    )
    combined = Report(
        "arrow.row",
        1,
        requested=1,
        children=(raw, report),
        complete=raw.complete and report.complete,
    )
    return ArrowRowResult(row, subject, cells, combined)
