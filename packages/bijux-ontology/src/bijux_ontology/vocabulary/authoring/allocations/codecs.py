"""Bounded exact assignment records and allocation artifact serialization."""

import json
from typing import Literal as TypingLiteral
from typing import cast

from rdflib import URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.json.values import string_value
from bijux_ontology.rdf.records.codecs import read_term, term_data
from bijux_ontology.rdf.records.types import PatchLiteral
from bijux_ontology.rdf.terms import bound_node, literal_datatype, literal_term

from .types import AllocationAssignment, AllocationRequest, text


def encode(value: object, operation: Budget) -> bytes:
    """Prepay conservative JSON encoding and retained bytes before serialization."""
    size = json_bound(value, operation)
    if size > operation.limits.max_bytes:
        raise LimitExceeded("Allocation artifact exceeds max_bytes")
    operation.consume("bytes", 3 * size)
    operation.consume("work", size)
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def assignment_data(item: AllocationAssignment, operation: Budget) -> list[object]:
    """Retain exact labels and caller keys without inventing term equivalence."""
    if not isinstance(item, AllocationAssignment):
        raise InputError("Expected AllocationAssignment")
    bound_node(item.iri, operation)
    request = item.request
    text(request.key, operation)
    text(request.kind, operation)
    if request.label is not None:
        bound_node(request.label, operation)
    label = (
        PatchLiteral(
            str(request.label),
            str(literal_datatype(request.label)),
            request.label.language,
        )
        if request.label is not None
        else None
    )
    operation.consume("states", 4)
    return [str(item.iri), request.key, request.kind, term_data(label, operation)]


def read_assignment(value: object, operation: Budget) -> AllocationAssignment:
    """Decode a finite assignment through the existing exact RDF term codec."""
    if not isinstance(value, list) or len(value) != 4:
        raise InputError("Malformed allocation assignment")
    for item in value[:3]:
        text(item, operation)
    label = read_term(value[3], operation)
    if label is not None and not isinstance(label, PatchLiteral):
        raise InputError("Allocation labels require literal records")
    if value[2] not in ("class", "concept", "scheme"):
        raise InputError("Unsupported allocated declaration")
    return AllocationAssignment(
        URIRef(string_value(value[0])),
        AllocationRequest(
            string_value(value[1]),
            cast(TypingLiteral["class", "concept", "scheme"], value[2]),
            literal_term(
                label.lexical, datatype=label.datatype, language=label.language
            )
            if label is not None
            else None,
        ),
    )
