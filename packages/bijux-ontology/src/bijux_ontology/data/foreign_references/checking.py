"""Resolve selected source keys against owned, explicitly scoped target indexes."""

from collections.abc import Sequence

from rdflib import Graph

from bijux_ontology.data.keys.extraction import _KeyExtraction
from bijux_ontology.data.keys.types import KeyRecord
from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.value_comparison.observations import ScalarComparisons
from bijux_ontology.vocabulary.sources.view import Vocabulary

from .obligations import TargetObligations
from .preparation import _operation, _records
from .resolution import admit_targets, resolve_reference
from .types import ForeignReferenceResult, ForeignResolution, ForeignTargetIndex


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
    obligations = TargetObligations(models, vocabulary, operation, issues)
    try:
        comparisons = ScalarComparisons(operation)
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
        candidates = admit_targets(targets, operation)
        for key in extraction:
            resolution, matches = resolve_reference(
                key,
                targets,
                operation,
                issues,
                resolutions,
                candidates=candidates,
                comparisons=comparisons,
            )
            if resolution.status == "resolved" and contract.target_model is not None:
                for target in matches:
                    report = obligations.check(target, contract.target_model)
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
