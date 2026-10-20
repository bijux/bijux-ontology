"""Atomic exact object migrations through checked portable RDF dataset patches."""

import json
import threading
import time
from collections.abc import Sequence
from dataclasses import replace

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import XSD

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.execution.processes.transport import checkpoint
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.rdf.literals.checking import check_literal, datatype_relation
from bijux_ontology.rdf.migrations.candidates import (
    MigrationCheck,
    finish_migration,
)
from bijux_ontology.rdf.patches.applying import apply_rdf_patch
from bijux_ontology.rdf.patches.planning import Captured, capture_source
from bijux_ontology.rdf.patches.types import DatasetPatch, PatchBlankBinding
from bijux_ontology.rdf.records.codecs import quad_data
from bijux_ontology.rdf.records.types import (
    PatchLiteral,
    PatchQuad,
    PatchResource,
    PatchUnavailable,
    PatchValue,
)
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, literal_datatype, literal_term
from bijux_ontology.rdf.transforms import Transformation

from .types import (
    LiteralConversion,
    ValueMap,
    ValueMigrationCandidate,
    ValueSelection,
    ValueTerm,
    _tuple,
)


def _portable(value: ValueTerm, budget: Budget) -> PatchValue:
    """Use the existing patch's intrinsic RDF identity, never Python value equality."""
    bound_node(value, budget)
    if isinstance(value, Literal):
        return PatchLiteral(str(value), str(literal_datatype(value)), value.language)
    return URIRef(value)


def _literal(value: PatchLiteral) -> Literal:
    """Reconstruct the exact lexical sample with the maintained term constructor."""
    return literal_term(value.lexical, datatype=value.datatype, language=value.language)


def _filters(
    selection: ValueSelection, captured: Captured, budget: Budget
) -> tuple[set[URIRef], set[URIRef] | None, set[PatchResource | None] | None]:
    """Bound every filter under the operation allowance before hashing/copying."""
    predicates = set()
    for value in _tuple(selection.predicates, budget):
        assert isinstance(value, str)
        bound_text(value, budget)
        predicates.add(URIRef(value))
    subjects = None
    if selection.subjects is not None:
        subjects = set()
        for value in _tuple(selection.subjects, budget):
            assert isinstance(value, str)
            bound_text(value, budget)
            subjects.add(URIRef(value))
    graphs: set[PatchResource | None] | None = None
    if selection.graphs is not None:
        graphs = set()
        for value in _tuple(selection.graphs, budget):
            if isinstance(value, BNode):
                bound_node(value, budget)
                graphs.add(captured.reference(value))
            elif isinstance(value, URIRef):
                bound_node(value, budget)
                graphs.add(URIRef(value))
            else:
                graphs.add(None)
    return predicates, subjects, graphs


def _supported(action: LiteralConversion, budget: Budget) -> bool:
    """Qualify only declared string interpretation and decimal/integer retagging."""
    for value in (action.source_datatype, action.target_datatype):
        bound_text(value, budget)
    source, target = action.source_datatype, action.target_datatype
    if source == str(XSD.string):
        return target in (str(XSD.string), str(XSD.integer))
    return all(
        value == str(XSD.decimal)
        or datatype_relation(value, str(XSD.integer), budget=budget)
        in ("equal", "subset")
        for value in (source, target)
    )


def _evidence(quad: PatchQuad, budget: Budget) -> str:
    """Bound exact source-quad evidence before canonical ASCII JSON allocation."""
    data = {"quad": quad_data(quad, budget)}
    size = json_bound(data, budget)
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Migration sample evidence exceeds max_bytes")
    budget.consume("bytes", size)
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def migrate_values(
    source: PreparedSource,
    selection: ValueSelection,
    action: LiteralConversion | ValueMap,
    *,
    allow_merge: bool = False,
    before_checks: Sequence[MigrationCheck] = (),
    after_checks: Sequence[MigrationCheck] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 10.0,
    cancel: threading.Event | None = None,
) -> ValueMigrationCandidate:
    """Prepare an atomic lexical/exact rewrite and independently check its candidate.

    Conversions preserve the original lexical characters; source and target
    lexical checks must both establish validity. Maps change only exact selected
    objects. No casts, canonicalization, inferred aliases or inferred precision
    equivalence occur. Unsupported conversions, unexercised selections, invalid
    samples and unapproved term collapse publish no partial candidate.
    """
    if (
        not isinstance(selection, ValueSelection)
        or not isinstance(action, LiteralConversion | ValueMap)
        or type(allow_merge) is not bool
    ):
        raise InputError(
            "Value migrations require typed selection/action and merge flag"
        )
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children: list[Report] = []
    patch = None
    transformed = None
    selected = changed = unchanged = rejected = 0
    try:
        captured = capture_source(source, "base:", operation)
        predicates, subjects, graphs = _filters(selection, captured, operation)
        mapping: dict[PatchValue, PatchValue] = {}
        if isinstance(action, LiteralConversion):
            if not _supported(action, operation):
                raise PatchUnavailable("The declared lexical conversion is unsupported")
        else:
            for entry in _tuple(action.entries, operation):
                assert isinstance(entry, tuple)
                operation.consume("work")
                mapping[_portable(entry[0], operation)] = _portable(entry[1], operation)
        output: dict[PatchQuad, PatchQuad] = {}
        for quad in captured.witness.quads:
            operation.consume("work")
            value = quad.value
            in_scope = (
                quad.predicate in predicates
                and (subjects is None or quad.subject in subjects)
                and (graphs is None or quad.graph in graphs)
            )
            matched = in_scope and (
                isinstance(action, LiteralConversion) or value in mapping
            )
            target = value
            if matched:
                selected += 1
                accepted = True
                if isinstance(action, LiteralConversion):
                    if (
                        not isinstance(value, PatchLiteral)
                        or value.datatype != action.source_datatype
                        or value.language is not None
                    ):
                        issues.append(
                            Issue(
                                "rdf.value_migration_rejected",
                                "Selected object does not satisfy "
                                "the declared source datatype",
                                evidence_json=_evidence(quad, operation),
                            )
                        )
                        accepted = False
                    else:
                        target = PatchLiteral(value.lexical, action.target_datatype)
                else:
                    target = mapping[value]
                    old_language = (
                        value.language if isinstance(value, PatchLiteral) else None
                    )
                    new_language = (
                        target.language if isinstance(target, PatchLiteral) else None
                    )
                    if (
                        old_language != new_language
                        and not action.allow_language_change
                    ):
                        issues.append(
                            Issue(
                                "rdf.value_migration_rejected",
                                "An exact rewrite changes language "
                                "without explicit permission",
                                evidence_json=_evidence(quad, operation),
                            )
                        )
                        accepted = False
                samples = (
                    (value, target)
                    if isinstance(action, LiteralConversion)
                    else (target,)
                )
                for sample in samples:
                    if isinstance(sample, PatchLiteral):
                        operation.consume("work", len(sample.lexical) + 1)
                        operation.consume("states")
                        checked = check_literal(_literal(sample), budget=operation)
                        children.append(checked)
                        issues.extend(checked.issues)
                        if not checked.verified:
                            issues.append(
                                Issue(
                                    "rdf.value_migration_rejected",
                                    "The selected sample lacks verified "
                                    "source/target lexical validity",
                                    kind="finding"
                                    if checked.complete
                                    else "unverified",
                                    evidence_json=_evidence(quad, operation),
                                )
                            )
                        accepted = accepted and checked.verified
                if not accepted:
                    rejected += 1
                elif target == value:
                    unchanged += 1
                else:
                    changed += 1
            operation.consume("states")
            candidate_quad = replace(quad, value=target)
            previous = output.get(candidate_quad)
            if previous is not None and previous != quad and not allow_merge:
                rejected += 1
                issues.append(
                    Issue(
                        "rdf.value_migration_collision",
                        "Distinct object terms would collapse "
                        "without intentional merge permission",
                        evidence_json=_evidence(quad, operation),
                    )
                )
            output[candidate_quad] = quad
        if not selected:
            issues.append(
                Issue(
                    "rdf.value_migration_unverified",
                    "The required selected value migration was not exercised",
                    kind="unverified",
                )
            )
        if not any(i.kind != "finding" or i.severity == "error" for i in issues):
            operation.consume("work", len(output) + len(captured.witness.quads))
            operation.consume("states", len(output) + len(captured.witness.quads))
            before = set(captured.witness.quads)
            after = set(output)
            target_witness = replace(captured.witness, quads=tuple(after))
            patch = DatasetPatch(
                captured.witness,
                target_witness,
                removals=tuple(before - after),
                additions=tuple(after - before),
            )
            bindings = []
            seen: set[str] = set()
            for node, reference in captured.owned.values():
                operation.consume("work")
                if reference.token not in seen:
                    operation.consume("states")
                    seen.add(reference.token)
                    bindings.append(PatchBlankBinding(reference.token, node))
            checkpoint(operation.deadline, cancel)
            remaining = operation.deadline - time.monotonic()
            if remaining <= 0:
                raise LimitExceeded("Value migration exceeded its deadline")
            transformed = apply_rdf_patch(
                source,
                patch,
                blank_bindings=tuple(bindings),
                budget=operation,
                timeout=remaining,
                cancel=cancel,
            )
            issues.extend(transformed.report.issues)
            children.append(transformed.report)
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except PatchUnavailable as error:
        try:
            issues.append(
                Issue("rdf.value_migration_unverified", str(error), kind="unverified")
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    raw = Report(
        "rdf.value_migration",
        selected,
        tuple(issues),
        requested=max(1, selected),
        children=tuple(children),
        regime="exact-selected-object-migration",
    )
    available = transformed.source if transformed is not None and raw.verified else None
    finished = finish_migration(
        source,
        Transformation(available, raw),
        patch,
        before_checks=before_checks,
        after_checks=after_checks,
        budget=operation,
    )
    return ValueMigrationCandidate(
        finished.source,
        finished.patch,
        finished.before,
        finished.after,
        finished.report,
        selected,
        changed,
        unchanged,
        rejected,
        _metadata=finished._metadata,
        _checked_source=finished._checked_source,
    )
