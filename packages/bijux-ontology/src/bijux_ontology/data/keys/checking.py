"""Finite application uniqueness over explicitly associated RDF records."""

import json
from collections.abc import Sequence
from typing import cast

from rdflib import BNode, Literal, URIRef

from bijux_ontology.declarations.keys import KeyContract
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.terms import bound_text, term_key
from bijux_ontology.rdf.value_comparison.comparing import compare_literal_values
from bijux_ontology.reports import Issue, Report

from .extraction import _Key, _KeyExtraction, _scope
from .types import KeyRecord


def _canonical_evidence(
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


def _evidence(records: tuple[_Key, ...], budget: Budget) -> str:
    """Retain original values and source associations, never normalized claims."""
    data: list[dict[str, object]] = []
    allowance = 512
    for key in records:
        name, union = _scope(key.record)
        source = key.record.source
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
    return _canonical_evidence(data, budget, allowance)


def _relation(
    left: _Key, right: _Key, contract: KeyContract, budget: Budget
) -> tuple[str, tuple[Issue, ...]]:
    """A known unequal component defeats uncertainty in other components."""
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
            result = compare_literal_values(
                a, b, regime=component.equality, budget=budget
            )
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


def check_record_keys(
    records: Sequence[KeyRecord],
    contract: KeyContract,
    *,
    complete: bool,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> Report:
    """Check supplied application keys; a partial corpus never proves uniqueness.

    Rejecting absence/multiplicity produces findings. Explicit exclusions narrow
    the declared domain; unverified policies retain uncertainty. No key implies
    sameAs, and separate records never borrow missing values from one another.
    """
    if not isinstance(contract, KeyContract):
        raise InputError("Expected a typed KeyContract")
    if not isinstance(records, Sequence) or isinstance(
        cast(object, records), str | bytes
    ):
        raise InputError("Key records require a finite typed sequence")
    if type(complete) is not bool:
        raise InputError("Declare whether the selected key corpus is complete")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Expected a shared Budget")
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("Expected explicit Limits")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Explicit key limits conflict with the shared Budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    finished = True
    count = len(records)
    extraction = _KeyExtraction(records, contract, operation, issues, count)
    anchor = ""
    try:
        bound_text(contract.name, operation)
        anchor = contract.name
        keys: list[_Key] = []
        groups: dict[tuple[tuple[str | None, ...], ...], list[_Key]] = {}
        uncertain_keys: list[_Key] = []
        semantic = any(c.equality != "rdf-term" for c in contract.components)
        for key in extraction:
            has_blank = any(isinstance(value, BNode) for value in key.values)
            if semantic or has_blank:
                candidates = keys
            else:
                operation.consume(
                    "work",
                    sum(len(part or "") for token in key.tokens for part in token) + 1,
                )
                matches = groups.get(key.tokens, ())
                operation.consume("states", len(matches) + len(uncertain_keys))
                candidates = [*matches, *uncertain_keys]
            for prior in candidates:
                relation, pending = _relation(prior, key, contract, operation)
                if relation == "different":
                    continue
                issues.extend(pending)
                issues.append(
                    Issue(
                        "data.key_collision"
                        if relation == "equal"
                        else "data.key_comparison_unverified",
                        "Selected records share the declared compound key"
                        if relation == "equal"
                        else "Selected record keys cannot be distinguished",
                        contract.name,
                        kind="finding" if relation == "equal" else "unverified",
                        evidence_json=_evidence((prior, key), operation),
                    )
                )
            operation.consume("states", 2)
            keys.append(key)
            if has_blank:
                uncertain_keys.append(key)
            else:
                groups.setdefault(key.tokens, []).append(key)
        if not keys and extraction.exclusions:
            issues.append(
                Issue(
                    "data.key_corpus_unverified",
                    "No eligible record keys remain in the declared domain",
                    contract.name,
                    kind="unverified",
                )
            )
        if not complete:
            issues.append(
                Issue(
                    "data.key_corpus_unverified",
                    "The supplied record corpus is not declared complete",
                    contract.name,
                    kind="unverified",
                )
            )
    except LimitExceeded as error:
        finished = False
        issues.exhausted(str(error), location=anchor)
    return Report(
        "record-keys",
        extraction.checked,
        tuple(issues),
        requested=count,
        complete=finished,
        exclusions=tuple(extraction.exclusions),
        regime="application-keys",
        families=("application-keys",),
    )
