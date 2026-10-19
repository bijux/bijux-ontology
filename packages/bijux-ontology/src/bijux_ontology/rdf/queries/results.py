"""Exact bounded local query response admission independent of dispatch policy."""

from rdflib import BNode

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.json.values import decode_json
from bijux_ontology.rdf.queries.protocol import read_term

from .types import QueryResult, QueryScope


def adopt_result(
    raw: bytes,
    scope: QueryScope,
    limits: Limits,
    max_rows: int,
    *,
    budget: Budget | None = None,
) -> QueryResult:
    """Validate peer output before constructing a completed typed public answer."""
    response = decode_json(
        raw, max_bytes=limits.max_total_bytes, max_depth=limits.max_depth
    )
    if not isinstance(response, dict):
        raise ResourceError("Invalid local query response object")
    status = response.get("status")
    if status in ("unverified", "unavailable"):
        if set(response) != {"status", "message"} or not isinstance(
            response["message"], str
        ):
            raise ResourceError("Malformed query capability response")
        return QueryResult(
            Report(
                "rdf.query",
                requested=1,
                complete=False,
                issues=(
                    Issue(
                        "rdf.literal_conversion_unverified"
                        if status == "unverified"
                        else "rdf.query_unverified",
                        response["message"],
                        kind="unverified",
                    ),
                ),
            ),
            scope,
        )
    if status != "result":
        if not isinstance(status, str) or status not in {"input_error", "exhausted"}:
            raise ResourceError("Unrecognised local query response status")
        error = (
            LimitExceeded(str(response.get("message", "Query incomplete")))
            if status == "exhausted"
            else InputError(str(response.get("message", "Query input invalid")))
        )
        raise error
    kind, complete = response.get("kind"), response.get("complete")
    if (
        not isinstance(kind, str)
        or kind not in {"ASK", "SELECT"}
        or type(complete) is not bool
    ):
        raise ResourceError("Invalid local query completion evidence")
    if kind == "ASK":
        if set(response) != {"status", "kind", "answer", "complete"}:
            raise ResourceError("Invalid local ASK response fields")
        if type(response.get("answer")) is not bool or not complete:
            raise ResourceError("Invalid local ASK answer")
        return QueryResult(
            Report("rdf.query", 1, requested=1, families=("sparql-ask",)),
            scope,
            "ASK",
            response["answer"],
        )
    if set(response) != {"status", "kind", "variables", "rows", "complete", "message"}:
        raise ResourceError("Invalid local SELECT response fields")
    message = response["message"]
    if (
        not isinstance(message, str)
        or (complete and message)
        or (not complete and not message)
    ):
        raise ResourceError("Invalid local SELECT completion reason")
    variables, supplied = response.get("variables"), response.get("rows")
    if (
        not isinstance(variables, list)
        or len(variables) > limits.max_states
        or any(not isinstance(v, str) or not v for v in variables)
        or len(set(variables)) != len(variables)
    ):
        raise ResourceError("Invalid local SELECT variables")
    if not isinstance(supplied, list) or len(supplied) > max_rows:
        raise ResourceError("Invalid local SELECT row scope")
    blanks: dict[str, BNode] = {}
    rows = []
    budget = budget if budget is not None else Budget(limits)
    budget.consume("states", len(variables))
    for row in supplied:
        budget.consume("states", len(variables))
        if not isinstance(row, list) or len(row) != len(variables):
            raise ResourceError("Invalid local SELECT row width")
        rows.append(tuple(read_term(value, blanks) for value in row))
    issues = (
        ()
        if complete
        else (
            Issue(
                "execution.limit_exceeded",
                str(response.get("message", "SELECT result incomplete")),
                kind="exhausted",
            ),
        )
    )
    return QueryResult(
        Report(
            "rdf.query",
            1,
            issues,
            requested=1,
            complete=complete,
            families=("sparql-select",),
        ),
        scope,
        "SELECT",
        variables=tuple(variables),
        rows=tuple(rows),
    )
