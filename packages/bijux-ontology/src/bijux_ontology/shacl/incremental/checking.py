"""Exact patch preconditions and conservative selected-obligation invalidation."""

from collections.abc import Sequence
from threading import Event
from typing import Literal

from rdflib import URIRef
from rdflib.namespace import OWL, RDF, RDFS, SH

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.loading import _charge_graph
from bijux_ontology.rdf.patches.applying import apply_rdf_patch
from bijux_ontology.rdf.patches.types import DatasetPatch, PatchBlankBinding
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.shacl.coverage import ShaclTarget
from bijux_ontology.shacl.execution.results import ShaclResult
from bijux_ontology.shacl.profiles.execution import remaining_deadline

from .preparation import _run, _targets, _unknown
from .types import IncrementalResult, IncrementalState

_SCHEMA = frozenset(
    (
        RDF.type,
        RDF.first,
        RDF.rest,
        RDFS.subClassOf,
        RDFS.subPropertyOf,
        RDFS.domain,
        RDFS.range,
        OWL.imports,
        OWL.sameAs,
        OWL.equivalentClass,
        OWL.equivalentProperty,
        OWL.inverseOf,
    )
)


def _affected(
    state: IncrementalState,
    patch: DatasetPatch,
    operation: Operation,
) -> set[URIRef] | None:
    """Return named affected focuses only when target/locality proof is unchanged."""
    operation.consume("work", 1 + 3 * len(state.report.issues))
    if (
        not state.local
        or not state.report.verified
        or patch.added_graphs
        or patch.removed_graphs
    ):
        return None
    subjects = set()
    for edits in (patch.removals, patch.additions):
        for quad in edits:
            operation.consume("work")
            bound_node(quad.predicate, operation)
            if (
                quad.predicate in _SCHEMA
                or quad.predicate in state.target_predicates
                or str(quad.predicate).startswith(str(SH))
            ):
                return None
            if isinstance(quad.subject, URIRef):
                bound_node(quad.subject, operation)
                operation.consume("states")
                subjects.add(quad.subject)
    for target in state.targets:
        operation.consume("work")
        bound_node(target.shape, operation)
        bound_node(target.focus_node, operation)
        if not isinstance(target.focus_node, URIRef):
            return None
    return subjects


def revalidate(
    state: IncrementalState,
    patch: DatasetPatch,
    *,
    blank_bindings: Sequence[PatchBlankBinding] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> IncrementalResult:
    """Apply an exact checked delta and rerun every conservatively affected scope.

    A complete passing finite local baseline permits reuse of unaffected actual
    named root/focus obligations. Target/type/schema, complex/global features,
    anonymous coverage and previous failures force full selected-target checking.
    Raw results, exact patch outcome and evaluated/reused counts stay separate.
    This operation never modifies the prior state or publishes partial state.
    """
    selected_limits = limits or (
        budget.limits
        if budget is not None
        else (state.limits if isinstance(state, IncrementalState) else Limits())
    )
    operation = Operation(selected_limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    application = None
    results: list[ShaclResult] = []
    updated = None
    strategy: Literal["local", "full"] = "full"
    checked = reused = total = 0
    try:
        if not isinstance(state, IncrementalState) or not isinstance(
            patch, DatasetPatch
        ):
            raise InputError("Revalidation requires owned state and a typed RDF patch")
        operation.consume("states", len(state.targets) + len(state.roots) + 2)
        for root in state.roots:
            bound_text(root, operation)
        for resource in (state.shapes, state.ontology):
            if resource is not None:
                for text in (
                    resource.document_iri,
                    resource.effective_url,
                    resource.graph_name,
                ):
                    if text is not None:
                        bound_text(text, operation)
                _charge_graph(
                    resource._read(budget=operation),
                    operation,
                    operation.limits.max_bytes,
                )
        application = apply_rdf_patch(
            state.source,
            patch,
            blank_bindings=blank_bindings,
            budget=operation,
            timeout=remaining_deadline(operation),
            cancel=cancel,
        )
        retain_report(application.report, operation, issues)
        if application.source is None or not application.report.verified:
            _unknown(
                issues, "The exact patch base or resulting snapshot is unavailable"
            )
        else:
            affected = _affected(state, patch, operation)
            targets: tuple[ShaclTarget, ...] | None = None
            if affected is None:
                raw = _run(
                    state.shapes,
                    state.ontology,
                    state.roots,
                    application.source,
                    operation,
                    trusted_sparql=state.trusted_sparql,
                )
                results.append(raw)
                retain_report(raw.report, operation, issues)
                targets = _targets(raw, state.roots, operation)
                checked = total = len(targets) if targets is not None else 0
            else:
                strategy = "local"
                total = len(state.targets)
                grouped: dict[str, list[URIRef]] = {}
                for target in state.targets:
                    operation.consume("work")
                    if target.focus_node in affected:
                        assert isinstance(target.focus_node, URIRef)
                        operation.consume("states")
                        grouped.setdefault(str(target.shape), []).append(
                            target.focus_node
                        )
                    else:
                        reused += 1
                complete = True
                for root, focus in grouped.items():
                    raw = _run(
                        state.shapes,
                        state.ontology,
                        (root,),
                        application.source,
                        operation,
                        trusted_sparql=state.trusted_sparql,
                        focus=tuple(focus),
                        pairs=True,
                    )
                    results.append(raw)
                    retain_report(raw.report, operation, issues)
                    actual = _targets(raw, (root,), operation)
                    if actual is None:
                        complete = False
                    else:
                        expected = {(URIRef(root), node) for node in focus}
                        operation.consume("states", len(expected))
                        if {(t.shape, t.focus_node) for t in actual} != expected:
                            complete = False
                        checked += len(actual)
                if complete and checked + reused == total:
                    targets = state.targets
            if targets is None:
                _unknown(
                    issues, "Required selected-target revalidation did not complete"
                )
            else:
                qualification = Report(
                    "shacl.incremental.coverage",
                    checked + reused,
                    requested=total,
                    regime="checked-and-proven-local-reuse",
                )
                current = Report(
                    "shacl.incremental",
                    checked + reused,
                    tuple(issues),
                    requested=total,
                    children=(qualification,),
                    regime="checked-rdf-delta-selected-obligations",
                )
                if current.complete:
                    updated = IncrementalState._owned(
                        source=application.source,
                        shapes=state.shapes,
                        ontology=state.ontology,
                        roots=state.roots,
                        targets=targets,
                        target_predicates=state.target_predicates,
                        local=state.local,
                        trusted_sparql=state.trusted_sparql,
                        limits=operation.limits,
                        report=current,
                    )
    except LimitExceeded as error:
        if application is not None:
            issues.retain_completed(application.report)
        for raw in results:
            issues.retain_completed(raw.report)
        issues.exhausted(str(error), location="shacl.incremental")
    except (InputError, ResourceError, RuntimeError):
        _unknown(issues, "Incremental patch or required execution is unavailable")
    report = Report(
        "shacl.incremental",
        checked + reused,
        tuple(issues),
        requested=max(1, total, checked + reused),
        complete=updated is not None,
        regime="checked-rdf-delta-selected-obligations",
    )
    return IncrementalResult(
        updated,
        application,
        tuple(results),
        strategy,
        checked,
        reused,
        total,
        report,
    )
