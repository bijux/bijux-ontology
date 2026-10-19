"""Reuse existing role-aware OWL construction for finite datatype requests."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from rdflib import RDF

from bijux_ontology.declarations.types import FieldDeclaration
from bijux_ontology.errors import InputError, ResourceError
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.terms import bound_node, bound_text

from ..axioms.changes.encoding import ConstructionEncoder
from ..axioms.decoding import ProjectionDecoder
from ..axioms.types import NamedEntity
from .types import DatatypeFieldBranch, DatatypeObligation, DatatypeRequest


def _field_requests(
    field: FieldDeclaration,
    indices: Sequence[int],
    operation: Budget,
) -> list[DatatypeFieldBranch]:
    """Capture original branch ownership without manufacturing a filtered field."""
    if type(field) is not FieldDeclaration:
        raise InputError("Datatype fields require ordinary FieldDeclaration records")
    for text in (field.name, field.predicate, field.location):
        bound_text(text, operation)
    operation.consume("states", len(indices) * 4)
    operation.consume("bytes", 512 * len(indices))
    result = []
    for branch in indices:
        operation.consume("work")
        if type(branch) is not int or not 0 <= branch < len(field.values):
            raise InputError(
                "Datatype branch index must select an original field value"
            )
        value = field.values[branch]
        if value.datatype is not None:
            bound_text(value.datatype, operation)
        if isinstance(value.language, str):
            bound_text(value.language, operation)
        for sample in value.samples:
            bound_node(sample, operation)
        result.append(DatatypeFieldBranch(field, branch))
    return result


def capture_requests(
    raw: Sequence[DatatypeObligation]
    | Sequence[FieldDeclaration]
    | Sequence[DatatypeFieldBranch],
    operation: Budget,
    *,
    fields: bool,
    branches: bool = False,
) -> tuple[DatatypeRequest, ...]:
    """Capture all required branches before native work or import-map callbacks."""
    if not isinstance(raw, Sequence) or isinstance(cast(object, raw), str | bytes):
        raise InputError("Datatype requests require a finite typed sequence")
    size = len(raw)
    if not size:
        raise InputError("No required datatype requests were supplied")
    operation.consume("states", size)
    operation.consume("bytes", 64 + 128 * size)
    requests: list[DatatypeRequest] = []
    for index in range(size):
        operation.consume("work")
        item = raw[index]
        if branches:
            if type(item) is not DatatypeFieldBranch:
                raise InputError("Expected selected original datatype field branches")
            requests.extend(_field_requests(item.field, (item.branch,), operation))
        elif fields:
            if type(item) is not FieldDeclaration:
                raise InputError(
                    "Datatype fields require ordinary FieldDeclaration records"
                )
            requests.extend(_field_requests(item, range(len(item.values)), operation))
        else:
            if type(item) is not DatatypeObligation:
                raise InputError("Expected typed datatype obligations")
            bound_text(item.location, operation)
            if item.relation not in ("inclusion", "disjointness"):
                raise InputError(
                    "Choose inclusion or disjointness", location=item.location
                )
            operation.consume("states", 4)
            operation.consume("bytes", 512)
            requests.append(item)
    return tuple(requests)


def encode_request(request: DatatypeRequest, operation: Budget) -> dict[str, object]:
    """Validate data roles through the existing constructor and projection decoder."""
    encoder = ConstructionEncoder(operation)
    decoder = ProjectionDecoder(operation)
    if isinstance(request, DatatypeFieldBranch):
        value = request.field.values[request.branch]
        if value.kind != "literal" or request.field.reverse:
            raise ResourceError(
                "Only outgoing literal branches have this global range profile"
            )
        if value.constraints is not None or value.direction is not None:
            raise ResourceError(
                "Application value constraints are outside this OWL profile"
            )
        datatype = (
            str(RDF.langString) if isinstance(value.language, str) else value.datatype
        )
        if datatype is None or request.field.predicate.startswith("@"):
            raise ResourceError("The branch has no explicit datatype/data property")
        actual = encoder.node(NamedEntity("datatype", datatype))
        decoder.data_range(actual, 1)
        return {
            "kind": "field",
            "property": encoder.text(request.field.predicate, iri=True),
            "actual": actual,
        }
    actual, required = encoder.node(request.actual), encoder.node(request.required)
    decoder.data_range(actual, 1)
    decoder.data_range(required, 1)
    return {"kind": request.relation, "actual": actual, "required": required}
