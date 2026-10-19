"""Fixed-universe annotation enrichment delegated to a maintained numerical worker."""

import json
import math
import time
from collections.abc import Sequence
from dataclasses import asdict
from threading import Event
from typing import cast

from rdflib import URIRef

from bijux_ontology._json import decode_json, object_value
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.limits import Budget, BudgetKind, IssueCollector, Limits
from bijux_ontology.rdf.execution import execute_worker
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.relation_paths.automaton import named

from .population import capture, fingerprint, ordered, qualify, reserve, text
from .types import (
    AnnotationPopulation,
    EnrichmentCounts,
    EnrichmentIdentity,
    EnrichmentRead,
    EnrichmentTerm,
)

_KINDS: tuple[BudgetKind, ...] = (
    "bytes",
    "resources",
    "triples",
    "work",
    "issues",
    "states",
)


def _native(
    counts: list[EnrichmentCounts], correction: str, operation: Operation
) -> dict[str, object]:
    """Share host controls and adopt actual worker receipts before native values."""
    reserve(operation, len(counts) * 16 + 64)
    capacity = 4096 + 256 * len(counts)
    if capacity > operation.limits.max_bytes:
        raise LimitExceeded("Numerical family exceeds the owned protocol byte bound")
    # Prepay bounded input/output pipes, decoding and owned response inventories.
    operation.consume("bytes", capacity * 6)
    operation.consume("work", capacity * 2)
    request: dict[str, object] = {
        "tables": [[list(row) for row in item.table] for item in counts],
        "correction": correction,
        "limits": asdict(operation.limits),
        "allowances": {kind: operation.remaining(kind) for kind in _KINDS},
    }
    size = json_bound(request, operation)
    if size > capacity:
        raise LimitExceeded("Numerical request exceeds the owned protocol byte bound")
    operation.consume("bytes", size)
    allowances = {kind: operation.remaining(kind) for kind in _KINDS}
    request["allowances"] = allowances
    payload = json.dumps(request, allow_nan=False, separators=(",", ":")).encode(
        "ascii"
    )
    remaining = operation.deadline - time.monotonic()
    if remaining <= 0:
        raise LimitExceeded("Annotation enrichment exceeded its wall-clock deadline")
    raw = execute_worker(
        "bijux_ontology.data.analysis.worker",
        payload,
        cap=capacity,
        timeout=remaining,
        cancel=operation.cancel,
    )
    operation.consume("work")
    response = object_value(
        decode_json(raw, max_bytes=capacity, max_depth=16), location="/"
    )
    consumed = object_value(response.get("consumed"), location="/consumed")
    if set(consumed) != set(_KINDS):
        raise InputError("Malformed numerical control receipt")
    for kind in _KINDS:
        amount = consumed[kind]
        if type(amount) is not int or not 0 <= amount <= allowances[kind]:
            raise InputError("Numerical worker exceeded its shared control allowance")
        operation.consume(kind, amount)
    return response


def enrich_annotations(
    population: AnnotationPopulation,
    study: Sequence[str],
    *,
    family: Sequence[URIRef],
    family_id: str,
    correction: str,
    dependence: str,
    outside_background: str = "reject",
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> EnrichmentRead:
    """Test the whole prespecified family against the unchanged eligible universe.

    The one-sided Fisher alternative is greater. BY declares arbitrary dependence;
    BH requires independent-or-PRDS declaration. Fixed-margin sampling assumptions
    concern the supplied snapshot and are not independently established. No score,
    p-value or adjusted value establishes ontology truth or a biological verdict.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    counts: list[EnrichmentCounts] = []
    tests: tuple[EnrichmentTerm, ...] = ()
    excluded: tuple[str, ...] = ()
    unannotated: tuple[str, ...] = ()
    identity = None
    complete = False
    requested = 1
    try:
        qualify(population, operation, issues)
        text(family_id, operation)
        for value in (correction, dependence, outside_background):
            text(value, operation)
        if (
            correction not in ("by", "bh")
            or dependence not in ("arbitrary", "independent-or-prds")
            or outside_background not in ("reject", "exclude")
        ):
            raise InputError(
                "Declare supported correction, dependence and outside-background policy"
            )
        if correction == "bh" and dependence != "independent-or-prds":
            raise InputError("BH requires explicit independent-or-PRDS declaration")
        captured_family = capture(family, URIRef, operation)
        captured_study = capture(study, str, operation)
        if not captured_family or not captured_study:
            raise InputError(
                "Enrichment requires a nonempty prespecified family and study"
            )
        reserve(operation, len(population.background))
        eligible = frozenset(population.background)
        selected = []
        seen = set()
        for term in captured_family:
            if type(term) is not URIRef:
                raise InputError("Prespecified terms require named URIRef identities")
            named(term, operation)
            reserve(operation)
            if term not in population.members:
                issues.append(
                    Issue(
                        "analysis.family_unknown",
                        "Prespecified term lies outside the qualified hierarchy",
                        str(term),
                        kind="unverified",
                    )
                )
            if term not in seen:
                seen.add(term)
                selected.append(term)
        requested = len(selected)
        actual_study: set[str] = set()
        outside: set[str] = set()
        for entity in captured_study:
            text(entity, operation)
            reserve(operation)
            (actual_study if entity in eligible else outside).add(entity)
        excluded = ordered(outside, operation)
        if outside and outside_background == "reject":
            raise InputError(
                "Study entities lie outside the supplied eligible background"
            )
        if not actual_study:
            raise InputError(
                "No study entity remains in the supplied eligible background"
            )
        reserve(operation, len(population.unannotated) + len(actual_study))
        unannotated = ordered(
            actual_study.intersection(population.unannotated), operation
        )
        if issues:
            return EnrichmentRead(
                (),
                (),
                None,
                excluded,
                unannotated,
                Report(
                    "analysis.enrichment",
                    requested=requested,
                    complete=False,
                    issues=tuple(issues),
                ),
            )
        N, n = len(eligible), len(actual_study)
        if N > 2**63 - 1:
            issues.append(
                Issue(
                    "analysis.native_capacity",
                    "Eligible counts exceed the qualified native integer range",
                    kind="unverified",
                )
            )
        else:
            reserve(operation, len(selected) * 8)
            for term in selected:
                members = population.members[term]
                operation.consume("work", len(actual_study))
                a = sum(entity in members for entity in actual_study)
                K = len(members)
                counts.append(
                    EnrichmentCounts(
                        term, a, n, K, N, ((a, n - a), (K - a, N - n - K + a))
                    )
                )
            study_order = ordered(actual_study, operation)
            study_identity = fingerprint(
                tuple((entity,) for entity in study_order), operation
            )
            family_identity = fingerprint(
                tuple((str(term),) for term in selected), operation
            )
            native = _native(counts, correction, operation)
            status = native.get("status")
            if status == "complete":
                if set(native) != {
                    "status",
                    "raw",
                    "adjusted",
                    "scipy",
                    "numpy",
                    "consumed",
                }:
                    raise InputError("Malformed completed numerical response")
                scipy_version = text(cast(str, native["scipy"]), operation)
                numpy_version = text(cast(str, native["numpy"]), operation)
                values = []
                for key in ("raw", "adjusted"):
                    probabilities = native[key]
                    if (
                        type(probabilities) is not list
                        or len(probabilities) != requested
                    ):
                        raise InputError(
                            "Numerical response omitted required family coverage"
                        )
                    reserve(operation, requested)
                    for value in probabilities:
                        if (
                            type(value) is not float
                            or not math.isfinite(value)
                            or not 0 <= value <= 1
                        ):
                            raise InputError(
                                "Numerical output requires finite double probabilities"
                            )
                    values.append(probabilities)
                reserve(operation, requested)
                identity = EnrichmentIdentity(
                    population.identity,
                    study_identity,
                    family_id,
                    family_identity,
                    correction,
                    dependence,
                    outside_background,
                    scipy_version,
                    numpy_version,
                )
                tests = tuple(
                    EnrichmentTerm(item, values[0][index], values[1][index])
                    for index, item in enumerate(counts)
                )
                complete = True
            elif status == "exhausted":
                raise LimitExceeded(
                    "Numerical worker exhausted its shared control allowance"
                )
            elif status in ("unavailable", "unsupported"):
                message = text(cast(str, native.get("message")), operation)
                issues.append(
                    Issue(
                        "analysis.backend_unavailable"
                        if status == "unavailable"
                        else "analysis.backend_unsupported",
                        message,
                        kind="unverified",
                    )
                )
            else:
                raise InputError("Unknown numerical worker status")
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except (InputError, ResourceError) as error:
        try:
            issues.append(
                Issue(
                    "analysis.backend_failed"
                    if isinstance(error, ResourceError)
                    else "analysis.input",
                    str(error),
                    kind="unverified"
                    if isinstance(error, ResourceError)
                    else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    report = Report(
        "analysis.enrichment",
        checked=len(tests),
        requested=requested,
        complete=complete,
        issues=tuple(issues),
    )
    return EnrichmentRead(
        tests if report.verified else (),
        tuple(counts),
        identity if report.verified else None,
        excluded,
        unannotated,
        report,
    )
