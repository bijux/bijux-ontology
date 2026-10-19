"""Lazy native capability and explicit model/column contract qualification."""

import importlib
import sys
from collections.abc import Sequence
from typing import Any

from rdflib import URIRef
from rdflib.namespace import XSD

from bijux_ontology.diagnostics.errors import InputError, ResourceError
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget, Limits, bound_text
from bijux_ontology.models.contracts.preparation import PreparedModels
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.terms import bound_node

from .types import ArrowColumn, ArrowContract


def backend() -> Any:
    """Load the optional maintained implementation only for an Arrow operation."""
    try:
        importlib.import_module("pyarrow")
    except ImportError as error:
        raise ResourceError(
            "Install bijux-ontology[arrow] for Arrow records"
        ) from error
    native = sys.modules["pyarrow"]
    if native.__version__ != "26.0.0":
        raise ResourceError("Arrow records require qualified PyArrow 26.0.0")
    return native


def prepare_arrow_contract(
    models: PreparedModels,
    *,
    model: str,
    subject_column: str,
    columns: Sequence[ArrowColumn],
    types: Sequence[URIRef] = (),
    graph_name: URIRef | str = "@default",
    ignored_columns: Sequence[str] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ArrowContract:
    """Own explicit output bindings without requiring PyArrow in the core install."""
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Arrow contract preparation requires a Budget")
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("Arrow contract preparation requires Limits")
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("Conflicting Arrow preparation limits")
    operation = budget if budget is not None else Budget(limits)
    if type(models) is not PreparedModels:
        raise InputError("Arrow contracts require compiled PreparedModels")
    for text in (model, subject_column):
        if type(text) is not str or not text:
            raise InputError("Model and subject column require nonempty ordinary text")
        bound_text(text, operation)
    operation.consume("states", len(models.models))
    operation.consume("work", len(models.models))
    operation.consume("bytes", 64 + 16 * len(models.models))
    selected = next((item for item in models.models if item.identifier == model), None)
    if selected is None:
        raise InputError("Arrow model is absent from the compiled model registry")
    captured = capture_items(columns, ArrowColumn, operation)
    operation.consume("states", 2 * len(captured))
    operation.consume("bytes", 512 * len(captured))
    copies = []
    for column in captured:
        if type(column) is not ArrowColumn:
            raise InputError("Use ordinary immutable ArrowColumn records")
        copies.append(
            ArrowColumn(
                column.field,
                column.column,
                column.predicate,
                column.kind,
                column.datatype,
                column.list,
                column.null,
                column.nan,
                column.timezone,
            )
        )
    owned = tuple(copies)
    asserted = capture_items(types, URIRef, operation)
    ignored = capture_items(ignored_columns, str, operation)
    names = {subject_column}
    fields = set()
    operation.consume("states", len(selected.fields))
    operation.consume("bytes", 64 + 64 * len(selected.fields))
    for field in selected.fields:
        bound_text(field.name, operation)
    declared = {field.name for field in selected.fields}
    for column in owned:
        if type(column) is not ArrowColumn:
            raise InputError("Use ordinary immutable ArrowColumn records")
        for text in (column.field, column.column):
            if type(text) is not str or not text:
                raise InputError("Column and logical field require nonempty text")
            bound_text(text, operation)
        if column.column in names or column.field in fields:
            raise InputError("Each physical column/logical field needs one binding")
        if column.field not in declared:
            raise InputError("Column binding names an absent model field")
        names.add(column.column)
        fields.add(column.field)
        if type(column.predicate) is not URIRef:
            raise InputError("Actual output predicates require URIRef identities")
        bound_node(column.predicate, operation)
        absolute_iri(column.predicate)
        if column.kind not in ("iri", "literal") or type(column.list) is not bool:
            raise InputError("Unknown Arrow scalar/container interpretation")
        if column.null not in ("omit", "reject") or column.nan not in (
            "preserve",
            "reject",
        ):
            raise InputError("Unknown explicit null/NaN policy")
        if column.kind == "iri":
            if column.datatype is not None or column.timezone is not None:
                raise InputError("IRI bindings do not have literal datatype/timezone")
        elif type(column.datatype) is not URIRef:
            raise InputError("Literal columns require an explicit datatype URIRef")
        else:
            bound_node(column.datatype, operation)
            if column.datatype not in (
                XSD.string,
                XSD.integer,
                XSD.boolean,
                XSD.double,
                XSD.decimal,
                XSD.dateTime,
            ):
                raise ResourceError("This Arrow datatype mapping is not qualified")
        if column.timezone is not None:
            if type(column.timezone) is not str or not column.timezone:
                raise InputError(
                    "An explicit timestamp timezone requires ordinary text"
                )
            bound_text(column.timezone, operation)
            if column.datatype != XSD.dateTime:
                raise InputError("Timezone is only applicable to timestamp bindings")
    if fields != declared:
        raise ResourceError(
            "Every selected model field needs an explicit Arrow binding"
        )
    for name in ignored:
        if type(name) is not str or not name or name in names:
            raise InputError("Ignored physical columns must be explicit and distinct")
        bound_text(name, operation)
        names.add(name)
    for kind in asserted:
        bound_node(kind, operation)
        absolute_iri(kind)
    if type(graph_name) not in (str, URIRef):
        raise InputError("Choose an ordinary default or named IRI output graph")
    bound_text(graph_name, operation)
    if graph_name != "@default":
        absolute_iri(graph_name)
    operation.consume("states", 8 + len(names))
    operation.consume("bytes", 512 + 64 * len(names))
    result = object.__new__(ArrowContract)
    for name, value in (
        ("models", models),
        ("model", model),
        ("subject_column", subject_column),
        ("columns", owned),
        ("types", asserted),
        ("graph_name", graph_name),
        ("ignored_columns", ignored),
    ):
        object.__setattr__(result, name, value)
    return result
