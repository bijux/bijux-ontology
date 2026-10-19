"""Finite supplied ShExJ capability and import closure; no semantic translation."""

from collections.abc import Mapping, Sequence
from typing import Any

from rdflib import Graph, Literal
from rdflib.namespace import XSD

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.inputs import capture
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.json.values import decode_json, object_value, owned_json
from bijux_ontology.rdf.entailment.protocol import encode_rows
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_node

from .types import ShexBinding, ShexSchema

CONTEXT = "http://www.w3.org/ns/shex.jsonld"


class Unsupported(InputError):
    """A supplied feature exceeds the qualified native capability profile."""


def _items(value: object, budget: Budget) -> tuple[Any, ...]:
    """Require actual finite JSON arrays at native schema collection fields."""
    if not isinstance(value, list):
        raise InputError("ShEx schema collections require JSON arrays")
    return capture(value, budget)


def _iri(value: object, budget: Budget) -> str:
    """Bound exact named identities before parsing, hashing or native dispatch."""
    if not isinstance(value, str):
        raise InputError("ShEx identifiers require absolute IRI strings")
    bound_text(value, budget)
    try:
        absolute_iri(value)
    except InputError as error:
        raise Unsupported("Relative ShEx identifiers are unsupported") from error
    return value


def _expression(value: Any, budget: Budget, refs: set[str], depth: int = 0) -> None:
    """Gate native standard JSON fields, leaving constraint semantics maintained."""
    budget.consume("work")
    if depth > budget.limits.max_depth:
        raise LimitExceeded("ShEx expression exceeds max_depth")
    if isinstance(value, str):
        budget.consume("states")
        refs.add(_iri(value, budget))
        return
    if not isinstance(value, dict):
        raise InputError("ShEx expressions require native objects or named references")
    kind = value.get("type")
    fields = {
        "Shape": {"type", "expression", "closed", "extra"},
        "NodeConstraint": {"type", "nodeKind", "datatype"},
        "TripleConstraint": {"type", "predicate", "valueExpr", "min", "max", "inverse"},
        "EachOf": {"type", "expressions", "min", "max"},
        "OneOf": {"type", "expressions", "min", "max"},
    }
    if not isinstance(kind, str) or kind not in fields or set(value) - fields[kind]:
        raise Unsupported("ShEx expression features exceed the native profile")
    for key in ("min", "max"):
        if key in value:
            count = value[key]
            if type(count) is not int or count < (-1 if key == "max" else 0):
                raise InputError("ShEx cardinalities require native nonnegative ranges")
            if count.bit_length() > 31:
                raise Unsupported("ShEx cardinality exceeds the native scalar profile")
    for key in ("closed", "inverse"):
        if key in value and type(value[key]) is not bool:
            raise InputError("ShEx closed and inverse flags require booleans")
    if kind == "Shape":
        if "extra" in value:
            for iri in _items(value["extra"], budget):
                _iri(iri, budget)
        if "expression" in value:
            _expression(value["expression"], budget, refs, depth + 1)
    elif kind == "NodeConstraint":
        if "datatype" in value and _iri(value["datatype"], budget) not in (
            str(XSD.string),
            str(XSD.integer),
        ):
            raise Unsupported("ShEx datatype is outside the qualified native profile")
        if "nodeKind" in value and value["nodeKind"] not in (
            "iri",
            "bnode",
            "literal",
            "nonliteral",
        ):
            raise Unsupported("Unsupported native ShEx node kind")
    elif kind == "TripleConstraint":
        _iri(value.get("predicate"), budget)
        if "valueExpr" in value:
            _expression(value["valueExpr"], budget, refs, depth + 1)
    else:
        children = _items(value.get("expressions"), budget)
        if len(children) < 2:
            raise InputError("ShEx grouped expressions require at least two members")
        for child in children:
            _expression(child, budget, refs, depth + 1)


def schemas(
    root: ShexSchema, imports: Mapping[str, ShexSchema] | None, budget: Budget
) -> tuple[dict[str, object], tuple[str, ...], set[str]]:
    """Join only original supplied declaration records after exact closure checks."""
    if (
        not isinstance(root, ShexSchema)
        or imports is not None
        and not isinstance(imports, Mapping)
    ):
        raise InputError("ShEx requires typed supplied schema documents")
    _iri(root.document_iri, budget)
    sources = {root.document_iri: root}
    if imports is not None:
        budget.consume("states", len(imports))
        for key, source in imports.items():
            _iri(key, budget)
            if not isinstance(source, ShexSchema) or source.document_iri != key:
                raise InputError("ShEx import binding conflicts with document identity")
            if key in sources and source != sources[key]:
                raise InputError("Conflicting supplied ShEx documents")
            bound_text(source.content, budget)
            sources[key] = source
    seen: set[str] = set()
    active: set[str] = set()
    ids: set[str] = set()
    refs: set[str] = set()
    declarations: list[object] = []
    closure: list[str] = []

    def visit(iri: str, depth: int) -> None:
        """Resolve finite logical bindings without a native URI or file loader."""
        budget.consume("work")
        if depth > budget.limits.max_depth:
            raise LimitExceeded("ShEx imports exceed max_depth")
        if iri in active:
            raise Unsupported("Cyclic ShEx imports are unsupported")
        if iri in seen:
            return
        if iri not in sources:
            raise Unsupported("A required ShEx import has no supplied document")
        active.add(iri)
        budget.consume("resources")
        text = sources[iri].content
        bound_text(text, budget)
        doc = object_value(
            decode_json(
                text,
                max_bytes=budget.limits.max_bytes,
                max_depth=budget.limits.max_depth,
            ),
            location="ShExJ schema",
        )
        owned, _ = owned_json(doc, budget)
        doc = object_value(owned, location="ShExJ schema")
        if (
            set(doc) - {"@context", "type", "shapes", "imports"}
            or doc.get("@context") != CONTEXT
            or doc.get("type") != "Schema"
        ):
            raise Unsupported(
                "Only the explicit native ShapeDecl ShExJ profile is supported"
            )
        for declaration in _items(doc.get("shapes"), budget):
            if (
                not isinstance(declaration, dict)
                or set(declaration) != {"type", "id", "shapeExpr"}
                or declaration["type"] != "ShapeDecl"
            ):
                raise Unsupported(
                    "ShEx declarations require the native ShapeDecl profile"
                )
            identity = _iri(declaration["id"], budget)
            if identity in ids:
                raise InputError("Duplicate authored ShEx shape identifiers")
            budget.consume("states")
            ids.add(identity)
            _expression(declaration["shapeExpr"], budget, refs)
            declarations.append(declaration)
        for child in _items(doc.get("imports", []), budget):
            visit(_iri(child, budget), depth + 1)
        active.remove(iri)
        seen.add(iri)
        if iri != root.document_iri:
            closure.append(iri)

    visit(root.document_iri, 0)
    if refs - ids:
        raise InputError("ShEx schema contains dangling named shape references")
    return (
        {"@context": CONTEXT, "type": "Schema", "shapes": declarations},
        tuple(closure),
        ids,
    )


def bindings(values: Sequence[ShexBinding], budget: Budget) -> tuple[ShexBinding, ...]:
    """Capture requested RDF identities without applying native coercions."""
    result = []
    for value in capture(values, budget):
        if not isinstance(value, ShexBinding):
            raise InputError("ShEx shape maps require typed ShexBinding records")
        for term in (value.focus, value.shape):
            bound_node(term, budget)
        result.append(value)
    return tuple(result)


def data(
    source: Graph | PreparedSource,
    graph_name: GraphSelection | None,
    union: bool,
    budget: Budget,
) -> tuple[list[list[object]], str]:
    """Capture exact selected RDF and enforce the explicit native integer capacity."""
    prepared = (
        source
        if isinstance(source, PreparedSource)
        else prepare_rdf(source, limits=budget.limits, budget=budget)
    )
    graph = prepared._read(graph_name, union=union, budget=budget)
    for triple in graph:
        budget.consume("work")
        for term in triple:
            if isinstance(term, Literal) and term.datatype == XSD.integer:
                bound_node(term, budget)
                text = str(term)
                if len(text) > 19 or len(text.lstrip("+-")) > 18:
                    raise Unsupported(
                        "Integer literal exceeds the native scalar capacity"
                    )
                budget.consume("work", len(text))
                if any(char in " \t\r\n" for char in text):
                    raise Unsupported(
                        "Integer XML whitespace is outside the native lexical profile"
                    )
    scope = (
        "union"
        if union or prepared.union
        else str(prepared.graph_name or graph_name or "@graph")
    )
    return encode_rows(graph, budget), scope
