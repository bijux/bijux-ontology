"""Apply explicit reviewed group choices and recheck actual native mapping output."""

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.patches.correspondence import Operation
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.mappings.types import MappingSet

from .planning import _check, _ContextUnavailable, _produce, _reserve
from .types import MappingReconciliation, MappingResolution, ReconciledMappings


def apply_mapping_resolution(
    reconciliation: MappingReconciliation,
    resolution: MappingResolution,
    *,
    mapping_set_id: str,
    license: str,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
) -> ReconciledMappings:
    """Produce only the explicitly retained groups, then rerun selected contracts.

    Baseline disagreement is review evidence, not an approval veto. Approval
    cannot override unknown final semantics, bad endpoints or retained conflicts.
    The result ID/license are explicit; original IDs/licenses remain in witnesses.
    """
    if not isinstance(reconciliation, MappingReconciliation) or not isinstance(
        resolution, MappingResolution
    ):
        raise InputError("Apply an owned reconciliation and explicit MappingResolution")
    operation = Operation(limits, budget, timeout, None)
    issues = IssueCollector(operation)
    reports: list[Report] = []
    production = None
    checked = 0
    try:
        if resolution.reconciliation is not reconciliation:
            raise InputError("Resolution belongs to a different reconciliation")
        for text in (resolution.approved_by, resolution.reason):
            if type(text) is not str:
                raise InputError(
                    "Resolution requires explicit nonempty authority and reason"
                )
            bound_text(text, operation)
            if not text.strip():
                raise InputError(
                    "Resolution requires explicit nonempty authority and reason"
                )
        if not reconciliation.inventory_complete:
            raise _ContextUnavailable(
                "An incomplete source inventory cannot be approved"
            )
        if type(resolution.retain) is not tuple or not resolution.retain:
            raise InputError("Explicitly approve a nonempty immutable group inventory")
        _reserve(operation, len(resolution.retain))
        chosen = []
        seen = set()
        for index in resolution.retain:
            operation.consume("work")
            if (
                type(index) is not int
                or not 0 <= index < len(reconciliation.groups)
                or index in seen
            ):
                raise InputError(
                    "Resolution positions must uniquely belong to this review"
                )
            _reserve(operation, 2)
            seen.add(index)
            chosen.append(reconciliation.groups[index])
        for group in chosen:
            for occurrence in group.occurrences:
                operation.consume("work")
                if not isinstance(occurrence.mapping_set, MappingSet):
                    raise InputError(
                        "Resolution requires actual source mapping occurrences"
                    )
        _reserve(operation)
        production = _produce(tuple(chosen), mapping_set_id, license, operation)
        _check(
            production,
            reconciliation.subject_vocabulary,
            reconciliation.object_vocabulary,
            reconciliation.policy,
            operation,
            issues,
            reports,
        )
        operation.consume("work", 0)
        checked = 1
    except _ContextUnavailable as error:
        try:
            issues.append(
                Issue(
                    "mapping.reconciliation_unverified", str(error), kind="unverified"
                )
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining unresolved source context")
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(
                Issue("mapping.reconciliation_invalid", str(error), kind="input_error")
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining invalid resolution")
    return ReconciledMappings(
        reconciliation,
        resolution,
        production,
        Report(
            "mapping.reconciliation_application",
            checked,
            tuple(issues),
            requested=1,
            complete=checked == 1,
            children=tuple(reports),
            regime="explicit-approved-mapping-selection",
        ),
    )
