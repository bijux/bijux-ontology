"""Capture finite caller inventories before invoking the optional native processor."""

import json
import math
from collections.abc import Sequence

from rdflib import Literal

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.rdf.terms import bound_node

from .types import GeometryValue, SpatialDistance, SpatialRelation


def prepare_request(
    geometries: Sequence[GeometryValue],
    relations: Sequence[SpatialRelation],
    distances: Sequence[SpatialDistance],
    budget: Budget,
) -> tuple[
    list[dict[str, object]],
    tuple[SpatialRelation, ...],
    tuple[SpatialDistance, ...],
    bytes,
]:
    """Own all source metadata and contracts before native or consumer callbacks."""
    for inventory in (geometries, relations, distances):
        candidate: object = inventory
        if not isinstance(candidate, Sequence) or isinstance(candidate, str | bytes):
            raise InputError("Geometry contracts require finite sequences")
        budget.consume("bytes", 1024 + 1024 * len(inventory))
        budget.consume("work", len(inventory) + 1)
    if not geometries:
        raise InputError("Geometry checks require a nonempty supplied value inventory")
    values: list[dict[str, object]] = []
    keys: set[str] = set()
    for original in geometries:
        budget.consume("states")
        if type(original) is not GeometryValue or type(original.literal) is not Literal:
            raise InputError("Expected ordinary GeometryValue and RDF Literal records")
        key, literal, location = original.key, original.literal, original.location
        if type(key) is not str or not key or type(location) is not str:
            raise InputError(
                "Geometry keys must be nonempty strings with string locations"
            )
        bound_text(key, budget)
        bound_text(location, budget)
        bound_node(literal, budget)
        if key in keys:
            raise InputError("Geometry keys must be unique")
        keys.add(key)
        # Scalar capture precedes any external callback; returned terms are defensive.
        values.append(
            {
                "key": key,
                "lexical": str(literal),
                "datatype": str(literal.datatype)
                if literal.datatype is not None
                else None,
                "language": literal.language,
                "location": location,
            }
        )
    selected_relations: list[SpatialRelation] = []
    selected_distances: list[SpatialDistance] = []
    for inventory in (relations, distances):
        for request in inventory:
            budget.consume("states")
            if type(request) not in (SpatialRelation, SpatialDistance):
                raise InputError("Expected exact spatial contract records")
            for text in (request.left, request.right, request.location):
                if type(text) is not str:
                    raise InputError("Spatial references and locations require strings")
                bound_text(text, budget)
            if request.left not in keys or request.right not in keys:
                raise InputError(
                    "Spatial obligations must reference supplied geometry keys"
                )
            if isinstance(request, SpatialRelation):
                relation_name: object = request.relation
                if (
                    inventory is not relations
                    or type(relation_name) is not str
                    or relation_name
                    not in (
                        "within",
                        "contains",
                        "intersects",
                        "disjoint",
                        "topological_equals",
                    )
                    or type(request.expected) is not bool
                ):
                    raise InputError(
                        "Unsupported or malformed spatial relation contract"
                    )
                selected_relations.append(request)
            else:
                if (
                    inventory is not distances
                    or type(request.maximum) is not float
                    or not math.isfinite(request.maximum)
                    or request.maximum < 0
                    or type(request.unit) is not str
                    or not request.unit
                ):
                    raise InputError(
                        "Distance needs a finite float maximum and explicit unit"
                    )
                bound_text(request.unit, budget)
                selected_distances.append(request)
    budget.consume("bytes", 512 * (len(relations) + len(distances) + 1))
    relation_data = [
        {"left": r.left, "right": r.right, "relation": r.relation}
        for r in selected_relations
    ]
    distance_data = [
        {"left": r.left, "right": r.right, "unit": r.unit} for r in selected_distances
    ]
    request_data = {
        "protocol": 1,
        "operation": "geosparql",
        "geometries": values,
        "relations": relation_data,
        "distances": distance_data,
    }
    size = json_bound(request_data, budget)
    budget.consume("bytes", 2 * size)
    payload = json.dumps(request_data, ensure_ascii=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return values, tuple(selected_relations), tuple(selected_distances), payload
