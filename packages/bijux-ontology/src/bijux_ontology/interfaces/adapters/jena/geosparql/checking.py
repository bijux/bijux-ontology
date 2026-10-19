"""Qualify actual local Jena geometry receipts without manufacturing spatial truth."""

import math
from collections.abc import Sequence
from threading import Event
from typing import cast

from bijux_ontology._json import decode_json, object_value
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, IssueCollector, Limits, bound_text

from ..runtime import invoke_jena
from ..types import JenaBackend
from .preparation import prepare_request
from .types import (
    GeometryCheck,
    GeometryOutcome,
    GeometryValue,
    SpatialDistance,
    SpatialOutcome,
    SpatialRelation,
)


def _rows(response: dict[str, object], name: str, maximum: int) -> list[object]:
    """Require an ordered complete prefix, never an invented skipped receipt."""
    value = response.get(name)
    if not isinstance(value, list) or len(value) > maximum:
        raise ResourceError("Jena omitted the bounded spatial receipt inventory")
    return value


def _row(value: object, index: int) -> dict[str, object]:
    """Reject reordered, repeated and malformed native evidence identities."""
    row = object_value(value, location="Jena spatial receipt")
    if type(row.get("index")) is not int or row["index"] != index:
        raise ResourceError(
            "Jena spatial receipt did not identify its exact obligation"
        )
    return row


def _report(
    scope: str,
    row: dict[str, object],
    issues: IssueCollector,
    operation: Operation,
    location: str,
    agrees: bool | None,
) -> Report:
    """Pay a diagnostic before attaching the corresponding per-obligation report."""
    status = row.get("status")
    if status not in ("result", "invalid_lexical", "invalid_geometry", "unavailable"):
        raise ResourceError("Jena returned an unsupported spatial receipt status")
    complete = status != "unavailable"
    issue: Issue | None = None
    if agrees is False or not complete:
        message = row.get("message", "Selected spatial obligation does not hold")
        if type(message) is not str:
            raise ResourceError("Jena spatial receipt has malformed diagnostics")
        bound_text(message, operation)
        issue = Issue(
            "geospatial." + (str(status) if status != "result" else "relation"),
            message,
            location=location,
            kind="finding" if complete else "unverified",
        )
        issues.append(issue)
    return Report(
        scope,
        int(complete),
        () if issue is None else (issue,),
        requested=1,
        complete=complete,
    )


def _geometry(
    source: dict[str, object],
    row: dict[str, object],
    issues: IssueCollector,
    operation: Operation,
) -> GeometryOutcome:
    """Keep invalid lexical/value evidence separate from unknown interpretation."""
    status = row.get("status")
    lexical, valid = row.get("lexical_valid"), row.get("geometry_valid")
    if any(
        value is not None and type(value) is not bool
        for value in (lexical, valid, row.get("empty"))
    ):
        raise ResourceError("Jena geometry validity metadata is malformed")
    if (
        status == "result"
        and (lexical is not True or valid is not True)
        or status == "invalid_lexical"
        and (lexical is not False or valid is not None)
        or status == "invalid_geometry"
        and (lexical is not True or valid is not False)
    ):
        raise ResourceError(
            "Jena geometry status contradicts its actual validity receipt"
        )
    crs, axis, dimensions, empty = (
        row.get("crs"),
        row.get("axis_order"),
        row.get("dimensions"),
        row.get("empty"),
    )
    if status in ("result", "invalid_geometry"):
        expected_axis = {
            "http://www.opengis.net/def/crs/OGC/1.3/CRS84": "longitude_latitude",
            "http://www.opengis.net/def/crs/EPSG/0/4326": "latitude_longitude",
        }
        if (
            not isinstance(crs, str)
            or crs not in expected_axis
            or axis != expected_axis[crs]
            or type(dimensions) is not int
            or dimensions != 2
            or type(empty) is not bool
        ):
            raise ResourceError(
                "Jena geometry omitted the qualified CRS/axis/dimension receipt"
            )
    elif any(value is not None for value in (crs, axis, dimensions, empty)):
        raise ResourceError(
            "Jena unavailable geometry supplied an unqualified interpretation"
        )
    report = _report(
        "geospatial.value",
        row,
        issues,
        operation,
        cast(str, source["location"]),
        status == "result" if status != "unavailable" else None,
    )
    return GeometryOutcome(
        cast(str, source["key"]),
        cast(str, source["lexical"]),
        cast(str | None, source["datatype"]),
        cast(str | None, source["language"]),
        cast(str, source["location"]),
        cast(bool | None, lexical),
        cast(bool | None, valid),
        cast(str | None, crs),
        cast(str | None, axis),
        cast(int | None, dimensions),
        cast(bool | None, empty),
        report,
    )


def check_geometries(
    backend: JenaBackend,
    geometries: Sequence[GeometryValue],
    relations: Sequence[SpatialRelation] = (),
    distances: Sequence[SpatialDistance] = (),
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> GeometryCheck:
    """Check a finite GeoSPARQL1.0/Jena6.2 subset with exact original literals.

    Two-dimensional WKT Point/LineString/Polygon/empty and qualified GML3.2
    Point/pos use maintained native parsing. Only locally bundled CRS84 and
    EPSG4326 are supported, including their distinct axis orders; WKT's default
    CRS84 is reported. Unknown CRS/resources/serializations remain unknown.
    Distance is explicitly planar angular degrees, never inferred metres or a
    geodesic measurement. Topological equality is separate from RDF identity.
    Lexical/geometry failure is completed negative evidence; dependent relations
    remain unavailable. No broad GeoSPARQL1.1, network or Windows claim is made.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    values: list[GeometryOutcome] = []
    relation_results: list[SpatialOutcome] = []
    distance_results: list[SpatialOutcome] = []
    children: list[Report] = []
    requested = 1
    backend_identity: str | None = None
    complete = False
    try:
        sources, selected_relations, selected_distances, payload = prepare_request(
            geometries, relations, distances, operation
        )
        requested = len(sources) + len(selected_relations) + len(selected_distances)
        response = object_value(
            decode_json(invoke_jena(backend, payload, operation, heap_mb)),
            location="Jena geometry response",
        )
        identity = response.get("backend")
        if type(identity) is not str:
            raise ResourceError("Jena geometry response omitted its runtime identity")
        bound_text(identity, operation)
        backend_identity = identity
        for index, raw in enumerate(_rows(response, "geometries", len(sources))):
            operation.consume("work")
            outcome = _geometry(sources[index], _row(raw, index), issues, operation)
            children.append(outcome.report)
            values.append(outcome)
        source_by_key = {cast(str, source["key"]): source for source in sources}
        for name, obligations, output in (
            ("relations", selected_relations, relation_results),
            ("distances", selected_distances, distance_results),
        ):
            for index, raw in enumerate(_rows(response, name, len(obligations))):
                operation.consume("work")
                row = _row(raw, index)
                obligation = obligations[index]
                status, value = row.get("status"), row.get("value")
                if status == "result":
                    if isinstance(obligation, SpatialRelation):
                        if type(value) is not bool:
                            raise ResourceError(
                                "Jena relation result must be a boolean"
                            )
                        agrees = value == obligation.expected
                    else:
                        if (
                            type(value) not in (int, float)
                            or not math.isfinite(cast(float, value))
                            or cast(float, value) < 0
                        ):
                            raise ResourceError(
                                "Jena distance must be a finite nonnegative number"
                            )
                        value = float(cast(float, value))
                        agrees = value <= obligation.maximum
                elif status == "unavailable" and value is None:
                    agrees = None
                else:
                    raise ResourceError(
                        "Jena spatial result contradicted its completion status"
                    )
                report = _report(
                    "geospatial." + name,
                    row,
                    issues,
                    operation,
                    obligation.location,
                    agrees,
                )
                children.append(report)
                left, right = (
                    source_by_key[obligation.left],
                    source_by_key[obligation.right],
                )
                same_term = all(
                    left[field] == right[field]
                    for field in ("lexical", "datatype", "language")
                )
                output.append(SpatialOutcome(obligation, value, same_term, report))
        if response.get("status") == "exhausted":
            raise LimitExceeded("Jena geometry wrapper exhausted its bounded execution")
        if response.get("status") != "result":
            raise ResourceError("Jena geometry operation did not complete")
        if (
            len(values) != len(sources)
            or len(relation_results) != len(selected_relations)
            or len(distance_results) != len(selected_distances)
        ):
            raise ResourceError("Jena geometry response omitted required obligations")
        complete = all(child.complete for child in children)
    except LimitExceeded as error:
        issues.exhausted(str(error), location="geospatial")
    except (InputError, OSError) as error:
        try:
            unavailable = isinstance(error, ResourceError | OSError)
            issues.append(
                Issue(
                    "geospatial.unavailable"
                    if unavailable
                    else "geospatial.input_error",
                    str(error),
                    kind="unverified" if unavailable else "input_error",
                )
            )
        except LimitExceeded as stopped:
            issues.exhausted(str(stopped), location="geospatial")
    return GeometryCheck(
        tuple(values),
        tuple(relation_results),
        tuple(distance_results),
        backend_identity,
        Report(
            "geospatial",
            sum(child.checked for child in children),
            tuple(issues),
            requested=requested,
            complete=complete,
            children=tuple(children),
        ),
    )
