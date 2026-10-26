"""Prepare reusable exact-key buckets from the existing streaming key extractor."""

from collections.abc import Sequence
from types import MappingProxyType
from typing import cast

from rdflib import BNode

from bijux_ontology.data.keys.evidence import compare_keys, key_evidence
from bijux_ontology.data.keys.extraction import _Key, _KeyExtraction
from bijux_ontology.data.keys.types import KeyRecord
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.rdf.value_comparison.observations import ScalarComparisons

from .types import ForeignReferenceContract, ForeignTargetIndex


def _operation(limits: Limits | None, budget: Budget | None) -> Budget:
    """Validate the same finite operation contract used by ordinary key checks."""
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("Expected explicit Limits")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Expected a shared Budget")
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("Foreign reference limits conflict with the shared Budget")
    return budget if budget is not None else Budget(limits)


def _records(records: Sequence[KeyRecord], complete: bool) -> int:
    """Require an explicit finite record selection and corpus coverage claim."""
    if not isinstance(records, Sequence) or isinstance(
        cast(object, records), str | bytes
    ):
        raise InputError("Foreign references require finite KeyRecord sequences")
    if type(complete) is not bool:
        raise InputError("Declare whether the supplied corpus is complete")
    return len(records)


def prepare_foreign_targets(
    records: Sequence[KeyRecord],
    contract: ForeignReferenceContract,
    *,
    complete: bool,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ForeignTargetIndex:
    """Keep proven target prefixes without claiming absent keys on partial lookup.

    Duplicate distinct records retain a separate target-key finding. Repeated
    identical record bindings are invalid inputs, following ordinary key checks.
    Value-regime comparisons remain maintained comparisons, never Python casts.
    """
    if not isinstance(contract, ForeignReferenceContract):
        raise InputError("Expected a typed ForeignReferenceContract")
    operation = _operation(limits, budget)
    count = _records(records, complete)
    issues = IssueCollector(operation)
    extraction = _KeyExtraction(records, contract.target, operation, issues, count)
    keys: list[_Key] = []
    buckets: dict[tuple[tuple[str | None, ...], ...], list[_Key]] = {}
    uncertain: list[_Key] = []
    duplicates: list[tuple[KeyRecord, KeyRecord]] = []
    finished = True
    anchor = ""
    semantic = any(c.equality != "rdf-term" for c in contract.target.components)
    try:
        comparisons = ScalarComparisons(operation)
        bound_text(contract.name, operation)
        anchor = contract.name
        bound_text(contract.target.name, operation)
        for key in extraction:
            blank = any(isinstance(value, BNode) for value in key.values)
            if semantic or blank:
                candidates = keys
            else:
                matches = buckets.get(key.tokens, ())
                operation.consume("states", len(matches) + len(uncertain))
                candidates = [*matches, *uncertain]
            for prior in candidates:
                relation, pending = compare_keys(
                    prior, key, contract.target, operation, comparisons=comparisons
                )
                issues.extend(pending)
                if relation == "equal":
                    operation.consume("states")
                    operation.consume("bytes", 64)
                    duplicates.append((prior.record, key.record))
                if relation == "equal" and contract.maximum_targets == 1:
                    issues.append(
                        Issue(
                            "data.foreign_target_duplicate",
                            "Distinct target records share the declared lookup key",
                            anchor,
                            evidence_json=key_evidence((prior, key), operation),
                        )
                    )
            operation.consume("states", 2)
            operation.consume("bytes", 128)
            keys.append(key)
            if blank:
                uncertain.append(key)
            else:
                buckets.setdefault(key.tokens, []).append(key)
        if not keys and extraction.exclusions:
            issues.append(
                Issue(
                    "data.foreign_lookup_unverified",
                    "No eligible target keys remain in the selected domain",
                    anchor,
                    kind="unverified",
                )
            )
        if not complete:
            issues.append(
                Issue(
                    "data.foreign_lookup_unverified",
                    "Target corpus is not declared complete",
                    anchor,
                    kind="unverified",
                )
            )
    except LimitExceeded as error:
        finished = False
        issues.exhausted(str(error), location=anchor)
    report = Report(
        "foreign-targets",
        extraction.checked,
        tuple(issues),
        requested=count,
        allow_empty=True,
        complete=finished,
        regime="application-foreign-keys",
        exclusions=tuple(extraction.exclusions),
    )
    result = object.__new__(ForeignTargetIndex)
    for name, value in (
        ("contract", contract),
        ("duplicates", tuple(duplicates)),
        ("report", report),
        (
            "complete",
            complete
            and finished
            and not any(
                i.kind in ("unverified", "input_error", "exhausted") for i in issues
            )
            and extraction.checked == count
            and len(keys) + len(extraction.exclusions) == count,
        ),
        ("_keys", tuple(keys)),
        (
            "_buckets",
            MappingProxyType(
                {token: tuple(values) for token, values in buckets.items()}
            ),
        ),
        ("_uncertain", tuple(uncertain)),
    ):
        object.__setattr__(result, name, value)
    return result
