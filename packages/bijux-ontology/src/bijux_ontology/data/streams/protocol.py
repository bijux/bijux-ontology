"""Bounded owned stream-worker records using existing RDF and failure codecs."""

from dataclasses import asdict
from pathlib import Path
from typing import cast

from rdflib import BNode, URIRef

from bijux_ontology._json import object_value
from bijux_ontology.diagnostics.errors import InputError, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.limits import Budget, bound_text
from bijux_ontology.rdf.queries.access import QueryScope
from bijux_ontology.rdf.queries.protocol import read_term, term_data
from bijux_ontology.rdf.stores.preparation import _adopt
from bijux_ontology.rdf.stores.protocol import scope_data
from bijux_ontology.rdf.stores.seals import StoreFile
from bijux_ontology.rdf.stores.types import StoredRdf
from bijux_ontology.rdf.terms import bound_node

from .types import StreamStatement


def stored_data(
    snapshot: StoredRdf, scope: QueryScope, budget: Budget
) -> dict[str, object]:
    """Capture only bounded metadata from the actual immutable native owner."""
    selected = scope_data(snapshot, scope, budget)
    if scope.kind == "dataset":
        raise InputError("Stream vocabulary needs default, named or union scope")
    bound_text(str(snapshot.directory), budget)
    budget.consume("states", len(snapshot.files) + len(snapshot.graph_names))
    files = []
    for item in snapshot.files:
        bound_text(item.name, budget)
        bound_text(item.sha256, budget)
        record = asdict(item)
        budget.consume("bytes", json_bound(record, budget))
        files.append(record)
    names = []
    for name in snapshot.graph_names:
        bound_node(name, budget)
        names.append(term_data(name))
    metadata = {
        "source_sha256": snapshot.source_sha256,
        "source_format": snapshot.source_format,
        "quads": snapshot.quads,
        "graphs": names,
        "backend": snapshot.backend_version,
        "files": files,
    }
    budget.consume("bytes", json_bound(metadata, budget))
    bound_text(snapshot.revision, budget)
    return {
        "directory": str(snapshot.directory),
        "metadata": metadata,
        "revision": snapshot.revision,
        **selected,
    }


def read_stored(value: object, budget: Budget) -> tuple[StoredRdf, QueryScope]:
    """Reuse the native snapshot adoption contract, preserving its real IDs."""
    data = object_value(value, location="RDF stream worker")
    metadata = object_value(data.get("metadata"), location="RDF stream worker")
    directory = data.get("directory")
    if not isinstance(directory, str) or not isinstance(data.get("revision"), str):
        raise ResourceError("Invalid stream vocabulary snapshot identity")
    bound_text(directory, budget)
    files_data = metadata.get("files")
    if not isinstance(files_data, list):
        raise ResourceError("Stream vocabulary has no sealed file inventory")
    budget.consume("states", len(files_data))
    files = []
    for item in files_data:
        fields = object_value(item, location="RDF stream worker")
        if set(fields) != {"name", "size", "modified_ns", "inode", "sha256"}:
            raise ResourceError("Invalid stream vocabulary file record")
        for key in ("name", "sha256"):
            if not isinstance(fields[key], str):
                raise ResourceError("Invalid sealed file identity")
            bound_text(cast(str, fields[key]), budget)
        if any(
            type(fields[k]) is not int or cast(int, fields[k]) < 0
            for k in ("size", "modified_ns", "inode")
        ):
            raise ResourceError("Invalid sealed file revision")
        files.append(
            StoreFile(
                cast(str, fields["name"]),
                cast(int, fields["size"]),
                cast(int, fields["modified_ns"]),
                cast(int, fields["inode"]),
                cast(str, fields["sha256"]),
            )
        )
    snapshot = _adopt(Path(directory), metadata, tuple(files), cast(Operation, budget))
    if snapshot.revision != data["revision"]:
        raise ResourceError("Stream vocabulary revision changed in handoff")
    kind = data.get("scope")
    if kind not in ("default", "named", "union"):
        raise ResourceError("Invalid stream vocabulary graph scope")
    name: URIRef | BNode | None = None
    if kind == "named":
        raw = object_value(data.get("graph"), location="RDF stream worker")
        node = read_term(raw, {})
        if isinstance(node, BNode):
            budget.consume("work", len(snapshot.graph_names))
            name = next(
                (
                    n
                    for n in snapshot.graph_names
                    if isinstance(n, BNode) and str(n) == raw["value"]
                ),
                None,
            )
            if name is None:
                raise ResourceError("Unowned stream vocabulary blank graph")
        elif isinstance(node, URIRef):
            name = node
        else:
            raise ResourceError("Invalid stream vocabulary graph name")
    return snapshot, QueryScope(kind, name)


def read_statement(
    value: object, blanks: dict[str, BNode], budget: Budget
) -> StreamStatement:
    """Retain one bounded original occurrence in the joint result blank scope."""
    data = object_value(value, location="RDF stream worker")
    index, terms = data.get("index"), data.get("terms")
    if (
        type(index) is not int
        or index <= 0
        or not isinstance(terms, list)
        or len(terms) != 4
    ):
        raise ResourceError("Invalid stream statement witness")
    budget.consume("states", 5)
    nodes = tuple(read_term(term, blanks) for term in terms)
    subject, predicate, target, graph = nodes
    if (
        not isinstance(subject, URIRef | BNode)
        or not isinstance(predicate, URIRef)
        or target is None
        or graph is not None
        and not isinstance(graph, URIRef | BNode)
    ):
        raise ResourceError("Invalid stream statement roles")
    for node in nodes:
        if node is not None:
            bound_node(node, budget)
    return StreamStatement(index, subject, predicate, target, graph)
