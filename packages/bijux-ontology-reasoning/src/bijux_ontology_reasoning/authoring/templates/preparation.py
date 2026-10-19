"""Finite native-template fields and exact row-token qualification."""

import csv
import io
import re
from dataclasses import dataclass
from typing import cast

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import IssueCollector
from bijux_ontology.reports import Issue, Report

from ...axioms.codec.encoding import ConstructionEncoder
from ...axioms.types import (
    AnnotationAssertion,
    Declaration,
    EntityKind,
    IriValue,
    NamedEntity,
    NativeLiteral,
    ObjectProperty,
    ObjectQuantifier,
    ProjectedAxiom,
    SubClassOf,
)
from ...documents.sources.capture import capture_document
from ...documents.types import OwlDocument
from .types import TableRowReceipt, TemplateColumn, TemplateIdentifier, TemplateTerm

LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
STRING = "http://www.w3.org/2001/XMLSchema#string"


@dataclass(frozen=True)
class RequestedRow:
    """One complete requested row, independently of native execution success."""

    row: int
    iri: str
    axioms: tuple[ProjectedAxiom, ...]


@dataclass(frozen=True)
class PreparedTable:
    """Owned original table, explicit bindings and complete finite row inventory."""

    content: bytes
    columns: tuple[TemplateColumn, ...]
    bindings: OwlDocument
    terms: tuple[TemplateTerm, ...]
    prefixes: tuple[tuple[str, str], ...]
    ontology_iri: str
    rows: tuple[RequestedRow, ...]
    suffix: str


def _column_kind(
    column: TemplateColumn, terms: dict[str, NamedEntity]
) -> tuple[str, NamedEntity | None]:
    """Select only documented class templates; native code performs substitution."""
    template = column.template
    if template in ("ID", "LABEL", "TYPE", "SC %"):
        return template, None
    if template.startswith("SC ") and template.endswith(" some %"):
        token = template[3:-7]
        entity = terms.get(token)
        if (
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]*", token)
            and entity is not None
            and entity.kind == "object_property"
        ):
            return "some", entity
    if template.startswith("A "):
        entity = terms.get(template[2:])
        if (
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]*", template[2:])
            and entity is not None
            and entity.kind == "annotation_property"
        ):
            return "annotation", entity
    raise InputError(
        "Unsupported or unbound native template instruction",
        location="table[2]." + column.header,
    )


def _row_axioms(
    row: list[str],
    number: int,
    columns: tuple[TemplateColumn, ...],
    kinds: tuple[tuple[str, NamedEntity | None], ...],
    identifiers: dict[str, str],
    terms: dict[str, NamedEntity],
    seen: set[str],
    operation: Operation,
) -> tuple[str, tuple[ProjectedAxiom, ...]]:
    """Construct typed requested witnesses, never native syntax or expressions."""
    if len(row) != len(columns):
        raise InputError(
            "Row width differs from the declared columns", location=f"table[{number}]"
        )
    for column, value in zip(columns, row, strict=True):
        if column.required and not value:
            raise InputError(
                "A required template cell is empty",
                location=f"table[{number}].{column.header}",
            )
    token = row[next(index for index, pair in enumerate(kinds) if pair[0] == "ID")]
    iri = identifiers.get(token)
    if iri is None:
        raise InputError(
            "Row ID has no explicit IRI binding", location=f"table[{number}].ID"
        )
    if iri in seen:
        raise InputError(
            "Repeated row identifier or aliased duplicate IRI",
            location=f"table[{number}].ID",
        )
    seen.add(iri)
    operation.consume("bytes", 256 + 512 * len(columns))
    operation.consume("states", 2 * len(columns) + 2)
    defined = NamedEntity("class", iri)
    axioms: list[ProjectedAxiom] = [Declaration(defined, ())]
    for column, value, (kind, entity) in zip(columns, row, kinds, strict=True):
        operation.consume("work")
        if not value or kind == "ID":
            continue
        location = f"table[{number}].{column.header}"
        if kind == "TYPE":
            if value not in ("class", "owl:Class"):
                raise InputError(
                    "Only ordinary class rows are supported", location=location
                )
        elif kind in ("LABEL", "annotation"):
            property_iri = (
                LABEL if kind == "LABEL" else entity.iri if entity is not None else ""
            )
            axioms.append(
                AnnotationAssertion(
                    property_iri, IriValue(iri), NativeLiteral(value, STRING, None), ()
                )
            )
        else:
            filler = terms.get(value)
            if filler is None or filler.kind != "class":
                raise InputError(
                    "Logical cell has no explicit local class binding",
                    location=location,
                )
            expression = (
                filler
                if kind == "SC %"
                else ObjectQuantifier("some", ObjectProperty(entity.iri), filler)
                if entity is not None
                else filler
            )
            axioms.append(SubClassOf(defined, expression, ()))
    return iri, tuple(axioms)


def prepare_table(
    table: bytes | str,
    delimiter: str,
    columns: tuple[TemplateColumn, ...],
    bindings: OwlDocument,
    identifiers: tuple[TemplateIdentifier, ...],
    terms: tuple[TemplateTerm, ...],
    prefixes: tuple[tuple[str, str], ...],
    ontology_iri: str,
    operation: Operation,
    receipts: list[TableRowReceipt],
    issues: IssueCollector,
) -> PreparedTable:
    """Own original bytes and prepay CSV workspace before the maintained parser."""
    encoder = ConstructionEncoder(operation)
    if type(delimiter) is not str:
        raise InputError("Choose csv or tsv")
    encoder.text(delimiter)
    if delimiter not in ("csv", "tsv"):
        raise InputError("Choose csv or tsv")
    ontology_iri = encoder.text(ontology_iri, iri=True)
    original = capture_document(bindings, operation)
    if original.format != "functional":
        raise InputError(
            "Template bindings initially require original Functional Syntax"
        )
    captured_terms: list[TemplateTerm] = []
    term_map: dict[str, NamedEntity] = {}
    for raw in encoder.sequence(terms):
        if type(raw) is not TemplateTerm or type(raw.entity) is not NamedEntity:
            raise InputError("Terms require ordinary explicit native role bindings")
        token = encoder.text(raw.token)
        role = encoder.text(raw.entity.kind)
        if role not in ("class", "object_property", "annotation_property"):
            raise InputError("Term bindings require a supported native role")
        entity = NamedEntity(
            cast(EntityKind, role), encoder.text(raw.entity.iri, iri=True)
        )
        if (
            not token
            or token in term_map
            or entity.kind not in ("class", "object_property", "annotation_property")
        ):
            raise InputError("Term bindings require unique tokens and supported roles")
        operation.consume("bytes", 256)
        term_map[token] = entity
        captured_terms.append(TemplateTerm(token, entity))
    ids: dict[str, str] = {}
    id_values: set[str] = set()
    for raw_id in encoder.sequence(identifiers):
        if type(raw_id) is not TemplateIdentifier:
            raise InputError("Identifiers require ordinary explicit row bindings")
        token, iri = encoder.text(raw_id.token), encoder.text(raw_id.iri, iri=True)
        if not token or token in ids or iri in id_values or iri == ontology_iri:
            raise InputError("Generated class bindings must be unique")
        operation.consume("bytes", 256)
        ids[token] = iri
        id_values.add(iri)
    captured_columns: list[TemplateColumn] = []
    names: set[str] = set()
    kinds: list[tuple[str, NamedEntity | None]] = []
    for raw_column in encoder.sequence(columns):
        if (
            type(raw_column) is not TemplateColumn
            or type(raw_column.required) is not bool
        ):
            raise InputError(
                "Columns require typed native instructions and required flags"
            )
        header = encoder.text(raw_column.header)
        template = encoder.text(raw_column.template)
        if not header or header in names or any(char in header for char in "\r\n"):
            raise InputError(
                "Column headers must be unique nonempty single-line text",
                location="table[1]",
            )
        operation.consume("bytes", 256)
        owned = TemplateColumn(header, template, raw_column.required)
        kinds.append(_column_kind(owned, term_map))
        names.add(header)
        captured_columns.append(owned)
    captured = tuple(captured_columns)
    kind_values = tuple(kinds)
    if sum(kind == "ID" for kind, _ in kinds) != 1 or any(
        sum(kind == selected for kind, _ in kinds) > 1 for selected in ("LABEL", "TYPE")
    ):
        raise InputError(
            "Choose exactly one ID and at most one LABEL/TYPE column",
            location="table[2]",
        )
    prefix_values: list[tuple[str, str]] = []
    prefix_names: set[str] = set()
    for pair in encoder.sequence(prefixes):
        values = encoder.sequence(pair)
        if len(values) != 2:
            raise InputError("Prefixes require explicit name/namespace pairs")
        prefix = encoder.text(values[0]) if type(values[0]) is str else ""
        namespace = encoder.text(values[1], iri=True) if type(values[1]) is str else ""
        if (
            not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", prefix)
            or not namespace
            or prefix in prefix_names
        ):
            raise InputError("Prefix names must be explicit and unique")
        prefix_names.add(prefix)
        prefix_values.append((prefix, namespace))
    if type(table) is str:
        encoder.text(table)
        operation.consume("bytes", 4 * len(table))
        content = table.encode("utf-8")
    elif type(table) is bytes:
        content = table
    else:
        raise InputError("Templates require ordinary supplied UTF-8 bytes or text")
    if len(content) > operation.limits.max_bytes:
        raise LimitExceeded("Template table exceeds max_bytes")
    operation.consume("bytes", 8 * len(content))
    operation.consume("work", 4 * len(content))
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InputError("Template table is not UTF-8") from error
    separator = "," if delimiter == "csv" else "\t"
    capacity = text.count(separator) + text.count("\n") + 1
    operation.consume("states", capacity)
    operation.consume("bytes", 256 * capacity)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=separator, strict=True)
    try:
        for number, expected in (
            (1, [column.header for column in captured]),
            (2, [column.template for column in captured]),
        ):
            if next(reader, None) != expected:
                raise InputError(
                    "Table headers/instructions differ from the declared pattern",
                    location=f"table[{number}]",
                )
        requested: list[RequestedRow] = []
        seen: set[str] = set()
        for number, row in enumerate(reader, 3):
            operation.consume("bytes", 1024)
            operation.consume("states", 4)
            row_iri: str | None = None
            local: tuple[Issue, ...] = ()
            try:
                row_iri, axioms = _row_axioms(
                    row, number, captured, kind_values, ids, term_map, seen, operation
                )
                requested.append(RequestedRow(number, row_iri, axioms))
            except LimitExceeded:
                raise
            except InputError as error:
                issue = Issue(
                    "templates.row_input_error",
                    str(error),
                    error.location or f"table[{number}]",
                    kind="input_error",
                )
                local = (issue,)
                issues.append(issue)
                receipts.append(
                    TableRowReceipt(
                        number,
                        row_iri,
                        Report(
                            "templates.row_input",
                            issues=local,
                            requested=1,
                            complete=False,
                        ),
                    )
                )
                continue
            receipts.append(
                TableRowReceipt(
                    number, row_iri, Report("templates.row_input", checked=1)
                )
            )
        if not receipts:
            raise InputError(
                "Template table has no required data rows", location="table[3]"
            )
        if id_values != seen:
            raise InputError(
                "The supplied identifier inventory is not fully represented by rows",
                location="table",
            )
    except csv.Error as error:
        if str(error).startswith("field larger than field limit"):
            raise LimitExceeded(
                "CSV parser field allowance exceeded",
                location=f"table[line {reader.line_num}]",
            ) from error
        raise InputError(
            "Malformed CSV/TSV record", location=f"table[line {reader.line_num}]"
        ) from error
    return PreparedTable(
        content,
        captured,
        original,
        tuple(captured_terms),
        tuple(prefix_values),
        ontology_iri,
        tuple(requested),
        delimiter,
    )
