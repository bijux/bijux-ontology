"""Project actual native cells through explicit RDF mappings and ordinary checks."""

import threading
from dataclasses import replace
from decimal import Decimal
from typing import cast

from rdflib import XSD, Graph, URIRef
from rdflib import Literal as RdfLiteral

from bijux_ontology.declarations.path_fields import PathFieldDeclaration
from bijux_ontology.declarations.types import OutputRequirement
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, IssueCollector, bound_text, retain_report
from bijux_ontology.models.emission.counts import check_occurrences
from bijux_ontology.models.path_fields.checking import check_path_fields
from bijux_ontology.models.path_fields.types import PathFieldUsage
from bijux_ontology.processes.transport import checkpoint
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.reports import Location, Report

from .profile import UnsupportedPackage, array, object_value, text
from .types import PackageCell, ResourceBinding


def _logical(value: object, budget: Budget) -> str | int | Decimal | bool | None:
    """Reconstruct only native scalar kinds, keeping bool distinct from integer."""
    parts = array(value)
    if len(parts) != 2:
        raise ResourceError("Native package scalar has invalid arity")
    kind, actual = parts
    if kind == "null" and actual is None:
        return None
    if kind == "boolean" and type(actual) is bool:
        return actual
    if type(actual) is str:
        bound_text(actual, budget)
        budget.consume("bytes", 16 * len(actual) + 128)
        if kind == "string":
            return actual
        if kind == "integer":
            return int(actual)
        if kind == "decimal":
            result = Decimal(actual)
            if result.is_finite():
                return result
    raise ResourceError(
        "Native package scalar is outside the supported logical profile"
    )


def _literal(value: str | int | Decimal | bool, budget: Budget) -> RdfLiteral:
    """Bound exact scalar rendering before decimal expansion."""
    if type(value) is bool:
        return RdfLiteral(
            "true" if value else "false", datatype=XSD.boolean, normalize=False
        )
    if type(value) is int:
        rendered = str(value)
        bound_text(rendered, budget)
        return RdfLiteral(rendered, datatype=XSD.integer, normalize=False)
    if isinstance(value, Decimal):
        lexical = str(value)
        budget.consume("bytes", 64 * len(lexical) + 128)
        budget.consume("work", len(lexical) + 1)
        parts = value.as_tuple()
        exponent = parts.exponent
        if type(exponent) is not int:
            raise ResourceError("Nonfinite decimal cannot establish RDF output")
        size = len(parts.digits) + abs(exponent) + 8
        if size > budget.limits.max_bytes:
            raise LimitExceeded("Native decimal expansion exceeds max_bytes")
        budget.consume("bytes", 8 * size)
        budget.consume("work", size)
        return RdfLiteral(format(value, "f"), datatype=XSD.decimal, normalize=False)
    return RdfLiteral(value, datatype=XSD.string, normalize=False)


def bindings(
    bindings: tuple[ResourceBinding, ...],
    entries: tuple[dict[str, object], ...],
    budget: Budget,
) -> dict[str, ResourceBinding]:
    """Qualify selected semantic resource/field scope before native dispatch."""
    if type(bindings) is not tuple or not bindings:
        raise InputError(
            "Package extraction requires a nonempty tuple of ResourceBinding"
        )
    budget.consume("states", len(bindings))
    available = {text(item["name"]): item for item in entries}
    result = {}
    for binding in bindings:
        if not isinstance(binding, ResourceBinding):
            raise InputError("Expected ResourceBinding")
        if binding.resource in result or binding.resource not in available:
            raise InputError("Semantic resource bindings must be supplied and unique")
        bound_text(binding.resource, budget)
        bound_text(binding.subject_column, budget)
        schema = object_value(available[binding.resource]["schema"])
        native_fields = {
            text(object_value(value)["name"]): object_value(value)
            for value in array(schema["fields"])
        }
        if (
            binding.subject_column not in native_fields
            or native_fields[binding.subject_column].get("type", "string") != "string"
        ):
            raise UnsupportedPackage(
                "Semantic subjects require an explicit native string column"
            )
        budget.consume("states", len(binding.columns))
        if len({column.field.predicate for column in binding.columns}) != len(
            binding.columns
        ):
            raise UnsupportedPackage(
                "Selected columns require distinct RDF predicates per row"
            )
        for column in binding.columns:
            bound_text(column.field.name, budget)
            bound_text(column.field.predicate, budget)
            if column.field.name not in native_fields:
                raise InputError(
                    "A bound semantic column was not declared in the native schema"
                )
            if (
                column.mode == "iri"
                and native_fields[column.field.name].get("type", "string") != "string"
            ):
                raise UnsupportedPackage(
                    "IRI output requires native strings containing absolute IRIs"
                )
        result[binding.resource] = binding
    return result


def rows(
    values: object,
    entries: tuple[dict[str, object], ...],
    selected: dict[str, ResourceBinding],
    budget: Budget,
    cells: list[PackageCell],
    checks: list[Report],
    issues: IssueCollector,
    graph: Graph,
    deadline: float,
    cancel: threading.Event | None,
    *,
    check: bool,
) -> int:
    """Check each actual row independently so shared subjects cannot hide omissions."""
    available = {text(item["name"]): item for item in entries}
    checked = 0
    ordinals: dict[str, int] = {}
    for raw in array(values):
        checkpoint(deadline, cancel)
        row = object_value(raw)
        resource, path, ordinal = row["resource"], row["path"], row["row"]
        if (
            type(resource) is not str
            or resource not in available
            or path != available[resource]["path"]
            or type(ordinal) is not int
            or ordinal <= ordinals.get(resource, 1)
        ):
            raise ResourceError("Native row resource/ordinal coverage is invalid")
        ordinals[resource] = ordinal
        native_fields = array(object_value(available[resource]["schema"])["fields"])
        raw_values, values_row = array(row["raw"]), array(row["values"])
        if len(values_row) != len(native_fields) or len(raw_values) != len(
            native_fields
        ):
            if not check:
                continue
            raise ResourceError("Native row did not account for every declared column")
        budget.consume("states", len(native_fields) + 1)
        budget.consume("bytes", 256 * (len(native_fields) + 1))
        logical = [_logical(value, budget) for value in values_row]
        names = [text(object_value(field)["name"]) for field in native_fields]
        binding = selected.get(resource)
        subject = None
        if binding is not None and check:
            value = logical[names.index(binding.subject_column)]
            if type(value) is not str:
                raise UnsupportedPackage(
                    "A selected row lacks an absolute-IRI string subject"
                )
            subject = URIRef(absolute_iri(value))
        row_graph = Graph(bind_namespaces="none")
        usages = []
        columns = (
            {column.field.name: column for column in binding.columns}
            if binding is not None
            else {}
        )
        for position, (name, original, value) in enumerate(
            zip(names, raw_values, logical, strict=True), 1
        ):
            if original is not None and type(original) is not str:
                raise ResourceError("CSV native raw cell is not original text/null")
            if type(original) is str:
                bound_text(original, budget)
            statements: tuple[tuple[URIRef, URIRef, URIRef | RdfLiteral], ...] = ()
            column = columns.get(name)
            if subject is not None and column is not None:
                field = column.field
                predicate = URIRef(field.predicate)
                if value is not None:
                    if column.mode == "iri":
                        if type(value) is not str:
                            raise UnsupportedPackage("IRI cells require native strings")
                        target: URIRef | RdfLiteral = URIRef(absolute_iri(value))
                    else:
                        target = _literal(value, budget)
                    budget.consume("triples")
                    statements = ((subject, predicate, target),)
                    row_graph.add(statements[0])
                    graph.add(statements[0])
                output = field.output or OutputRequirement(
                    allow_null=True, allow_empty=True
                )
                if value is None:
                    where = Location(source=cast(str, path), path=(ordinal, name))
                    issues.extend(
                        replace(issue, details=where)
                        for issue in check_occurrences(
                            output,
                            (),
                            where=str(where),
                            budget=budget,
                            native_literals={},
                            present=True,
                            raw=None,
                        )
                    )
                endpoint_output = replace(
                    output, require_property=False, allow_null=True, allow_empty=True
                )
                if value is not None:
                    usages.append(
                        PathFieldUsage(
                            PathFieldDeclaration(
                                name, predicate, field.values, output=endpoint_output
                            ),
                            subject,
                            location=Location(
                                source=cast(str, path), path=(ordinal, name)
                            ),
                        )
                    )
            cells.append(
                PackageCell(
                    resource,
                    cast(str, path),
                    ordinal,
                    position,
                    name,
                    original,
                    value,
                    statements,
                )
            )
        if subject is not None:
            if usages:
                candidate = prepare_rdf(row_graph, budget=budget)
                report = check_path_fields(candidate, tuple(usages), budget=budget)
            else:
                report = Report(
                    "frictionless-null-row", 1, regime="native-null-no-emission"
                )
            try:
                retain_report(report, budget, issues)
            finally:
                issues.retain_completed(report)
            checks.append(report)
            checked += 1
    return checked
