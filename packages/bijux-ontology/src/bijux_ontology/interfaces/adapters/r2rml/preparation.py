"""Owned local snapshots, actual native capability and finite mapping inspection."""

from __future__ import annotations

import hashlib
import json
import re
import signal
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, XSD
from rdflib.term import Node

from bijux_ontology._json import decode_json, object_value
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.processes.transport import run_worker
from bijux_ontology.rdf.patches.correspondence import Operation, _json_bound
from bijux_ontology.rdf.terms import bound_node, bound_text, term_key

from .types import MorphBackend, R2rmlJoin, SqliteSnapshot
from .worker import REQUIRED

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


def _owned(cls: type[MorphBackend] | type[SqliteSnapshot], **values: object) -> object:
    """Publish private factories only after actual input/capability qualification."""
    result = object.__new__(cls)
    for name, value in values.items():
        object.__setattr__(result, name, value)
    return result


def _call(
    python: Path,
    request: dict[str, object],
    directory: Path,
    operation: Operation,
    *,
    file_cap: int,
) -> dict[str, object]:
    """Invoke the concrete stdlib worker in the explicitly supplied environment."""
    bound_text(str(python), operation)
    if not python.is_absolute() or not python.is_file():
        raise ResourceError("Supply an absolute installed Morph-KGC Python interpreter")
    cap = min(16384, operation.limits.max_bytes)
    if cap < 512 or file_cap < 512:
        raise LimitExceeded(
            "Native materialization requires a bounded control allowance"
        )
    operation.consume("states", 4)
    operation.consume("resources")
    operation.consume("bytes", 6 * cap + _json_bound(request, operation))
    payload = json.dumps(request, separators=(",", ":")).encode()
    if len(payload) > cap:
        raise LimitExceeded("Native control request exceeds its byte allowance")
    source, receipt = directory / "request.json", directory / "receipt.json"
    source.write_bytes(payload)
    command = [
        str(python),
        "-I",
        str(Path(__file__).with_name("worker.py")),
        str(source),
        str(receipt),
        str(file_cap),
    ]
    for value in command:
        bound_text(value, operation)
    code, stopped, _diagnostics = run_worker(
        command,
        timeout=max(0.000001, operation.deadline - time.monotonic()),
        cap=cap,
        cancellation=operation.cancel,
    )
    if stopped or code == -signal.SIGXFSZ:
        raise LimitExceeded("Native materialization stopped within its owned limits")
    if code != 0 or receipt.is_symlink() or not receipt.is_file():
        raise ResourceError("Native materializer did not return a complete receipt")
    with receipt.open("rb") as reader:
        raw = reader.read(cap + 1)
    if len(raw) > cap:
        raise LimitExceeded("Native completion receipt exceeds its byte allowance")
    data = object_value(
        decode_json(raw, max_bytes=cap, max_depth=16), location="Morph-KGC receipt"
    )
    operation.consume("bytes", _json_bound(data, operation))
    if data.get("versions") != REQUIRED:
        raise ResourceError(
            "Native interpreter differs from the qualified pinned profile"
        )
    if data.get("status") == "exhausted":
        raise LimitExceeded(
            "Native materializer exhausted its actual file/memory bound"
        )
    if data.get("status") != "completed":
        raise ResourceError(
            "The actual materializer did not complete: "
            + str(data.get("error", "unavailable"))
        )
    return data


def prepare_morph_backend(
    python: Path,
    *,
    artifacts: Path | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
) -> MorphBackend:
    """Qualify a separate normal Morph-KGC install without changing dependencies.

    Morph-KGC's RDFLib/PyOxigraph requirements conflict with the core pins.
    Its explicitly supplied interpreter runs a fixed concrete isolated worker;
    no environment is discovered, installed or added to Python's source path.
    """
    operation = Operation(limits, budget, timeout, None)
    if type(python) is not type(Path()) or (
        artifacts is not None and not isinstance(artifacts, Path)
    ):
        raise InputError("Native interpreter and artifact destination require Paths")
    destination = (
        artifacts
        if artifacts is not None
        else Path.cwd() / "artifacts" / "relational-rdf"
    )
    bound_text(str(destination), operation)
    if any(value in str(destination) for value in ("\r", "\n", ",", "%", "${")):
        raise InputError("Artifact paths cannot contain native config delimiters")
    destination.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="capability-", dir=destination) as job:
        _call(python, {"action": "capability"}, Path(job), operation, file_cap=16384)
    return cast(
        MorphBackend,
        _owned(
            MorphBackend,
            python=python,
            artifacts=destination.absolute(),
            version="2.10.0",
        ),
    )


def _read_file(path: Path, operation: Budget) -> bytes:
    """Pay the complete finite allocation before an owned binary read."""
    size = path.stat().st_size
    if size > operation.limits.max_bytes:
        raise LimitExceeded("SQLite snapshot exceeds max_bytes")
    operation.consume("bytes", size + 1)
    operation.consume("work", size + 1)
    with path.open("rb") as reader:
        raw = reader.read(size + 1)
    if len(raw) != size:
        raise ResourceError("SQLite snapshot changed during capture")
    return raw


def prepare_sqlite_snapshot(
    path: Path,
    *,
    artifacts: Path | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
) -> SqliteSnapshot:
    """Capture a read-only consistent SQLite backup and preserve original integrity.

    This local snapshot profile excludes adjacent WAL/SHM resources. SQLite's
    maintained backup operation supplies the consistent owned database image;
    no live connection or mapping SQL is accepted. Source changes invalidate
    capture. The materializer sees only a subsequent owned read-only copy.
    """
    operation = Operation(limits, budget, timeout, None)
    if not isinstance(path, Path):
        raise InputError("SQLite snapshots require an explicit local Path")
    bound_text(str(path), operation)
    if (
        path.is_symlink()
        or not path.is_file()
        or any(
            Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")
        )
    ):
        raise ResourceError("Supply one ordinary quiescent local SQLite snapshot")
    original = _read_file(path, operation)
    if not original.startswith(b"SQLite format 3\x00"):
        raise InputError("The supplied snapshot is not a SQLite database")
    digest = hashlib.sha256(original).hexdigest()
    destination = (
        artifacts
        if artifacts is not None
        else Path.cwd() / "artifacts" / "relational-rdf"
    )
    bound_text(str(destination), operation)
    destination.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="sqlite-snapshot-", dir=destination) as job:
        output = Path(job) / "snapshot.sqlite"
        try:
            with (
                sqlite3.connect(
                    path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0
                ) as source,
                sqlite3.connect(output) as target,
            ):

                def progress(status: int, remaining: int, total: int) -> None:
                    """Bound maintained backup callbacks, including retries."""
                    operation.consume("work")
                    if total * page_size > operation.limits.max_bytes:
                        raise LimitExceeded(
                            "SQLite backup exceeds its finite page allowance"
                        )

                page_size = source.execute("PRAGMA page_size").fetchone()[0]
                source.backup(target, pages=16, progress=progress, sleep=0.001)
        except sqlite3.Error as error:
            raise ResourceError(
                "The read-only SQLite snapshot is unavailable"
            ) from error
        content = _read_file(output, operation)
    if hashlib.sha256(_read_file(path, operation)).hexdigest() != digest:
        raise ResourceError("The original SQLite snapshot changed during capture")
    operation.consume("states")
    return cast(
        SqliteSnapshot,
        _owned(
            SqliteSnapshot,
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            source_sha256=digest,
            source_name=str(path),
        ),
    )


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
