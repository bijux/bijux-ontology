"""Maintained N-Triples 1.2 export with exact same-capture round-trip proof."""

from threading import Event

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.reports import Report

from .preparation import _execute, _failure
from .protocol import encode_quads
from .types import TRIPLE_TERM_CONTRACT, TripleTermExport, TripleTermSnapshot


def export_triple_terms(
    snapshot: TripleTermSnapshot,
    *,
    format: str = "nt-1.2",
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 10,
    cancel: Event | None = None,
) -> TripleTermExport:
    """Return exact qualified bytes, never write or flatten a described triple.

    Only the explicit N-Triples 1.2 format is supported. RDF 1.1 names such as
    'nt' are rejected. Snapshot reuse is requalified against the current budget.
    Export and reparse share captured blank tokens; this is no cross-document
    canonicalization or isomorphism claim. No backend Store is involved.
    """
    if not isinstance(snapshot, TripleTermSnapshot):
        raise InputError("Export requires a factory-owned triple-term snapshot")
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    content = None
    try:
        if not isinstance(format, str):
            raise InputError("Triple-term export requires an explicit supported format")
        bound_text(format, operation)
        if format != "nt-1.2":
            raise InputError("Triple terms require the nt-1.2 export format")
        for metadata in (
            snapshot.contract,
            snapshot.source_sha256,
            snapshot.document_iri,
            snapshot.effective_url,
        ):
            if metadata is not None:
                bound_text(metadata, operation)
        rows = encode_quads(snapshot.quads, operation)
        response = _execute({"action": "export", "rows": rows}, operation)
        value = response.get("content")
        if not isinstance(value, str):
            raise ResourceError("Missing triple-term export content")
        if len(value) > 2 * operation.limits.max_bytes or len(value) % 2:
            raise LimitExceeded("Triple-term export exceeds max_bytes")
        operation.consume("bytes", len(value) // 2)
        operation.consume("work", len(value) + 1)
        try:
            content = bytes.fromhex(value)
        except ValueError as error:
            raise ResourceError("Invalid triple-term export content") from error
    except RecursionError:
        _failure(
            issues,
            LimitExceeded("Triple-term nesting exceeds bounded protocol capacity"),
        )
    except (InputError, ResourceError, LimitExceeded) as error:
        _failure(issues, error)
    return TripleTermExport(
        content,
        Report(
            "rdf.triple_terms.export",
            int(content is not None),
            tuple(issues),
            requested=1,
            complete=content is not None,
            regime=TRIPLE_TERM_CONTRACT,
        ),
        format="nt-1.2",
    )
