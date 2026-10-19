"""Bounded inert diagnostic bytes with strict canonical truth reconstruction."""

from collections.abc import Iterator

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Report, report_from_data
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.json.encoding import json_bytes
from bijux_ontology.json.values import decode_json


def encode_report(report: Report, budget: Budget) -> bytes:
    """Prepay typed diagnostic ownership before canonical inert serialization."""
    pending = [(report, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > budget.limits.max_depth:
            raise LimitExceeded("Report exceeds max_depth")
        budget.consume("work", 1 + len(current.issues) + len(current.children))
        budget.consume("states", 32 + 16 * len(current.issues))
        budget.consume("bytes", 2048 + 1024 * len(current.issues))
        for text in _report_texts(current, budget):
            bound_text(text, budget)
            budget.consume("bytes", 16 * len(text) + 128)
        budget.consume("states", len(current.children))
        budget.consume("bytes", 64 + 32 * len(current.children))
        pending.extend((child, depth + 1) for child in current.children)
    return json_bytes(report.to_dict(), budget)


def _report_texts(report: Report, budget: Budget) -> Iterator[str]:
    """Yield existing text directly; pay integer-path conversion before allocating."""
    yield report.scope
    yield report.regime
    yield from report.families
    yield from report.exclusions
    for issue in report.issues:
        for text in (
            issue.code,
            issue.message,
            issue.location,
            issue.expected,
            issue.actual,
            issue.evidence_json,
        ):
            if text is not None:
                yield text
        details = issue.details
        if details is None:
            continue
        budget.consume("bytes", 4096)
        for part in details.path:
            if isinstance(part, int):
                bits = part.bit_length()
                if bits > min(4096, budget.limits.max_bytes):
                    raise LimitExceeded("Report path integer exceeds max_bytes")
                # The conservative decimal width avoids formatting first to
                # discover its cost. One conversion is live at a time.
                width = 1 + bits * 30103 // 100000
                budget.consume("work", width)
                budget.consume("bytes", 64 + 4 * width)
                yield str(part)
            else:
                yield part
        for text in (details.source, details.term, details.graph, details.model):
            if text is not None:
                yield text
        failure = details.failure
        if failure is not None:
            budget.consume("work", 1 + len(failure.import_chain))
            budget.consume("states", 8 + len(failure.import_chain))
            budget.consume("bytes", 1024 + 32 * len(failure.import_chain))
            yield failure.location
            yield failure.role
            yield from failure.import_chain
            if failure.document_iri is not None:
                yield failure.document_iri


def decode_report(raw: bytes, budget: Budget) -> Report:
    """Adopt caller-owned immutable bytes only after complete canonical validation.

    The caller already owns the input blob. Diagnostic reconstruction prepays
    its separate containers, scalar expansion and canonical verification before
    parsing; no partial report escapes if any admission or encoding step stops.
    """
    if type(raw) is not bytes or not raw:
        raise InputError("Reports require nonempty immutable JSON bytes")
    size = len(raw)
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Report exceeds max_bytes")
    budget.consume("states", size)
    budget.consume("work", size)
    budget.consume("bytes", (127 + 4 * budget.limits.max_depth) * size)
    try:
        report = report_from_data(
            decode_json(
                raw,
                max_bytes=budget.limits.max_bytes,
                max_depth=budget.limits.max_depth,
            )
        )
        if encode_report(report, budget) != raw:
            raise InputError("Noncanonical saved report")
    except LimitExceeded:
        raise
    except (ValueError, TypeError, RecursionError) as error:
        raise InputError("Saved report cannot be qualified") from error
    return report
