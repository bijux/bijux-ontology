"""Owned RDF term tables and complete physical graph inventories for artifacts."""

from dataclasses import fields
from typing import Literal as Choice
from typing import cast

from rdflib import BNode, Dataset, Literal, URIRef
from rdflib.term import Node

from bijux_ontology._json import (
    array_value,
    boolean_value,
    object_value,
    optional_string_value,
    string_value,
)
from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, literal_term, validate_triple
from bijux_ontology.vocabulary.sources.types import OwnedSource


def record(value: object, keys: set[str]) -> dict[str, object]:
    """Reject missing and unknown wire fields before interpreting domain records."""
    data = object_value(value, location="preparation")
    if set(data) != keys:
        raise InputError("Preparation record has missing or unknown keys")
    return data


def reference(value: object, count: int) -> int:
    """Require an in-range integer reference, excluding JSON booleans."""
    if type(value) is not int or not 0 <= value < count:
        raise InputError("Preparation contains an invalid table reference")
    return value


class TermTable:
    """Keep document-owned anonymous sharing and exact physical literal metadata."""

    def __init__(self, budget: Budget) -> None:
        self.budget = budget
        self.rows: list[object] = []
        self._keys: dict[tuple[object, ...], int] = {}

    def add(self, term: Node) -> int:
        """Intern one already-owned term without merging unlike RDF term kinds."""
        bound_node(term, self.budget)
        if isinstance(term, Literal):
            key: tuple[object, ...] = (
                "literal",
                str(term),
                term.datatype,
                term.language,
            )
            row: object = [
                "literal",
                str(term),
                str(term.datatype) if term.datatype is not None else None,
                term.language,
            ]
        elif isinstance(term, BNode):
            key = ("blank", str(term))
            row = ["blank"]
        elif isinstance(term, URIRef):
            key = ("iri", str(term))
            row = ["iri", str(term)]
        else:
            raise InputError("Unsupported preparation RDF term")
        if key not in self._keys:
            self.budget.consume("states")
            self.budget.consume("bytes", 128)
            self._keys[key] = len(self.rows)
            self.rows.append(row)
        return self._keys[key]


def read_terms(value: object, budget: Budget) -> tuple[Node, ...]:
    """Decode a finite exact term table; anonymous identities are newly owned."""
    rows = array_value(value)
    budget.consume("states", len(rows))
    budget.consume("bytes", 128 * len(rows))
    result: list[Node] = []
    for item in rows:
        budget.consume("work")
        raw = array_value(item)
        if raw == ["blank"]:
            term: Node = BNode()
        elif len(raw) == 2 and raw[0] == "iri":
            term = URIRef(absolute_iri(string_value(raw[1])))
        elif len(raw) == 4 and raw[0] == "literal":
            term = literal_term(
                string_value(raw[1]),
                datatype=optional_string_value(raw[2]),
                language=optional_string_value(raw[3]),
            )
            if (str(term.datatype) if term.datatype is not None else None) != raw[2]:
                raise InputError("Preparation literal metadata cannot be retained")
        else:
            raise InputError("Unsupported preparation term record")
        bound_node(term, budget)
        result.append(term)
    return tuple(result)


def source_data(
    scope: str,
    owned: OwnedSource,
    metadata: SnapshotMetadata,
    terms: TermTable,
    budget: Budget,
) -> dict[str, object]:
    """Capture all physical contexts, retaining the source's declared selection."""
    prepared = owned.prepared
    count = prepared._quad_count()
    budget.consume("triples", count)
    budget.consume("work", count)
    budget.consume("bytes", 256 * count + 256)
    for graph_name in prepared._graph_names(budget=budget):
        bound_node(graph_name, budget)
    dataset = prepared.dataset()
    contexts: list[object] = []
    for graph in dataset.graphs():
        budget.consume("states")
        budget.consume("bytes", 128)
        name = graph.identifier
        default = name == dataset.default_graph.identifier
        triples = []
        for subject, predicate, obj in graph:
            budget.consume("work")
            budget.consume("states")
            triples.append([terms.add(subject), terms.add(predicate), terms.add(obj)])
        contexts.append(
            {"name": None if default else terms.add(name), "triples": triples}
        )
    namespaces: list[object] = []
    for prefix, iri in dataset.namespaces():
        budget.consume("states")
        budget.consume("bytes", 64)
        namespaces.append([prefix, str(iri)])
    data = {
        field.name: getattr(metadata, field.name) for field in fields(SnapshotMetadata)
    }
    if isinstance(data["graph_name"], BNode) or isinstance(owned.graph_name, BNode):
        raise InputError(
            "External anonymous graph selections are not an artifact profile"
        )
    if not metadata.same_scope(SnapshotMetadata.capture(prepared, budget)):
        raise InputError("Prepared source metadata changed during artifact capture")
    return {
        "scope": scope,
        "metadata": data,
        "selection": {"graph_name": owned.graph_name, "union": owned.union},
        "contexts": contexts,
        "namespaces": namespaces,
    }


def read_source(
    value: object, terms: tuple[Node, ...], budget: Budget
) -> tuple[str, OwnedSource, SnapshotMetadata]:
    """Reconstruct one new owned source with full physical inventory and scope."""
    raw = record(value, {"scope", "metadata", "selection", "contexts", "namespaces"})
    meta = record(raw["metadata"], {field.name for field in fields(SnapshotMetadata)})
    is_dataset = boolean_value(meta["is_dataset"])
    document_iri = optional_string_value(meta["document_iri"])
    effective_url = optional_string_value(meta["effective_url"])
    for iri in (document_iri, effective_url):
        if iri is not None:
            absolute_iri(iri)
    graph_fidelity = string_value(meta["graph_fidelity"])
    if graph_fidelity not in ("complete", "nonempty-only"):
        raise InputError("Unsupported preparation graph fidelity")
    graph_name = optional_string_value(meta["graph_name"])
    dataset = Dataset()
    names: set[Node | None] = set()
    for item in array_value(raw["contexts"]):
        budget.consume("work")
        budget.consume("states")
        context = record(item, {"name", "triples"})
        name = (
            None
            if context["name"] is None
            else terms[reference(context["name"], len(terms))]
        )
        if name is not None and not isinstance(name, (URIRef, BNode)):
            raise InputError("Preparation graph names must be resources")
        if name in names:
            raise InputError("Preparation repeats a physical graph")
        names.add(name)
        graph = dataset.default_graph if name is None else dataset.graph(name)
        for row in array_value(context["triples"]):
            triple = array_value(row)
            if len(triple) != 3:
                raise InputError("Preparation triples need exactly three references")
            subject, predicate, obj = (
                terms[reference(ref, len(terms))] for ref in triple
            )
            validate_triple(subject, predicate, obj)
            budget.consume("triples")
            budget.consume("work")
            budget.consume("bytes", 256)
            graph.add((subject, predicate, obj))
    if None not in names or not is_dataset and names != {None}:
        raise InputError("Preparation physical graph inventory is incompatible")
    for item in array_value(raw["namespaces"]):
        pair = array_value(item)
        if len(pair) != 2:
            raise InputError("Preparation namespace needs a prefix and IRI")
        dataset.bind(string_value(pair[0]), URIRef(absolute_iri(string_value(pair[1]))))
    prepared = PreparedSource._from_owned(
        dataset,
        is_dataset=is_dataset,
        document_iri=document_iri,
        effective_url=effective_url,
        literal_fidelity=string_value(meta["literal_fidelity"]),
        max_triples=budget.limits.max_triples,
        graph_fidelity=cast(Choice["complete", "nonempty-only"], graph_fidelity),
        graph_name=graph_name,
        union=boolean_value(meta["union"]),
    )
    selection = record(raw["selection"], {"graph_name", "union"})
    selected_name = optional_string_value(selection["graph_name"])
    union = boolean_value(selection["union"])
    selected = prepared._read(selected_name, union=union, budget=budget)
    owned = OwnedSource(prepared, selected, selected_name, union)
    return string_value(raw["scope"]), owned, SnapshotMetadata.capture(prepared, budget)
