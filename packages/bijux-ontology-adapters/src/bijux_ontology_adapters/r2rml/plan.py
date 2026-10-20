"""One structurally qualified finite mapping and its exact source/output contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, XSD
from rdflib.term import Node

from bijux_ontology.data.foreign_references.types import (
    ForeignReferenceContract,
)
from bijux_ontology.data.keys.types import KeyRecord
from bijux_ontology.declarations.keys import KeyContract
from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.diagnostics.errors import InputError, ResourceError
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import (
    Budget,
    bound_text,
)
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.paths.traversal import finite_path_steps
from bijux_ontology.rdf.paths.types import capture_path
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_node, term_key

from .types import (
    MorphBackend,
    R2rmlForeignCheck,
    R2rmlJoin,
    R2rmlPopulation,
    R2rmlRow,
    SqliteSnapshot,
)

RR = Namespace("http://www.w3.org/ns/r2rml#")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_TEMPLATE = re.compile(r"[^{}\\]*\{([A-Za-z_][A-Za-z0-9_]*)\}[^{}\\]*\Z")


@dataclass(frozen=True)
class _Column:
    """A declared source reference and exact supported term-map role."""

    name: str
    role: str
    datatype: URIRef | None = None


@dataclass(frozen=True)
class _Map:
    """Original mapping identities, with no executable substitute mapping AST."""

    node: Node
    logical: Node
    table: str
    columns: tuple[_Column, ...]
    joins: tuple[R2rmlJoin, ...]
    graph: URIRef | str


def _identifier(value: str, operation: Budget) -> str:
    """Admit plain supported identifiers, never SQL fragments or functions."""
    bound_text(value, operation)
    if not _IDENTIFIER.fullmatch(value):
        raise ResourceError("Only plain SQLite table/column identifiers are supported")
    return value


def _values(
    graph: Graph, subject: Node, predicate: URIRef, operation: Budget
) -> tuple[Node, ...]:
    """Capture every actual property value before cardinality qualification."""
    values = []
    seen = set()
    for value in graph.objects(subject, predicate):
        operation.consume("work")
        bound_node(value, operation)
        operation.consume("states", 2)
        operation.consume("bytes", 128)
        key = term_key(value)
        if key not in seen:
            seen.add(key)
            values.append(value)
    return tuple(values)


def _one(graph: Graph, subject: Node, predicate: URIRef, operation: Budget) -> Node:
    """Require one exact structural value instead of taking the first alternative."""
    values = _values(graph, subject, predicate, operation)
    if len(values) != 1:
        raise ResourceError("A required R2RML mapping value is missing or ambiguous")
    return values[0]


def _text(value: Node, operation: Budget) -> str:
    """Preserve ordinary declared SQL/template text without value coercion."""
    bound_node(value, operation)
    if (
        not isinstance(value, Literal)
        or value.language
        or value.datatype not in (None, XSD.string)
    ):
        raise ResourceError("Mapping text requires one ordinary string literal")
    return str(value)


def _role(graph: Graph, node: Node, allowed: set[URIRef], operation: Budget) -> None:
    """Reject ignored structural fields even when their owner is otherwise supported."""
    for predicate, _ in graph.predicate_objects(node):
        operation.consume("work")
        if predicate not in allowed | {RDF.type, RDFS.label, RDFS.comment}:
            raise ResourceError("A mapping role has an unsupported or competing field")


def _graphs(
    graph: Graph, node: Node, visited: set[Node], operation: Budget
) -> tuple[URIRef | str, ...]:
    """Qualify actual constant graph maps while retaining default graph identity."""
    result = list(_values(graph, node, RR.graph, operation))
    for mapping in _values(graph, node, RR.graphMap, operation):
        visited.add(mapping)
        _role(graph, mapping, {RR.constant}, operation)
        result.append(_one(graph, mapping, RR.constant, operation))
    if any(not isinstance(value, URIRef) for value in result):
        raise ResourceError("This mapping profile requires constant IRI graph maps")
    return tuple(
        "@default" if value == RR.defaultGraph else cast(URIRef, value)
        for value in result
    )


def inspect_mapping(graph: Graph, operation: Budget) -> tuple[_Map, ...]:
    """Inspect complete finite topology without generating any mapped terms."""
    supported = {
        RR[name]
        for name in (
            "logicalTable",
            "tableName",
            "subjectMap",
            "template",
            "class",
            "graph",
            "graphMap",
            "constant",
            "predicateObjectMap",
            "predicate",
            "objectMap",
            "column",
            "datatype",
            "parentTriplesMap",
            "joinCondition",
            "child",
            "parent",
            "termType",
        )
    }
    for subject, predicate, value in graph:
        operation.consume("work")
        for node in (subject, predicate, value):
            bound_node(node, operation)
        if predicate not in supported | {RDF.type, RDFS.label, RDFS.comment}:
            raise ResourceError(
                "An R2RML extension/source/query/function is outside the finite profile"
            )
        if predicate == RDF.type and value not in (
            RR.TriplesMap,
            RR.LogicalTable,
            RR.SubjectMap,
            RR.ObjectMap,
            RR.PredicateObjectMap,
            RR.RefObjectMap,
            RR.Join,
            RR.GraphMap,
        ):
            raise ResourceError("Unsupported required mapping type")
    captured_roots = []
    for node in graph.subjects(RR.logicalTable, None):
        operation.consume("work")
        bound_node(node, operation)
        operation.consume("states", 4)
        operation.consume("bytes", 256)
        captured_roots.append(node)
    roots = tuple(captured_roots)
    if not roots or len(set(roots)) != len(roots):
        raise ResourceError("Mapping has no unambiguous triples-map inventory")
    visited: set[Node] = set()
    result = []
    for root in roots:
        visited.add(root)
        _role(
            graph,
            root,
            {RR.logicalTable, RR.subjectMap, RR.predicateObjectMap},
            operation,
        )
        logical = _one(graph, root, RR.logicalTable, operation)
        visited.add(logical)
        _role(graph, logical, {RR.tableName}, operation)
        table = _identifier(
            _text(_one(graph, logical, RR.tableName, operation), operation), operation
        )
        subject = _one(graph, root, RR.subjectMap, operation)
        visited.add(subject)
        _role(
            graph,
            subject,
            {RR.template, RR["class"], RR.graph, RR.graphMap, RR.termType},
            operation,
        )
        template = _text(_one(graph, subject, RR.template, operation), operation)
        matched = _TEMPLATE.fullmatch(template)
        if not matched:
            raise ResourceError(
                "Subject templates require one plain column in this profile"
            )
        kinds = _values(graph, subject, RR.termType, operation)
        if kinds not in ((), (RR.IRI,)):
            raise ResourceError("This profile requires IRI subject terms")
        columns = [_Column(matched.group(1), "subject")]
        subject_graphs = _graphs(graph, subject, visited, operation)
        contexts: set[URIRef | str] = set()
        if _values(graph, subject, RR["class"], operation):
            if any(
                not isinstance(value, URIRef)
                for value in graph.objects(subject, RR["class"])
            ):
                raise ResourceError("Asserted classes require actual IRI terms")
            contexts.update(subject_graphs or ("@default",))
        joins = []
        for pom in _values(graph, root, RR.predicateObjectMap, operation):
            visited.add(pom)
            _role(
                graph,
                pom,
                {RR.predicate, RR.objectMap, RR.graph, RR.graphMap},
                operation,
            )
            predicate = _one(graph, pom, RR.predicate, operation)
            if not isinstance(predicate, URIRef):
                raise ResourceError("Predicate maps require one constant IRI")
            contexts.update(
                (*subject_graphs, *_graphs(graph, pom, visited, operation))
                or ("@default",)
            )
            obj = _one(graph, pom, RR.objectMap, operation)
            visited.add(obj)
            parents = _values(graph, obj, RR.parentTriplesMap, operation)
            if parents:
                _role(graph, obj, {RR.parentTriplesMap, RR.joinCondition}, operation)
                if len(parents) != 1 or parents[0] not in roots:
                    raise ResourceError(
                        "Referencing object map has no unique selected parent"
                    )
                join = _one(graph, obj, RR.joinCondition, operation)
                visited.add(join)
                _role(graph, join, {RR.child, RR.parent}, operation)
                child = _identifier(
                    _text(_one(graph, join, RR.child, operation), operation), operation
                )
                parent = _identifier(
                    _text(_one(graph, join, RR.parent, operation), operation), operation
                )
                columns.append(_Column(child, "join"))
                joins.append(R2rmlJoin(obj, parents[0], predicate, child, parent))
                if (
                    _values(graph, obj, RR.column, operation)
                    or _values(graph, obj, RR.constant, operation)
                    or _values(graph, obj, RR.datatype, operation)
                    or _values(graph, obj, RR.termType, operation)
                ):
                    raise ResourceError(
                        "Referencing object maps cannot hide another term source"
                    )
            else:
                _role(graph, obj, {RR.column, RR.datatype, RR.termType}, operation)
                column = _identifier(
                    _text(_one(graph, obj, RR.column, operation), operation), operation
                )
                datatype = _values(graph, obj, RR.datatype, operation)
                if len(datatype) > 1 or any(
                    value not in (XSD.integer, XSD.string) for value in datatype
                ):
                    raise ResourceError(
                        "Only explicit integer/string datatypes are qualified"
                    )
                if _values(graph, obj, RR.termType, operation) not in (
                    (),
                    (RR.Literal,),
                ):
                    raise ResourceError("Column objects require literal term maps")
                columns.append(
                    _Column(
                        column,
                        "object",
                        cast(URIRef, datatype[0]) if datatype else None,
                    )
                )
        if len(contexts) != 1:
            raise ResourceError(
                "Each selected map requires one explicit physical output graph"
            )
        result.append(
            _Map(
                root, logical, table, tuple(columns), tuple(joins), next(iter(contexts))
            )
        )
    for subject, predicate, _ in graph:
        operation.consume("work")
        if predicate not in (RDFS.label, RDFS.comment) and subject not in visited:
            raise ResourceError(
                "A required mapping structure is outside the selected root inventory"
            )
    return tuple(result)


def _capture_populations(
    populations: Sequence[R2rmlPopulation],
    operation: Budget,
) -> tuple[R2rmlPopulation, ...]:
    """Own every explicit per-map key/subject obligation before native work."""
    result = []
    seen = set()
    for population in capture_items(populations, R2rmlPopulation, operation):
        if type(population) is not R2rmlPopulation:
            raise InputError("Supply ordinary typed mapping populations")
        bound_node(population.triples_map, operation)
        if population.triples_map in seen:
            raise InputError("Each triples map needs one unique population")
        operation.consume("states", 2)
        seen.add(population.triples_map)
        _identifier(population.key_column, operation)
        key = population.emitted_key
        if type(key) is not KeyContract:
            raise InputError(
                "Population emitted keys require ordinary KeyContract records"
            )
        if (
            len(key.components) != 1
            or key.absence != "reject"
            or key.multiplicity != "reject"
            or key.components[0].equality != "rdf-term"
            or key.components[0].normalization != "none"
        ):
            raise ResourceError(
                "Population keys require one exact non-excluded emitted key"
            )
        rows = capture_items(population.rows, R2rmlRow, operation)
        if not rows:
            raise ResourceError("A required map population cannot be empty")
        subjects = set()
        for row in rows:
            if (
                type(row) is not R2rmlRow
                or type(row.usage) is not RdfModelUsage
                or type(row.key) is not Literal
                or type(row.usage.subject) is not URIRef
            ):
                raise InputError(
                    "Population rows require ordinary literal keys "
                    "and named model bindings"
                )
            bound_node(row.key, operation)
            bound_node(row.usage.subject, operation)
            absolute_iri(row.usage.subject)
            bound_text(row.usage.model, operation)
            bound_text(row.usage.location, operation)
            if row.key.language or row.key.datatype not in (
                None,
                XSD.string,
                XSD.integer,
            ):
                raise ResourceError(
                    "Source keys require integer or ordinary string literals"
                )
            if row.usage.subject in subjects:
                raise ResourceError(
                    "Source rows require unique expected subjects within each map"
                )
            operation.consume("states")
            subjects.add(row.usage.subject)
        operation.consume("bytes", 128)
        result.append(
            R2rmlPopulation(population.triples_map, population.key_column, key, rows)
        )
    return tuple(result)


def _foreign_coverage(
    mappings: tuple[_Map, ...],
    populations: tuple[R2rmlPopulation, ...],
    requests: tuple[R2rmlForeignCheck, ...],
    operation: Budget,
) -> tuple[tuple[R2rmlForeignCheck, R2rmlPopulation], ...]:
    """Require actual joined-endpoint paths over every child population."""
    expected = {}
    for mapping in mappings:
        for join in mapping.joins:
            operation.consume("states")
            operation.consume("bytes", 64)
            if join.object_map in expected:
                raise ResourceError(
                    "Shared referencing object-map ownership is unqualified"
                )
            expected[join.object_map] = (mapping, join)
    if len(requests) != len(expected):
        raise ResourceError(
            "Every selected parent join requires explicit foreign-reference coverage"
        )
    result = []
    seen = set()
    by_map = {population.triples_map: population for population in populations}
    for request in requests:
        if type(request) is not R2rmlForeignCheck:
            raise InputError("Supply ordinary typed foreign-reference requests")
        bound_node(request.object_map, operation)
        if request.object_map not in expected or request.object_map in seen:
            raise InputError(
                "Foreign checks require unique actual referencing object-map identities"
            )
        seen.add(request.object_map)
        mapping, join = expected[request.object_map]
        contract = request.contract
        if (
            type(request) is not R2rmlForeignCheck
            or type(contract) is not ForeignReferenceContract
        ):
            raise InputError("Supply ordinary typed foreign-reference contracts")
        if (
            contract.lookup != "closed"
            or contract.minimum_targets != 1
            or contract.maximum_targets != 1
            or contract.source.absence != "reject"
        ):
            raise ResourceError(
                "Required joins need closed non-excluded foreign target coverage"
            )
        relevant = False
        for component in contract.source.components:
            steps = finite_path_steps(
                capture_path(component.path, budget=operation), operation
            )
            relevant |= bool(steps and steps[0] == (join.predicate, False))
        if not relevant:
            raise ResourceError(
                "Foreign coverage must read the actual emitted joined endpoint"
            )
        if join.parent_column != by_map[join.parent_map].key_column:
            raise ResourceError(
                "This join profile requires the parent's unique declared population key"
            )
        targets = capture_items(request.targets, KeyRecord, operation)
        if type(request.target_complete) is not bool:
            raise InputError("Foreign corpus completeness requires a boolean")
        operation.consume("states")
        result.append(
            (
                R2rmlForeignCheck(
                    request.object_map, contract, targets, request.target_complete
                ),
                by_map[mapping.node],
            )
        )
    return tuple(result)


@dataclass(frozen=True, slots=True)
class _MappingPlan:
    """Admitted finite obligations; source/native/output success remains separate."""

    backend: MorphBackend
    snapshot: SqliteSnapshot
    mapping: PreparedSource
    mapping_graph: GraphSelection | None
    output_graph: GraphSelection
    mappings: tuple[_Map, ...]
    populations: tuple[R2rmlPopulation, ...]
    models: tuple[ModelDeclaration, ...]
    foreign: tuple[tuple[R2rmlForeignCheck, R2rmlPopulation], ...]
    by_map: Mapping[Node, R2rmlPopulation]


def qualify_plan(
    backend: MorphBackend,
    snapshot: SqliteSnapshot,
    mapping: PreparedSource,
    selected: tuple[R2rmlPopulation, ...],
    declarations: tuple[ModelDeclaration, ...],
    requests: tuple[R2rmlForeignCheck, ...],
    graph_name: GraphSelection,
    mapping_graph: GraphSelection | None,
    operation: Budget,
) -> _MappingPlan:
    """Bind original mapping facts to every selected population and join contract."""
    graph = mapping._read(mapping_graph, budget=operation)
    mappings = inspect_mapping(graph, operation)
    by_map = {population.triples_map: population for population in selected}
    if set(by_map) != {item.node for item in mappings}:
        raise ResourceError(
            "Every actual triples map needs its full required population"
        )
    if isinstance(graph_name, BNode):
        raise ResourceError("This output profile requires a named IRI/default graph")
    if type(graph_name) not in (str, URIRef):
        raise InputError("Select an ordinary default or named IRI output graph")
    bound_text(graph_name, operation)
    if graph_name != "@default":
        absolute_iri(graph_name)
    selected_graph = "@default" if graph_name == "@default" else URIRef(str(graph_name))
    if any(item.graph != selected_graph for item in mappings):
        raise ResourceError(
            "Output contracts must retain each mapping's exact physical graph"
        )
    owned_blanks = set()
    for triple in graph:
        operation.consume("work")
        for node in triple:
            if isinstance(node, BNode):
                operation.consume("states")
                operation.consume("bytes", 32)
                owned_blanks.add(id(node))
    for identity in (
        *(population.triples_map for population in selected),
        *(request.object_map for request in requests),
    ):
        if isinstance(identity, BNode) and id(identity) not in owned_blanks:
            raise ResourceError(
                "Blank mapping identity lacks original snapshot ownership"
            )
    covered_foreign = _foreign_coverage(mappings, selected, requests, operation)
    # Fund the slotted record, read-only index proxy and constructor references.
    operation.consume("work", 12)
    operation.consume("states")
    operation.reserve_storage(512)
    return _MappingPlan(
        backend,
        snapshot,
        mapping,
        mapping_graph,
        selected_graph,
        mappings,
        selected,
        declarations,
        covered_foreign,
        MappingProxyType(by_map),
    )
