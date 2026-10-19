"""Checked simultaneous declaration/data replacements on one authorized snapshot."""

import threading
from collections.abc import Sequence
from dataclasses import replace
from typing import cast

from rdflib import BNode, URIRef
from rdflib.term import Node

from bijux_ontology.declarations.types import ModelDeclaration, TermDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.models.contracts.checking import check_models
from bijux_ontology.rdf.migrations.candidates import (
    MigrationCandidate,
    MigrationCheck,
    finish_migration,
)
from bijux_ontology.rdf.patches.applying import _materialize
from bijux_ontology.rdf.patches.planning import capture_source
from bijux_ontology.rdf.patches.types import (
    DatasetPatch,
    PatchBlank,
    PatchLiteral,
    PatchQuad,
    PatchUnavailable,
    PatchValue,
)
from bijux_ontology.rdf.roles.classification import classify_statement
from bijux_ontology.rdf.roles.types import TermPosition, TermRole
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import literal_term
from bijux_ontology.rdf.transforms import Transformation
from bijux_ontology.vocabulary.checking import check_vocabulary
from bijux_ontology.vocabulary.facts.types import FactPredicate
from bijux_ontology.vocabulary.registry.types import IdentifierRegistry
from bijux_ontology.vocabulary.sources.view import Vocabulary
from bijux_ontology.workflows.upgrades.uses import index_uses

from .declarations import own_models, rewrite_declarations
from .selection import Selection
from .types import (
    ApprovedReplacement,
    ReplacementCheck,
    ReplacementPlan,
    ReplacementState,
)


def plan_replacements(
    before_vocabulary: Vocabulary,
    after_vocabulary: Vocabulary,
    approvals: Sequence[ApprovedReplacement],
    *,
    ownership: IdentifierRegistry,
    data: PreparedSource,
    terms: Sequence[TermDeclaration] = (),
    models: Sequence[ModelDeclaration] = (),
    endpoint_roles: Sequence[FactPredicate] = (),
    before_checks: Sequence[ReplacementCheck] = (),
    after_checks: Sequence[ReplacementCheck] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 10.0,
    cancel: threading.Event | None = None,
) -> ReplacementPlan:
    """Build proposals only from explicit approvals and actual semantic occurrences.

    Callbacks receive original discriminator facts or fresh authorized check data.
    Explicit after checks are required for acceptance. No replacement hints,
    class/individual equivalence, global prefix rewrite or network is inferred.
    """
    if not isinstance(ownership, IdentifierRegistry):
        raise InputError("Replacement ownership requires an IdentifierRegistry")
    if not isinstance(before_vocabulary, Vocabulary) or not isinstance(
        after_vocabulary, Vocabulary
    ):
        raise InputError("Replacement plans require explicit owned vocabulary versions")
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children: list[Report] = []
    before_state = after_state = None
    patch = None
    selection = None
    completed = 0
    required = 1
    transformed_source = None
    try:
        rules = capture_items(approvals, ApprovedReplacement, operation)
        required = max(1, len(rules))
        selected_terms = capture_items(terms, TermDeclaration, operation)
        for term in selected_terms:
            for text in (term.iri, term.location, *term.kinds):
                bound_text(text, operation)
        selected_models = own_models(
            capture_items(models, ModelDeclaration, operation), operation
        )
        checks_before = capture_items(before_checks, ReplacementCheck, operation)
        checks_after = capture_items(after_checks, ReplacementCheck, operation)
        for check in (*checks_before, *checks_after):
            bound_text(check.name, operation)
        endpoints: dict[URIRef, FactPredicate] = {}
        for authored in capture_items(endpoint_roles, FactPredicate, operation):
            bound_text(authored.iri, operation)
            for role in (authored.subject_role, authored.object_role):
                if role is not None:
                    bound_text(role, operation)
            predicate = URIRef(authored.iri)
            if predicate in endpoints:
                raise InputError("Repeated replacement endpoint-role predicates")
            endpoints[predicate] = authored
        captured = capture_source(data, "replacement:", operation)
        before_state = ReplacementState(
            selected_terms,
            selected_models,
            _materialize(captured.witness, data, operation),
        )
        before = before_vocabulary._read(("local", "imports"), budget=operation).graph
        after = after_vocabulary._read(("local", "imports"), budget=operation).graph
        selection = Selection(
            rules, ownership, before, after, captured.witness, operation, issues
        )
        if not rules:
            selection.unknown("No approved replacement intent was supplied")
        use_index = index_uses(selected_terms, selected_models, budget=operation)
        retain_report(use_index.report, operation, issues)
        children.append(use_index.report)
        blanks: dict[str, BNode] = {}
        for original, reference in captured.owned.values():
            operation.consume("work")
            operation.consume("states")
            blanks[reference.token] = original

        def node(value: PatchValue) -> Node:
            """
            Recover exact capture-owned roles without interpreting portable blank
            spelling.
            """
            operation.consume("work")
            if isinstance(value, PatchBlank):
                return blanks[value.token]
            if isinstance(value, PatchLiteral):
                return literal_term(
                    value.lexical, datatype=value.datatype, language=value.language
                )
            return value

        classified: list[
            tuple[PatchQuad, dict[TermPosition, tuple[TermRole, ...]]]
        ] = []
        for quad in captured.witness.quads:
            operation.consume("work")
            rule = endpoints.get(quad.predicate)
            occurrences = classify_statement(
                (node(quad.subject), quad.predicate, node(quad.value)),
                graph=cast(
                    URIRef | BNode | None,
                    node(quad.graph) if quad.graph is not None else None,
                ),
                subject_role=rule.subject_role if rule is not None else None,
                object_role=rule.object_role if rule is not None else None,
                budget=operation,
            )
            position_lists: dict[TermPosition, list[TermRole]] = {}
            for occurrence in occurrences:
                operation.consume("work")
                operation.consume("states", 2)
                operation.consume("bytes", 96)
                position_lists.setdefault(occurrence.position, []).append(
                    occurrence.role
                )
                if isinstance(occurrence.term, URIRef):
                    selection.present.add((occurrence.term, occurrence.role))
            operation.consume("states")
            operation.consume("bytes", 128)
            classified.append(
                (
                    quad,
                    {
                        position: tuple(roles)
                        for position, roles in position_lists.items()
                    },
                )
            )
        for use in use_index.entries:
            operation.consume("work")
            operation.consume("states")
            use_role = "property" if use.role == "predicate" else use.role
            if use_role != "term":
                selection.present.add((URIRef(use.iri), cast(TermRole, use_role)))
        new_terms, new_models = rewrite_declarations(
            selected_terms, selected_models, use_index, selection
        )
        quads = []
        for quad, positions in classified:
            operation.consume("work")
            changed: dict[str, URIRef] = {}
            for position, attribute in (
                ("subject", "subject"),
                ("predicate", "predicate"),
                ("object", "value"),
                ("graph", "graph"),
            ):
                value = getattr(quad, attribute)
                if isinstance(value, URIRef):
                    changed[attribute] = selection.rewrite(
                        value,
                        positions.get(cast(TermPosition, position), ("unknown",)),
                        quad=quad,
                        position=cast(TermPosition, position),
                    )
            if (
                isinstance(quad.value, PatchLiteral)
                and URIRef(quad.value.datatype) in selection.rules
            ):
                selection.unknown(
                    "Literal datatype replacement requires an explicit value migration"
                )
            operation.consume("states")
            quads.append(replace(quad, **changed))
        if not selection.decisions:
            selection.unknown("No actual approved entity-role occurrence was selected")
        for checked in (
            check_vocabulary(
                after_vocabulary, new_terms, allow_empty=True, budget=operation
            ),
            check_models(
                new_models, after_vocabulary, allow_empty=True, budget=operation
            ),
        ):
            retain_report(checked, operation, issues)
            children.append(checked)
        # All exact records are already captured and paid before hashing the edit sets.
        operation.consume("work", len(quads) + len(captured.witness.quads))
        operation.consume("states", len(quads) + len(captured.witness.quads))
        target = replace(captured.witness, quads=tuple(dict.fromkeys(quads)))
        base_set, target_set = set(captured.witness.quads), set(target.quads)
        patch = DatasetPatch(
            captured.witness,
            target,
            tuple(quad for quad in captured.witness.quads if quad not in target_set),
            tuple(quad for quad in target.quads if quad not in base_set),
        )
        completed = required
        raw = Report(
            "upgrade.replacements",
            completed,
            tuple(issues),
            requested=required,
            children=tuple(children),
        )
        if raw.verified:
            transformed_source = _materialize(target, data, operation)
            after_state = ReplacementState(new_terms, new_models, transformed_source)

            def checks(
                selected: tuple[ReplacementCheck, ...], state: ReplacementState
            ) -> tuple[MigrationCheck, ...]:
                """
                Each trusted callback sees fresh data and fresh declaration literal
                objects.
                """
                result = []
                for declaration in selected:

                    def run(
                        source: PreparedSource,
                        declaration: ReplacementCheck = declaration,
                    ) -> Report:
                        """
                        Bind this explicit side's declarations to the gate's fresh
                        snapshot.
                        """
                        return declaration.check(
                            ReplacementState(
                                state.terms, own_models(state.models, operation), source
                            )
                        )

                    operation.consume("states")
                    result.append(MigrationCheck(declaration.name, run))
                return tuple(result)

            candidate = finish_migration(
                data,
                Transformation(transformed_source, raw),
                patch,
                before_checks=checks(checks_before, before_state),
                after_checks=checks(checks_after, after_state),
                budget=operation,
            )
            return ReplacementPlan(
                before_state,
                after_state,
                tuple(selection.decisions),
                candidate,
                candidate.report,
            )
    except PatchUnavailable:
        try:
            issues.append(
                Issue(
                    "upgrade.replacement_unverified",
                    "The selected data has no exact authorized patch scope",
                    kind="unverified",
                )
            )
        except LimitExceeded as error:
            issues.exhausted(str(error))
    except LimitExceeded as error:
        issues.exhausted(str(error))
    raw = Report(
        "upgrade.replacements",
        completed,
        tuple(issues),
        requested=required,
        children=tuple(children),
    )
    candidate = MigrationCandidate(None, patch, (), (), raw)
    return ReplacementPlan(
        before_state,
        None,
        tuple(selection.decisions) if selection is not None else (),
        candidate,
        raw,
    )
