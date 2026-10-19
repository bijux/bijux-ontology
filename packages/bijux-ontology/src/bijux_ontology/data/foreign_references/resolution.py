"""Resolve selected source keys against owned, explicitly scoped target indexes."""

from collections.abc import Sequence
from typing import Literal as Choice

from rdflib import BNode, Graph, URIRef

from bijux_ontology.data.keys.evidence import compare_keys, encode_evidence
from bijux_ontology.data.keys.extraction import _Key, _scope
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    bound_text,
)
from bijux_ontology.rdf.sources.metadata import SnapshotMetadata
from bijux_ontology.rdf.terms import bound_node, term_key

from .types import ForeignResolution, ForeignTargetIndex


def qualify_key(key: _Key, budget: Budget) -> Graph:
    """Requalify original physical selection, origin, fidelity and owned blanks."""
    reader = key.reader
    graph = reader.selected.read()
    current = SnapshotMetadata.capture(key.record.source, budget)
    if not reader.metadata.same_scope(current):
        raise InputError("Foreign key source metadata changed after key capture")
    if (
        isinstance(key.record.subject, BNode)
        and not reader.presence(key.record.subject, budget).owned
    ):
        raise InputError("Foreign key subject lost its exact snapshot ownership")
    reader.selected.read()
    return graph


def _retain(key: _Key, budget: Budget) -> None:
    """Qualify cached key metadata and original scope under each using operation."""
    qualify_key(key, budget)
    record = key.record
    bound_text(record.identifier, budget)
    bound_node(record.subject, budget)
    for value in key.values:
        bound_node(value, budget)
    name, _ = _scope(key.reader)
    if name is not None:
        bound_text(str(name), budget)
    for text in (key.reader.metadata.document_iri, key.reader.metadata.effective_url):
        if text is not None:
            bound_text(text, budget)
    for token in key.tokens:
        for text in token:
            if text is not None:
                bound_text(text, budget)


def _evidence(source: _Key, targets: Sequence[_Key], budget: Budget) -> str:
    """Bound correlated original terms before encoding required diagnostic evidence."""
    rows: list[dict[str, object]] = []
    allowance = 256
    budget.consume("states", len(targets) + 1)
    budget.consume("bytes", 64 + 32 * (len(targets) + 1))
    for role, key in (("source", source), *(("target", target) for target in targets)):
        _retain(key, budget)
        name, union = _scope(key.reader)
        allowance += 256 + 12 * sum(
            len(part or "")
            for token in (
                term_key(key.record.subject),
                *(term_key(value) for value in key.values),
            )
            for part in token
        )
        allowance += 12 * sum(
            len(text or "")
            for text in (
                key.record.identifier,
                str(name or ""),
                key.reader.metadata.document_iri,
                key.reader.metadata.effective_url,
            )
        )
        budget.consume("states", 10 + len(key.values))
        if allowance > budget.limits.max_bytes:
            raise LimitExceeded("Foreign reference evidence exceeds max_bytes")
        data = {
            "role": role,
            "record": key.record.identifier,
            "subject": term_key(key.record.subject),
            "document_iri": key.reader.metadata.document_iri,
            "effective_url": key.reader.metadata.effective_url,
            "graph": term_key(name) if isinstance(name, URIRef | BNode) else name,
            "union": union,
            "values": tuple(term_key(value) for value in key.values),
        }
        rows.append(data)
    return encode_evidence(rows, budget, allowance)


def resolve_reference(
    key: _Key,
    targets: ForeignTargetIndex,
    operation: Budget,
    issues: IssueCollector,
    resolutions: list[ForeignResolution],
    *,
    semantic: bool,
) -> tuple[ForeignResolution, list[_Key]]:
    """Resolve one original source key and retain its known findings immediately."""
    contract = targets.contract
    _retain(key, operation)
    if semantic or any(isinstance(value, BNode) for value in key.values):
        candidates = targets._keys
    else:
        operation.consume(
            "work",
            1 + sum(len(part or "") for token in key.tokens for part in token),
        )
        bucket = targets._buckets.get(key.tokens, ())
        operation.consume("states", len(bucket) + len(targets._uncertain))
        candidates = (*bucket, *targets._uncertain)
    matches: list[_Key] = []
    uncertain = False
    for target in candidates:
        operation.consume("work")
        _retain(target, operation)
        relation, pending = compare_keys(key, target, contract.source, operation)
        issues.extend(pending)
        if relation == "equal":
            operation.consume("states")
            matches.append(target)
        elif relation == "indeterminate":
            uncertain = True
    known = len(matches)
    status: Choice["resolved", "missing", "ambiguous", "unverified"] = "resolved"
    code = None
    if contract.maximum_targets is not None and known > contract.maximum_targets:
        status, code = "ambiguous", "data.foreign_ambiguous"
    elif (
        not targets.complete
        or uncertain
        or (known < contract.minimum_targets and contract.lookup == "open")
    ):
        status, code = "unverified", "data.foreign_lookup_unverified"
    elif known < contract.minimum_targets:
        status, code = "missing", "data.foreign_missing"
    operation.consume("states")
    operation.consume("bytes", 64 + 8 * (len(key.values) + known))
    resolution = ForeignResolution(
        key.record, key.values, tuple(match.record for match in matches), status
    )
    resolutions.append(resolution)
    if code is not None:
        issues.append(
            Issue(
                code,
                {
                    "missing": "Insufficient targets match this source key",
                    "ambiguous": "Too many targets match this source key",
                    "unverified": "Target lookup coverage is not established",
                }[status],
                key.record.identifier,
                kind="unverified" if status == "unverified" else "finding",
                evidence_json=_evidence(key, matches, operation),
            )
        )
    return resolution, matches
