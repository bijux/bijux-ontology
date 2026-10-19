"""Resolve selected source keys against owned, explicitly scoped target indexes."""

from collections.abc import Sequence
from typing import Literal as Choice

from rdflib import BNode, Graph, URIRef

from bijux_ontology.data.keys.evidence import compare_keys, encode_evidence
from bijux_ontology.data.keys.extraction import _Key, _KeyExtraction, _scope
from bijux_ontology.data.keys.types import KeyRecord
from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.rdf.terms import bound_node, bound_text, term_key
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.view import Vocabulary

from .preparation import _operation, _records
from .types import ForeignReferenceResult, ForeignResolution, ForeignTargetIndex


def _retain(key: _Key, budget: Budget) -> None:
    """Qualify cached key metadata and original scope under each using operation."""
    record = key.record
    bound_text(record.identifier, budget)
    bound_node(record.subject, budget)
    for value in key.values:
        bound_node(value, budget)
    name, _ = _scope(record)
    if name is not None:
        bound_text(str(name), budget)
    for text in (record.source.document_iri, record.source.effective_url):
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
        name, union = _scope(key.record)
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
                key.record.source.document_iri,
                key.record.source.effective_url,
            )
        )
        budget.consume("states", 10 + len(key.values))
        if allowance > budget.limits.max_bytes:
            raise LimitExceeded("Foreign reference evidence exceeds max_bytes")
        data = {
            "role": role,
            "record": key.record.identifier,
            "subject": term_key(key.record.subject),
            "document_iri": key.record.source.document_iri,
            "effective_url": key.record.source.effective_url,
            "graph": term_key(name) if isinstance(name, URIRef | BNode) else name,
            "union": union,
            "values": tuple(term_key(value) for value in key.values),
        }
        rows.append(data)
    return encode_evidence(rows, budget, allowance)


def check_foreign_references(
    records: Sequence[KeyRecord],
    targets: ForeignTargetIndex,
    *,
    source_complete: bool,
    models: Sequence[ModelDeclaration] = (),
    vocabulary: Vocabulary | Graph | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ForeignReferenceResult:
    """Join actual key values; open/incomplete lookup cannot establish absence.

    Exact keys use reusable buckets. Explicit semantic regimes compare selected
    literals through the maintained value comparator. Blank keys correspond only
    inside their one owned snapshot. Optional target contracts run on the actual
    selected target snapshots, never reconstructed resource labels or graphs.
    """
    if not isinstance(targets, ForeignTargetIndex):
        raise InputError("Expected a prepared ForeignTargetIndex")
    operation = _operation(limits, budget)
    count = _records(records, source_complete)
    issues = IssueCollector(operation)
    contract = targets.contract
    extraction = _KeyExtraction(records, contract.source, operation, issues, count)
    resolutions: list[ForeignResolution] = []
    children: list[Report] = []
    finished = True
    anchor = ""
    semantic = any(c.equality != "rdf-term" for c in contract.source.components)
    try:
        bound_text(contract.name, operation)
        anchor = contract.name
        bound_text(contract.source.name, operation)
        bound_text(contract.target.name, operation)
        for number in (contract.minimum_targets, contract.maximum_targets):
            if number is not None:
                if number.bit_length() > min(4096, operation.limits.max_bytes):
                    raise LimitExceeded("Foreign target count exceeds metadata limits")
                bound_text(str(number), operation)
        if contract.target_model is not None:
            bound_text(contract.target_model, operation)
        retain_report(targets.report, operation, issues)
        operation.consume("states")
        children.append(targets.report)
        for key in extraction:
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
                relation, pending = compare_keys(
                    key, target, contract.source, operation
                )
                issues.extend(pending)
                if relation == "equal":
                    operation.consume("states")
                    matches.append(target)
                elif relation == "indeterminate":
                    uncertain = True
            known = len(matches)
            status: Choice["resolved", "missing", "ambiguous", "unverified"] = (
                "resolved"
            )
            code = None
            if (
                contract.maximum_targets is not None
                and known > contract.maximum_targets
            ):
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
            resolutions.append(
                ForeignResolution(
                    key.record,
                    key.values,
                    tuple(match.record for match in matches),
                    status,
                )
            )
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
            if status == "resolved" and contract.target_model is not None:
                for target in matches:
                    record = target.record
                    report = check_rdf_emission(
                        record.source,
                        models,
                        (
                            RdfModelUsage(
                                contract.target_model,
                                record.subject,
                                location=record.identifier,
                            ),
                        ),
                        vocabulary=vocabulary,
                        graph_name=record.graph_name,
                        union=record.union,
                        budget=operation,
                    )
                    retain_report(report, operation, issues)
                    operation.consume("states")
                    children.append(report)
        if not resolutions and extraction.exclusions:
            issues.append(
                Issue(
                    "data.foreign_source_unverified",
                    "No eligible source keys remain in the selected domain",
                    anchor,
                    kind="unverified",
                )
            )
        if not source_complete:
            issues.append(
                Issue(
                    "data.foreign_source_unverified",
                    "Source record corpus is not declared complete",
                    anchor,
                    kind="unverified",
                )
            )
    except LimitExceeded as error:
        finished = False
        issues.exhausted(str(error), location=anchor)
    report = Report(
        "foreign-references",
        extraction.checked,
        tuple(issues),
        requested=count,
        complete=finished,
        regime="application-foreign-keys",
        exclusions=tuple(extraction.exclusions),
        children=tuple(children),
    )
    return ForeignReferenceResult(report, tuple(resolutions))
