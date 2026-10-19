"""Own exact asserted/program inputs and run existing maintained materializers."""

import time
from dataclasses import replace

from rdflib import BNode, URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import bound_text
from bijux_ontology.rdf.entailment.preparation import prepare_entailment
from bijux_ontology.rdf.migrations.candidates import MigrationCheck
from bijux_ontology.rdf.patches.applying import _materialize
from bijux_ontology.rdf.patches.planning import capture_source
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.shacl.rules.materialization import materialize_rules
from bijux_ontology.shacl.rules.preparation import namespaces, selected_names

from .types import EntailmentPlan, MaterializationPlan, NativeMaterialization, RulePlan


def own_source(source: PreparedSource, operation: Operation) -> PreparedSource:
    """Capture only one exact selected physical graph and private origin metadata."""
    if not isinstance(source, PreparedSource):
        raise InputError("Materialization sessions require a PreparedSource")
    metadata = SnapshotMetadata.capture(source, operation)
    if (
        metadata.union
        or metadata.graph_fidelity != "complete"
        or metadata.literal_fidelity not in ("source_terms", "supplied_terms")
        or isinstance(metadata.graph_name, BNode)
    ):
        raise InputError("Sessions require exact terms and one physical graph scope")
    captured = capture_source(source, "asserted:", operation)
    if captured.witness.kind == "dataset":
        raise InputError("Sessions require a plain or pinned named/default graph")
    owned = _materialize(captured.witness, source, operation)
    if not metadata.same_scope(
        SnapshotMetadata.capture(source, operation)
    ) or not metadata.same_scope(SnapshotMetadata.capture(owned, operation)):
        raise InputError("Materialization input metadata changed during capture")
    return owned


def own_plan(plan: MaterializationPlan, operation: Operation) -> MaterializationPlan:
    """Retain private rules and their actual supplied prefix environment once."""
    if isinstance(plan, EntailmentPlan):
        bound_text(plan.regime, operation)
        return plan
    if not isinstance(plan, RulePlan):
        raise InputError("Expected an EntailmentPlan or RulePlan")
    selected = selected_names(plan.use_shapes, operation)
    checks = capture_items(plan.checks, MigrationCheck, operation)
    for check in checks:
        bound_text(check.name, operation)
    owned = own_source(plan.rules, operation)
    bindings = namespaces(plan.rules, operation)
    # The owned rules source has no public alias. Binding its namespace store
    # preserves query prefix semantics without changing any RDF assertion.
    for prefix, namespace in bindings:
        operation.consume("work")
        owned._dataset.bind(prefix, URIRef(namespace), replace=True)
    return replace(plan, rules=owned, use_shapes=selected, checks=checks)


def execute(
    source: PreparedSource, plan: MaterializationPlan, operation: Operation
) -> NativeMaterialization:
    """Recompute from asserted input, never a previous derived/effective graph."""
    timeout = max(0.000001, operation.deadline - time.monotonic())
    if isinstance(plan, EntailmentPlan):
        return prepare_entailment(
            source,
            regime=plan.regime,
            budget=operation,
            timeout=timeout,
            cancel=operation.cancel,
        )
    return materialize_rules(
        source,
        plan.rules,
        use_shapes=plan.use_shapes,
        mode="global-fixed-point",
        trusted=plan.trusted,
        max_passes=plan.max_passes,
        functions=plan.functions,
        checks=plan.checks,
        budget=operation,
        timeout=timeout,
        cancel=operation.cancel,
    )
