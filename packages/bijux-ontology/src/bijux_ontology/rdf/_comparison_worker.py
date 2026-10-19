"""Private RDFLib comparison boundary for bounded parent operations."""

import json
import sys

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.compare import isomorphic
from rdflib.term import Node

from bijux_ontology._json import decode_json
from bijux_ontology.errors import InputError

_BASE = "urn:bijux:ontology:comparison:"


def _term(value: object) -> Node:
    if not isinstance(value, list) or not value:
        raise InputError("Malformed comparison term")
    if value[0] == "blank" and len(value) == 2 and isinstance(value[1], str):
        return BNode("original:" + value[1])
    # Exact tagged strings prevent IRI/literal/default collisions and normalisation.
    if value[0] not in ("iri", "literal", "default"):
        raise InputError("Unknown comparison term kind")
    return Literal(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")), normalize=False
    )


def _encode(records: object) -> Graph:
    """Reify each quad/empty-graph record without colliding with caller nodes."""
    if not isinstance(records, list):
        raise InputError("Malformed comparison records")
    graph = Graph(bind_namespaces="none")
    for index, record in enumerate(records):
        if (
            not isinstance(record, list)
            or len(record) != 2
            or record[0] not in ("quad", "graph")
            or not isinstance(record[1], list)
        ):
            raise InputError("Malformed comparison record")
        if len(record[1]) != (4 if record[0] == "quad" else 1):
            raise InputError("Malformed comparison record arity")
        node = BNode("record:" + str(index))
        graph.add((node, URIRef(_BASE + "kind"), Literal(record[0])))
        for position, value in enumerate(record[1]):
            graph.add((node, URIRef(_BASE + "position:" + str(position)), _term(value)))
    return graph


def process_request(payload: bytes, maximum: int) -> bytes:
    """Evaluate the same finite protocol within an already deadline-owned worker."""
    if type(maximum) is not int or maximum <= 0:
        raise InputError("Comparison requires a positive protocol byte bound")
    values = decode_json(payload, max_bytes=maximum)
    if not isinstance(values, list) or len(values) != 2:
        raise InputError("Comparison requires two exact record inventories")
    equal = isomorphic(_encode(values[0]), _encode(values[1]))
    return b"true\n" if equal else b"false\n"


def main() -> int:
    """Read a finite parent-owned JSON request and emit only one boolean result."""
    try:
        maximum = int(sys.argv[1])
        if maximum <= 0:
            return 2
        payload = sys.stdin.buffer.read(maximum + 1)
        sys.stdout.buffer.write(process_request(payload, maximum))
        return 0
    except (ValueError, IndexError, TypeError):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
