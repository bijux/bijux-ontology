"""Checked selected-graph transformations through the existing patch/export gate."""

import json
import threading
import time
from collections.abc import Sequence
from dataclasses import asdict

from rdflib import BNode, Literal, URIRef

from bijux_ontology._json import decode_json
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.execution.workers import execute_worker
from bijux_ontology.limits import (
    BUDGET_KINDS,
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.migrations.candidates import (
    MigrationCandidate,
    MigrationCheck,
    finish_migration,
)
from bijux_ontology.rdf.patches.applying import apply_rdf_patch
from bijux_ontology.rdf.patches.codecs import read_witness, term_data, witness_data
from bijux_ontology.rdf.patches.planning import Captured, capture_source
from bijux_ontology.rdf.patches.types import (
    DatasetPatch,
    PatchBlankBinding,
    PatchLiteral,
    PatchUnavailable,
    PatchValue,
)
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, literal_datatype
from bijux_ontology.rdf.transforms import Transformation
from bijux_ontology.reports import Issue, Report

from .preparation import TransformUnavailable
from .types import SparqlBinding, SparqlProgram


def _bindings(
    program: SparqlProgram, captured: Captured, operation: Budget
) -> list[object]:
    """Bound exact parameters before hashing or dispatching to native code."""
    result: list[object] = []
    seen = set()
    for binding in capture_items(program.bindings, SparqlBinding, operation):
        bound_text(binding.name, operation)
        if binding.name in seen:
            raise InputError("SPARQL binding names must be distinct")
        operation.consume("states", 2)
        seen.add(binding.name)
        value = binding.value
        bound_node(value, operation)
        portable: PatchValue
        if isinstance(value, BNode):
            portable = captured.reference(value)
        elif isinstance(value, Literal):
            portable = PatchLiteral(
                str(value), str(literal_datatype(value)), value.language
            )
        else:
            portable = URIRef(value)
        result.append([binding.name, term_data(portable, operation)])
    return result


def _native(
    source: Captured, program: SparqlProgram, operation: Operation
) -> dict[str, object]:
    """Use existing owned transport and shared remaining-allowance receipts."""
    bound_text(program.text, operation)
    request = {
        "source": witness_data(source.witness, operation),
        "kind": program.kind,
        "text": program.text,
        "bindings": _bindings(program, source, operation),
        "limits": asdict(operation.limits),
        "allowances": {kind: operation.remaining(kind) for kind in BUDGET_KINDS},
    }
    size = json_bound(request, operation)
    if size > operation.limits.max_bytes:
        raise LimitExceeded("SPARQL transformation request exceeds max_bytes")
    operation.consume("bytes", size)
    allowances = {kind: operation.remaining(kind) for kind in BUDGET_KINDS}
    request["allowances"] = allowances
    payload = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode(
        "ascii"
    )
    remaining = operation.deadline - time.monotonic()
    if remaining <= 0:
        raise LimitExceeded("SPARQL transformation exceeded its owned deadline")
    raw = execute_worker(
        "bijux_ontology.rdf.query_transforms.worker",
        payload,
        cap=operation.limits.max_bytes,
        timeout=remaining,
        cancel=operation.cancel,
    )
    operation.consume("bytes", len(raw))
    response = decode_json(
        raw, max_bytes=operation.limits.max_bytes, max_depth=operation.limits.max_depth
    )
    if not isinstance(response, dict):
        raise ResourceError("Malformed SPARQL transformation response")
    consumed = response.get("consumed")
    if (
        not isinstance(consumed, dict)
        or set(consumed) != set(BUDGET_KINDS)
        or any(
            type(consumed[kind]) is not int
            or not 0 <= consumed[kind] <= allowances[kind]
            for kind in BUDGET_KINDS
        )
    ):
        raise ResourceError("Malformed transformation worker accounting")
    for kind in BUDGET_KINDS:
        operation.consume(kind, consumed[kind])
    status = response.get("status")
    if status == "exhausted":
        raise LimitExceeded("SPARQL transformation exhausted its owned allowance")
    if status == "unverified":
        raise TransformUnavailable("Selected SPARQL semantics are unsupported")
    if status == "input_error":
        raise InputError("SPARQL syntax, parameters or actual output RDF is invalid")
    if status != "result" or set(response) != {"status", "target", "consumed"}:
        raise ResourceError("SPARQL transformation did not return a complete candidate")
    return response


def transform_sparql(
    source: PreparedSource,
    program: SparqlProgram,
    *,
    before_checks: Sequence[MigrationCheck] = (),
    after_checks: Sequence[MigrationCheck] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 10.0,
    cancel: threading.Event | None = None,
) -> MigrationCandidate:
    """Run a finite native program and independently check its exact owned candidate.

    CONSTRUCT replaces selected triples. Local UPDATE changes only a private
    selected graph. Plain graphs and explicitly pinned named/default graphs are
    supported; datasets/unions cannot silently widen scope. Parameters are native
    RDF bindings; blank parameters must belong to the actual source snapshot.
    Query constants require explicit datatypes or language tags; use xsd:string
    for string constants, or supply exact RDF parameters. Native plain-string
    matching does not establish intrinsic RDF 1.1 identity. Anonymous graph
    selection, BIND, VALUES, subqueries, grouping/aggregates, WHERE blank syntax
    and lossy literal constructors remain outside this finite profile. Native
    parsing alone does not qualify variable scoping or aggregate projection validity.
    Existing structural patch pre/postconditions remain required. Before checks
    retain baseline evidence, while selected after checks determine acceptance.
    Use export_migration followed by write_export for explicit atomic publication.
    Opaque engine search uses the owned process deadline, not an instruction or
    RSS counter. Host and explicit worker preparation/adoption share one budget.
    """
    if not isinstance(source, PreparedSource) or not isinstance(program, SparqlProgram):
        raise InputError("SPARQL transformations require an owned source and program")
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children: list[Report] = []
    patch = None
    applied = None
    checked = False
    try:
        if source.is_dataset and source.graph_name is None:
            raise TransformUnavailable(
                "Select one physical graph before transformation"
            )
        captured = capture_source(source, "base:", operation)
        data = _native(captured, program, operation)
        target = read_witness(data["target"], operation)
        if (
            target.kind != captured.witness.kind
            or target.graph_selection != captured.witness.graph_selection
            or target.graphs != captured.witness.graphs
        ):
            raise ResourceError("Transformation changed its authorized graph inventory")
        original = captured.witness
        operation.consume("states", len(original.quads) + len(target.quads))
        operation.consume("work", len(original.quads) + len(target.quads))
        old, new = set(original.quads), set(target.quads)
        operation.consume("states", len(old) + len(new))
        removals, additions = tuple(old - new), tuple(new - old)
        patch = DatasetPatch(original, target, removals, additions)
        bindings = []
        seen = set()
        for node, reference in captured.owned.values():
            operation.consume("work")
            if reference.token not in seen:
                operation.consume("states", 2)
                seen.add(reference.token)
                bindings.append(PatchBlankBinding(reference.token, node))
        operation.consume("states")
        remaining = operation.deadline - time.monotonic()
        if remaining <= 0:
            raise LimitExceeded("SPARQL transformation exceeded its owned deadline")
        applied = apply_rdf_patch(
            source,
            patch,
            blank_bindings=tuple(bindings),
            budget=operation,
            timeout=remaining,
            cancel=cancel,
        )
        retain_report(applied.report, operation, issues)
        children.append(applied.report)
        checked = applied.report.verified and applied.source is not None
    except LimitExceeded as error:
        if applied is not None:
            issues.retain_completed(applied.report)
        issues.exhausted(str(error), location="rdf.query_transform")
    except (TransformUnavailable, PatchUnavailable, ResourceError):
        try:
            issues.append(
                Issue(
                    "rdf.query_transform_unverified",
                    "The selected source or SPARQL program is unavailable",
                    kind="unverified",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted), location="rdf.query_transform")
    except InputError:
        try:
            issues.append(
                Issue(
                    "rdf.query_transform_invalid",
                    "SPARQL syntax, parameters or actual output RDF is invalid",
                    kind="input_error",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted), location="rdf.query_transform")
    report = Report(
        "rdf.query_transform",
        int(checked),
        tuple(issues),
        requested=1,
        complete=checked,
        children=tuple(children),
        families=("sparql-" + program.kind.lower(),),
        regime="selected-physical-graph-transformation",
    )
    if checked and applied is not None and report.verified:
        return finish_migration(
            source,
            Transformation(applied.source, report),
            patch,
            before_checks=before_checks,
            after_checks=after_checks,
            budget=operation,
        )
    return MigrationCandidate(None, patch, (), (), report)
