"""Shared key-value comparisons and original source-correlated evidence."""

import json

from rdflib import BNode, Literal, URIRef

from bijux_ontology.declarations.keys import KeyContract
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import Budget
from bijux_ontology.rdf.terms import term_key
from bijux_ontology.rdf.value_comparison.observations import ScalarComparisons

from .extraction import _Key, _scope


def encode_evidence(
    records: list[dict[str, object]], budget: Budget, allowance: int
) -> str:
    """Reserve the same ASCII JSON representation retained by Issue evidence."""
    if allowance > budget.limits.max_bytes:
        raise LimitExceeded("Key evidence exceeds max_bytes")
    budget.consume("work", allowance)
    budget.consume("bytes", allowance)
    text = json.dumps(
        {"records": records}, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    if len(text) > budget.limits.max_bytes:
        raise LimitExceeded("Key evidence exceeds max_bytes")
    return text


def key_evidence(records: tuple[_Key, ...], budget: Budget) -> str:
    """Retain original values and source associations, never normalized claims."""
    data: list[dict[str, object]] = []
    allowance = 512
    for key in records:
        name, union = _scope(key.reader)
        source = key.reader.metadata
        texts = (
            key.record.identifier,
            str(name or ""),
            source.document_iri or "",
            source.effective_url or "",
        )
        allowance += 12 * sum(len(text) for text in texts)
        allowance += 12 * sum(
            len(part or "")
            for value in (*key.values, key.record.subject)
            for part in term_key(value)
        )
        budget.consume("states", 10 + len(key.values))
        data.append(
            {
                "identifier": key.record.identifier,
                "source": {
                    "snapshot": key.source_index,
                    "document_iri": source.document_iri,
                    "effective_url": source.effective_url,
                },
                "subject": term_key(key.record.subject),
                "graph": term_key(name) if isinstance(name, URIRef | BNode) else name,
                "union": union,
                "values": [term_key(value) for value in key.values],
            }
        )
    return encode_evidence(data, budget, allowance)


def compare_keys(
    left: _Key,
    right: _Key,
    contract: KeyContract,
    budget: Budget,
    *,
    comparisons: ScalarComparisons,
) -> tuple[str, tuple[Issue, ...]]:
    """A known unequal component defeats uncertainty in other components."""
    if comparisons.budget is not budget:
        raise InputError("Scalar comparisons belong to a different Budget")
    uncertain = False
    pending: list[Issue] = []
    for index, component in enumerate(contract.components):
        budget.consume("work")
        a, b = left.values[index], right.values[index]
        if isinstance(a, BNode) or isinstance(b, BNode):
            if not isinstance(a, BNode) or not isinstance(b, BNode):
                return "different", ()
            if left.record.source is not right.record.source:
                uncertain = True
                continue
            if a != b:
                return "different", ()
        elif (
            component.equality == "rdf-term"
            or not isinstance(a, Literal)
            or not isinstance(b, Literal)
        ):
            budget.consume(
                "work",
                sum(
                    len(part or "")
                    for token in (left.tokens[index], right.tokens[index])
                    for part in token
                )
                + 1,
            )
            if left.tokens[index] != right.tokens[index]:
                return "different", ()
        else:
            result = comparisons.compare(a, b, regime=component.equality)
            if result.report.outcome == "exhausted":
                raise LimitExceeded(
                    "Literal key comparison exhausted its shared budget"
                )
            if result.relation == "different":
                return "different", ()
            if result.relation != "equal":
                uncertain = True
                pending.extend(result.report.issues)
    return ("indeterminate" if uncertain else "equal"), tuple(pending)
