"""Finite application uniqueness over explicitly associated RDF records."""

from collections.abc import Sequence
from typing import cast

from rdflib import BNode

from bijux_ontology.declarations.keys import KeyContract
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits, bound_text

from .evidence import compare_keys, key_evidence
from .extraction import _Key, _KeyExtraction
from .types import KeyRecord


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
                relation, pending = compare_keys(prior, key, contract, operation)
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
                        evidence_json=key_evidence((prior, key), operation),
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
