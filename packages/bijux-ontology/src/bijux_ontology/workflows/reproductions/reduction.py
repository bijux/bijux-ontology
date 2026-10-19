"""Bounded deletion passes retain only the same complete native contradiction."""

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from threading import Event
from typing import Literal, cast

from rdflib import URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.json.paths import SourcePath, at_path, bound_path, location
from bijux_ontology.json.snapshots import snapshot
from bijux_ontology.rdf.merging.planning import _list_domains
from bijux_ontology.rdf.records.types import (
    PatchBlank,
    SnapshotWitness,
    validate_witness,
)

from .execution import _run
from .preparation import reserve
from .types import (
    ContextRequest,
    EmissionRequest,
    FailureReduction,
    FailureSignature,
    OperationResult,
    ReproductionRequest,
    ReproductionUnavailable,
    SelectorRebinding,
    ShaclRequest,
    SkosRequest,
    SourceObligation,
)


@dataclass(frozen=True)
class _Deletion:
    role: Literal["document", "resource", "model", "source", "data", "shapes", "import"]
    path: SourcePath = ()
    indices: tuple[int, ...] = ()


def _complete(report: Report) -> bool:
    return bool(
        report.complete
        and report.outcome == "disagreement"
        and report.checked > 0
        and report.requested is not None
        and report.requested > 0
        and all(issue.kind == "finding" for issue in report.issues)
    )


def failure_signature(
    request: ReproductionRequest,
    result: OperationResult,
    finding: Issue,
) -> FailureSignature:
    """Bind one actual finding to owned nonempty completed public-operation evidence.

    Matching keeps exact native scope, regime, families, requested/completed counts,
    issue kind/code/severity/location/expected/actual and domain identity. Messages
    and generated report-node labels are diagnostics rather than stable obligations.
    """
    if (
        type(result) is not OperationResult
        or result._request is not request
        or not _complete(result.report)
        or not any(issue is finding for issue in result.report.issues)
        or finding.kind != "finding"
    ):
        raise ReproductionUnavailable(
            "Failure signatures require an actual complete "
            "nonvacuous native contradiction"
        )
    return FailureSignature._owned(
        _request=request,
        kind=request.kind,
        report=result.report,
        finding=finding,
        obligations=result.obligations,
        shacl_findings_json=result.shacl_findings_json,
    )


def _quad_units(
    witness: SnapshotWitness, role: Literal["source", "data", "shapes"], budget: Budget
) -> list[_Deletion]:
    """Group exact anonymous/list coupling without interpreting or repairing lists."""
    validate_witness(witness, budget)
    count = len(witness.quads)
    reserve(budget, 8 * count + 1)
    list_domains = _list_domains((witness,), budget)
    parents = list(range(count))
    owners: dict[tuple[object, ...], int] = {}

    def root(index: int) -> int:
        while parents[index] != index:
            budget.consume("work")
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for index, quad in enumerate(witness.quads):
        budget.consume("work")
        keys: list[tuple[object, ...]] = []
        for term in (quad.subject, quad.value, quad.graph):
            budget.consume("work")
            if isinstance(term, PatchBlank):
                reserve(budget)
                keys.append(("blank", term.token))
        for term in (quad.subject, quad.value):
            domain = (
                list_domains.get((quad.graph, term))
                if isinstance(term, (URIRef, PatchBlank))
                else None
            )
            if domain is not None:
                reserve(budget)
                keys.append(("list", quad.graph, domain))
        for key in keys:
            budget.consume("work")
            if key in owners:
                parents[root(index)] = root(owners[key])
            else:
                owners[key] = index
    grouped: dict[int, list[int]] = defaultdict(list)
    for index in range(count):
        budget.consume("work")
        reserve(budget)
        grouped[root(index)].append(index)
    return [_Deletion(role, indices=tuple(indices)) for indices in grouped.values()]


def _json_units(
    document: object, obligations: tuple[SourceObligation, ...], budget: Budget
) -> list[_Deletion]:
    result = []
    pending: list[tuple[SourcePath, object]] = [((), document)]
    protected = tuple(item.path for item in obligations if item.raw_json is not None)
    while pending:
        path, value = pending.pop()
        budget.consume("work")
        children: Iterable[tuple[str | int, object]]
        if isinstance(value, dict):
            children = value.items()
        elif isinstance(value, list):
            children = enumerate(value)
        else:
            continue
        for key, child in children:
            reserve(budget, 2)
            selected = (*path, key)
            bound_path(selected, budget)
            if any(target[: len(selected)] == selected for target in protected):
                # Retain a mandatory value while still visiting its ancestors'
                # other children for independently removable sibling subtrees.
                if selected not in protected:
                    pending.append((selected, child))
                continue
            if type(key) is int and any(
                not item.named
                and len(item.path) > len(path)
                and item.path[: len(path)] == path
                and type(item.path[len(path)]) is int
                and key < cast(int, item.path[len(path)])
                for item in obligations
            ):
                continue
            result.append(_Deletion("document", selected))
            pending.append((selected, child))
    return result


def _units(
    request: ReproductionRequest, result: OperationResult, budget: Budget
) -> list[_Deletion]:
    if isinstance(request, ContextRequest | EmissionRequest):
        document, _ = snapshot(request.document, budget)
        units = _json_units(document, result.obligations, budget)
        reserve(budget, len(request.resources))
        units.extend(
            _Deletion("resource", indices=(index,))
            for index in range(len(request.resources))
        )
        if isinstance(request, EmissionRequest):
            models = json.loads(request.models_json)
            reserve(budget, len(models))
            units.extend(
                _Deletion("model", indices=(index,)) for index in range(len(models))
            )
        return units
    if isinstance(request, SkosRequest):
        return _quad_units(request.source, "source", budget)
    units = _quad_units(request.data, "data", budget) + _quad_units(
        request.shapes, "shapes", budget
    )
    reserve(budget, len(request.imports))
    units.extend(
        _Deletion("import", indices=(index,)) for index in range(len(request.imports))
    )
    return units


def _groups(units: list[_Deletion], budget: Budget) -> list[list[_Deletion]]:
    """Batch sibling subtrees without coupling an ancestor to its descendants."""
    groups: dict[tuple[str, SourcePath], list[_Deletion]] = {}
    reserve(budget, 3 * len(units) + 1)
    for unit in units:
        budget.consume("work", len(unit.path) + 1)
        budget.consume("bytes", 16 * len(unit.path))
        parent = unit.path[:-1] if unit.role == "document" else ()
        groups.setdefault((unit.role, parent), []).append(unit)
    return list(groups.values())


def _shift(path: SourcePath, removed: SourcePath) -> SourcePath:
    if not removed or type(removed[-1]) is not int:
        return path
    parent, index = removed[:-1], removed[-1]
    if (
        len(path) > len(parent)
        and path[: len(parent)] == parent
        and type(path[len(parent)]) is int
        and cast(int, path[len(parent)]) > index
    ):
        return (*parent, cast(int, path[len(parent)]) - 1, *path[len(parent) + 1 :])
    return path


def _candidate(
    request: ReproductionRequest,
    deletions: list[_Deletion],
    result: OperationResult,
    budget: Budget,
) -> tuple[ReproductionRequest, tuple[SelectorRebinding, ...]]:
    reserve(budget, len(deletions) * 3 + len(result.obligations))
    paths = [item.path for item in result.obligations]
    original_paths = tuple(paths)
    if isinstance(request, ContextRequest | EmissionRequest):
        document, _ = snapshot(request.document, budget)
        document_deletions = [
            item.path for item in deletions if item.role == "document"
        ]
        budget.consume(
            "work",
            (len(document_deletions) + 1)
            * max(1, len(document_deletions).bit_length()),
        )
        # Deepest-first, with higher array indices first, keeps every old path valid.
        document_deletions.sort(
            key=lambda path: (
                len(path),
                tuple(
                    (0, part) if isinstance(part, str) else (1, part) for part in path
                ),
            ),
            reverse=True,
        )
        for removed in document_deletions:
            parent = at_path(document, removed[:-1])
            key = removed[-1]
            if isinstance(parent, dict):
                parent.pop(key)
            elif isinstance(parent, list) and type(key) is int:
                del parent[key]
            else:
                raise InputError("Deletion no longer refers to an owned source node")
            reserve(budget, sum(len(path) + 1 for path in paths))
            paths = [_shift(path, removed) for path in paths]
        _, document_json = snapshot(document, budget)
        removed_resources = {
            index
            for unit in deletions
            if unit.role == "resource"
            for index in unit.indices
        }
        resources = tuple(
            value
            for index, value in enumerate(request.resources)
            if index not in removed_resources
        )
        if isinstance(request, ContextRequest):
            declarations = json.loads(request.expectations_json)
            for index, declaration in enumerate(declarations):
                declaration["path"] = list(paths[index])
            _, expectations = snapshot(declarations, budget)
            candidate: ReproductionRequest = ContextRequest._owned(
                document_json=document_json,
                expectations_json=expectations,
                resources=resources,
                base=request.base,
            )
        else:
            declarations = json.loads(request.models_json)
            removed_models = {
                index
                for unit in deletions
                if unit.role == "model"
                for index in unit.indices
            }
            models = [
                value
                for index, value in enumerate(declarations)
                if index not in removed_models
            ]
            usages = json.loads(request.usages_json)
            # Usage anchors are emitted before their selected field obligations.
            for index, usage in enumerate(usages):
                old_path = tuple(usage.get("path", []))
                matching = next(
                    index
                    for index, obligation in enumerate(result.obligations)
                    if obligation.path == old_path and obligation.raw_json is None
                )
                usage["path"] = list(paths[matching])
            _, model_json = snapshot(models, budget)
            _, usage_json = snapshot(usages, budget)
            candidate = EmissionRequest._owned(
                document_json=document_json,
                models_json=model_json,
                usages_json=usage_json,
                resources=resources,
                base=request.base,
            )
    else:

        def reduced(witness: SnapshotWitness, role: str) -> SnapshotWitness:
            removed = {
                index
                for unit in deletions
                if unit.role == role
                for index in unit.indices
            }
            reserve(budget, len(witness.quads))
            return replace(
                witness,
                quads=tuple(
                    quad
                    for index, quad in enumerate(witness.quads)
                    if index not in removed
                ),
            )

        if isinstance(request, SkosRequest):
            candidate = SkosRequest._owned(
                source=reduced(request.source, "source"),
                concepts=request.concepts,
                rules=request.rules,
            )
        else:
            removed_imports = {
                index
                for unit in deletions
                if unit.role == "import"
                for index in unit.indices
            }
            candidate = ShaclRequest._owned(
                data=reduced(request.data, "data"),
                shapes=reduced(request.shapes, "shapes"),
                focus_nodes=request.focus_nodes,
                use_shapes=request.use_shapes,
                imports=tuple(
                    value
                    for index, value in enumerate(request.imports)
                    if index not in removed_imports
                ),
            )
    return candidate, tuple(
        SelectorRebinding(before, after)
        for before, after in zip(original_paths, paths, strict=True)
        if before != after
    )


def _issue_key(
    issue: Issue, rebindings: tuple[SelectorRebinding, ...] = ()
) -> tuple[object, ...]:
    where = issue.location
    for binding in rebindings:
        previous = location(binding.before)
        suffix = where[len(previous) :] if where.startswith(previous) else ""
        ordinal = (
            suffix.startswith("[")
            and suffix.endswith("]")
            and suffix[1:-1].isascii()
            and suffix[1:-1].isdigit()
        )
        if where == previous or ordinal:
            where = location(binding.after) + suffix
            break
    return issue.kind, issue.code, issue.severity, where, issue.expected, issue.actual


def _matches(
    signature: FailureSignature,
    result: OperationResult,
    bindings: tuple[SelectorRebinding, ...],
    budget: Budget,
) -> bool:
    before, after = signature.report, result.report
    reserve(
        budget,
        len(signature.obligations)
        + len(result.obligations)
        + 8 * (len(before.issues) + len(after.issues) + 1),
    )
    for report in (before, after):
        for issue in report.issues:
            budget.consume(
                "work",
                sum(
                    len(value)
                    for value in (
                        issue.kind,
                        issue.code,
                        issue.severity,
                        issue.location,
                        issue.expected or "",
                        issue.actual or "",
                    )
                )
                + 1,
            )
    if not _complete(after) or (
        before.scope,
        before.regime,
        before.families,
        before.checked,
        before.requested,
    ) != (after.scope, after.regime, after.families, after.checked, after.requested):
        return False
    if not any(
        _issue_key(issue) == _issue_key(signature.finding, bindings)
        for issue in after.issues
    ):
        return False
    allowed = {_issue_key(issue, bindings) for issue in before.issues}
    if any(_issue_key(issue) not in allowed for issue in after.issues):
        return False

    if (
        len(signature.obligations) != len(result.obligations)
        or signature.shacl_findings_json != result.shacl_findings_json
    ):
        return False
    for old, new in zip(signature.obligations, result.obligations, strict=True):
        budget.consume("work", len(old.raw_json or "") + len(new.raw_json or "") + 1)
        budget.consume(
            "work",
            sum(
                len(value or "")
                for value in (
                    *old.subjects,
                    *new.subjects,
                    *old.graphs,
                    *new.graphs,
                )
            ),
        )
        mapped = next(
            (binding.after for binding in bindings if binding.before == old.path),
            old.path,
        )
        if (
            new.path != mapped
            or old.raw_json != new.raw_json
            or old.subjects != new.subjects
            or old.graphs != new.graphs
            or old.named != new.named
            or mapped != old.path
            and not old.named
        ):
            return False
    return True


def reduce_failure(
    request: ReproductionRequest,
    signature: FailureSignature,
    *,
    maximum_trials: int = 128,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> FailureReduction:
    """Delete supported units to a finite local fixed point, keeping verified best.

    Search completion is separate from the returned native contradiction. A trial
    bound, exhausted allowance, deadline or cancellation retains the last fully
    proved runnable request; no unproved candidate replaces it and no global
    minimum or minimal causal explanation is claimed.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    original = best_result = None
    best = None
    trials = accepted = unverified = 0
    all_bindings: list[SelectorRebinding] = []
    complete = False
    try:
        if type(signature) is not FailureSignature or signature._request is not request:
            raise InputError("Failure signature belongs to a different owned request")
        if type(maximum_trials) is not int or maximum_trials <= 0:
            raise InputError("Reduction requires a positive finite trial bound")
        reserve(operation, 6)
        original = _run(request, operation)
        if not _matches(signature, original, (), operation):
            raise ReproductionUnavailable(
                "Original installed operation no longer establishes "
                "the required complete failure"
            )
        best, best_result = request, original
        current_signature = signature
        while True:
            units = _units(best, best_result, operation)
            retained = False
            for siblings in _groups(units, operation):
                granularity = max(1, len(siblings) // 2)
                while siblings:
                    for start in reversed(range(0, len(siblings), granularity)):
                        operation.consume("work")
                        if trials >= maximum_trials:
                            raise LimitExceeded(
                                "Reduction reached its explicit trial bound"
                            )
                        candidate, bindings = _candidate(
                            best,
                            siblings[start : start + granularity],
                            best_result,
                            operation,
                        )
                        # Pay publication before execution. A fully qualified
                        # matching candidate must survive a terminal allowance.
                        reserve(operation, len(bindings) + 16)
                        trials += 1
                        result = _run(candidate, operation)
                        if result.report.outcome == "unverified":
                            unverified += 1
                        if result.report.outcome == "exhausted":
                            raise LimitExceeded(
                                "Candidate native execution exhausted its allowance"
                            )
                        if _matches(current_signature, result, bindings, operation):
                            best, best_result = candidate, result
                            accepted += 1
                            all_bindings.extend(bindings)
                            matched = next(
                                issue
                                for issue in result.report.issues
                                if _issue_key(issue)
                                == _issue_key(current_signature.finding, bindings)
                            )
                            current_signature = failure_signature(best, result, matched)
                            retained = True
                            break
                    if retained or granularity == 1:
                        break
                    granularity = max(1, granularity // 2)
                if retained:
                    break
            if not retained:
                complete = not unverified
                if unverified:
                    issues.append(
                        Issue(
                            "reproduction.candidate_unverified",
                            "Some candidate operations were unavailable",
                            kind="unverified",
                            actual=str(unverified),
                        )
                    )
                break
    except LimitExceeded as error:
        if best_result is not None:
            issues.retain_completed(best_result.report)
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(
                Issue(
                    "reproduction.unavailable"
                    if isinstance(error, (ReproductionUnavailable, ResourceError))
                    else "reproduction.input",
                    str(error),
                    kind="unverified"
                    if isinstance(error, (ReproductionUnavailable, ResourceError))
                    else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    report = Report(
        "reproduction.reduction",
        checked=trials,
        requested=trials if complete and trials else max(1, trials + 1),
        complete=complete,
        issues=tuple(issues),
    )
    return FailureReduction(
        request,
        best,
        original,
        best_result,
        signature,
        trials,
        accepted,
        tuple(all_bindings),
        report,
    )
