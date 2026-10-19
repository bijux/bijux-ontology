"""Checked rule materialization through the existing owned RDF worker boundary."""

import json
import threading
import time
from collections.abc import Sequence
from dataclasses import asdict

from rdflib import URIRef

from bijux_ontology._json import decode_json
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.execution.workers import execute_worker
from bijux_ontology.limits import (
    Budget,
    BudgetKind,
    IssueCollector,
    Limits,
    retain_report,
)
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.migrations.candidates import MigrationCheck, finish_migration
from bijux_ontology.rdf.patches.applying import _materialize
from bijux_ontology.rdf.patches.codecs import read_witness, witness_data
from bijux_ontology.rdf.patches.correspondence import blank_tokens
from bijux_ontology.rdf.patches.planning import capture_source, plan_rdf_patch
from bijux_ontology.rdf.patches.types import SnapshotWitness
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.transforms import Transformation
from bijux_ontology.reports import Issue, Report
from bijux_ontology.shacl.functions import ShaclFunctionCatalog

from .preparation import RuleUnavailable, namespaces, selected_names, single_scope
from .types import RuleMaterialization, RuleMode

_KINDS: tuple[BudgetKind, ...] = (
    "bytes",
    "resources",
    "triples",
    "work",
    "issues",
    "states",
)


def _run(request: dict[str, object], operation: Operation) -> dict[str, object]:
    """Adopt exact owned worker accounting before accepting any result metadata."""
    request["limits"] = asdict(operation.limits)
    request["allowances"] = {kind: operation.remaining(kind) for kind in _KINDS}
    size = json_bound(request, operation)
    if size > operation.limits.max_bytes:
        raise LimitExceeded("Rule request exceeds max_bytes")
    operation.consume("bytes", size)
    allowances = {kind: operation.remaining(kind) for kind in _KINDS}
    request["allowances"] = allowances
    payload = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode(
        "ascii"
    )
    operation.consume("work")
    raw = execute_worker(
        "bijux_ontology.shacl.rules.worker",
        payload,
        cap=operation.limits.max_bytes,
        timeout=operation.deadline - time.monotonic(),
        cancel=operation.cancel,
    )
    operation.consume("bytes", len(raw))
    value = decode_json(raw, max_bytes=operation.limits.max_bytes)
    if not isinstance(value, dict) or value.get("status") not in (
        "result",
        "exhausted",
        "unsupported",
        "unavailable",
        "backend_error",
    ):
        raise ResourceError("Malformed rule worker response")
    consumed = value.get("consumed")
    if (
        not isinstance(consumed, dict)
        or set(consumed) != set(_KINDS)
        or any(
            type(consumed[kind]) is not int
            or not 0 <= consumed[kind] <= allowances[kind]
            for kind in _KINDS
        )
    ):
        raise ResourceError("Malformed rule worker accounting")
    for kind in _KINDS:
        operation.consume(kind, consumed[kind])
    status = value["status"]
    if status != "result":
        if set(value) != {"status", "consumed"}:
            raise ResourceError("Malformed incomplete rule completion")
        if status == "exhausted":
            raise LimitExceeded("Rule materialization exhausted its declared allowance")
        if status == "unavailable":
            raise RuleUnavailable(
                "Install bijux-ontology[shacl] for maintained rule execution"
            )
        if status == "unsupported":
            raise RuleUnavailable(
                "Required rule semantics exceed the finite supported profile"
            )
        raise RuleUnavailable(
            "The maintained rule executor did not complete successfully"
        )
    if set(value) != {"status", "consumed", "passes", "effective"}:
        raise ResourceError("Malformed successful rule completion")
    return value


def _diagnostic(issues: IssueCollector, issue: Issue) -> None:
    """Retain ordinary failure metadata only while its original allowance remains."""
    try:
        issues.append(issue)
    except LimitExceeded as error:
        issues.exhausted(str(error), location="shacl.rules")


def materialize_rules(
    data: PreparedSource,
    rules: PreparedSource,
    *,
    use_shapes: Sequence[str | URIRef],
    mode: RuleMode = "ordered-pass",
    trusted: bool = False,
    max_passes: int = 100,
    functions: ShaclFunctionCatalog | None = None,
    checks: Sequence[MigrationCheck] = (),
    before_checks: Sequence[MigrationCheck] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30.0,
    cancel: threading.Event | None = None,
) -> RuleMaterialization:
    """Run explicit ordered or global rules, then check actual candidate RDF.

    This finite profile supports named node-shape owners with distinct numeric
    orders, constant/sh:this TripleRules, parsed CONSTRUCT rules, Core conditions
    and separately trusted function catalogs. Custom targets and ambient functions
    are unavailable. Each source must select one exact physical graph. Original
    anonymous data requires correspondence outside this initial profile and stays
    unknown; newly generated blanks retain same-worker identity across passes.

    Maintained rule internals have no instruction counter. Owned preparation,
    retained term growth and protocol work are charged; the process deadline and
    output cap additionally bound backend execution. Application checks are normal
    in-process callbacks, as in finish_migration; they are not forcibly preempted.
    Worker resource failures remain unavailable execution, distinct from invalid
    supplied rule inputs. Missing checks, incomplete closure and rejected output
    cannot publish a verified export candidate. An ordered pass does not claim
    global fixed-point closure.
    """
    operation = Operation(limits, budget, timeout, cancel)
    selected_mode: RuleMode = "ordered-pass"
    issues = IssueCollector(operation)
    asserted = derived = candidate = None
    passes = 0
    children: list[Report] = []
    try:
        if not isinstance(data, PreparedSource) or not isinstance(
            rules, PreparedSource
        ):
            raise InputError("Rule materialization requires prepared data and rules")
        if (
            not isinstance(mode, str)
            or mode not in ("ordered-pass", "global-fixed-point")
            or type(trusted) is not bool
        ):
            raise InputError("Rules require an explicit supported execution policy")
        selected_mode = mode
        if type(max_passes) is not int or max_passes <= 0:
            raise InputError("max_passes must be positive and within max_work")
        if max_passes > operation.limits.max_work:
            raise LimitExceeded("Rule pass allowance exceeds max_work")
        if functions is not None and not isinstance(functions, ShaclFunctionCatalog):
            raise InputError("Rules require an explicit typed function catalog")
        names = selected_names(use_shapes, operation)
        after = capture_items(checks, MigrationCheck, operation)
        before = capture_items(before_checks, MigrationCheck, operation)
        if not trusted:
            raise RuleUnavailable("Rule execution requires explicit trusted=True")
        captured = capture_source(data, "asserted:", operation).witness
        selected = capture_source(rules, "rules:", operation).witness
        for witness in (captured, selected):
            single_scope(witness)
        if blank_tokens(captured, operation):
            raise RuleUnavailable(
                "Original anonymous data has no qualified transport correspondence"
            )
        asserted = _materialize(captured, data, operation)
        function_data = None
        function_names: tuple[str, ...] = ()
        function_namespaces: list[list[str]] = []
        if functions is not None:
            if not functions.trusted:
                raise RuleUnavailable(
                    "Explicit functions require their own trusted catalog"
                )
            owned_functions = prepare_rdf(functions.graph(), budget=operation)
            function_data = witness_data(
                capture_source(owned_functions, "functions:", operation).witness,
                operation,
            )
            function_names = selected_names(functions.selected, operation)
            function_namespaces = namespaces(owned_functions, operation)
        response = _run(
            {
                "data": witness_data(captured, operation),
                "rules": witness_data(selected, operation),
                "namespaces": namespaces(rules, operation),
                "selected": list(names),
                "mode": mode,
                "maximum": max_passes,
                "functions": function_data,
                "function_names": list(function_names),
                "function_namespaces": function_namespaces,
            },
            operation,
        )
        completed = response["passes"]
        if type(completed) is not int or not 1 <= completed <= (
            1 if mode == "ordered-pass" else max_passes
        ):
            raise ResourceError("Invalid rule pass completion count")
        effective = read_witness(response["effective"], operation)
        operation.consume("work", len(effective.quads) + len(captured.quads))
        operation.consume("states", len(effective.quads) + len(captured.quads))
        original = set(captured.quads)
        if (effective.kind, effective.graph_selection, effective.graphs) != (
            captured.kind,
            captured.graph_selection,
            captured.graphs,
        ) or not original <= set(effective.quads):
            raise ResourceError("Rule completion changed asserted scope or exact RDF")
        passes = completed
        derived_witness = SnapshotWitness(
            effective.kind,
            effective.graph_selection,
            tuple(quad for quad in effective.quads if quad not in original),
            effective.graphs,
        )
        output = _materialize(effective, data, operation)
        derived = _materialize(derived_witness, data, operation)
        stage = Report("shacl.rules.execution", passes, requested=passes, regime=mode)
        children.append(stage)
        plan = plan_rdf_patch(
            asserted,
            output,
            budget=operation,
            timeout=operation.deadline - time.monotonic(),
            cancel=cancel,
        )
        retain_report(plan.report, operation, issues)
        children.append(plan.report)
        if plan.report.verified and plan.patch is not None:
            candidate = finish_migration(
                asserted,
                Transformation(output, stage),
                plan.patch,
                before_checks=before,
                after_checks=after,
                budget=operation,
            )
            retain_report(candidate.report, operation, issues)
            children.append(candidate.report)
    except LimitExceeded as error:
        if candidate is not None:
            issues.retain_completed(candidate.report)
        issues.exhausted(str(error), location="shacl.rules")
    except RuleUnavailable as error:
        _diagnostic(
            issues, Issue("shacl.rules_unverified", str(error), kind="unverified")
        )
    except ResourceError:
        _diagnostic(
            issues,
            Issue(
                "shacl.rules_unverified",
                "The isolated rule worker did not return qualified completion",
                kind="unverified",
            ),
        )
    except InputError:
        _diagnostic(
            issues,
            Issue(
                "shacl.rules_input_error",
                "Rule inputs do not satisfy the typed finite contract",
                kind="input_error",
            ),
        )
    report = Report(
        "shacl.rules",
        passes,
        tuple(issues),
        requested=max(1, passes),
        children=tuple(children),
        regime=selected_mode,
    )
    return RuleMaterialization(
        asserted, derived, candidate, selected_mode, passes, report
    )
