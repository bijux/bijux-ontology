"""Checked lexical repairs through the existing vocabulary migration workflow."""

import threading
from collections.abc import Sequence

from rdflib import BNode

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.migrations.candidates import MigrationCandidate, MigrationCheck
from bijux_ontology.rdf.patches.correspondence import Operation
from bijux_ontology.rdf.patches.planning import capture_items
from bijux_ontology.rdf.patches.types import PatchUnavailable
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.annotations.graphs.reading import (
    _bound,
    _same,
    inspect_annotation_graph,
)
from bijux_ontology.vocabulary.annotations.graphs.types import (
    AnnotationLiteralRepair,
    AnnotationPath,
    AnnotationStatement,
)
from bijux_ontology.vocabulary.authoring.checking import author_vocabulary

from .checking import check_annotation_hygiene
from .types import HygieneCheck


def _same_path(left: AnnotationPath, right: AnnotationPath, operation: Budget) -> bool:
    """Require original source-owned blank identities at each selected position."""
    operation.consume("work", len(left.attachments) + len(right.attachments) + 1)
    for statement in (left.root, left.statement, right.root, right.statement):
        _bound(statement, operation)
    for nodes in (left.attachments, right.attachments):
        for node in nodes:
            bound_node(node, operation)
    return (
        _same(left.root.subject, right.root.subject)
        and left.root.predicate == right.root.predicate
        and _same(left.root.value, right.root.value)
        and len(left.attachments) == len(right.attachments)
        and all(
            _same(a, b)
            for a, b in zip(left.attachments, right.attachments, strict=True)
        )
        and _same(left.statement.subject, right.statement.subject)
        and left.statement.predicate == right.statement.predicate
        and _same(left.statement.value, right.statement.value)
    )


def repair_annotation_hygiene(
    checked: HygieneCheck,
    repairs: Sequence[AnnotationLiteralRepair],
    *,
    after_checks: Sequence[MigrationCheck],
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30.0,
    cancel: threading.Event | None = None,
) -> MigrationCandidate:
    """Prepare explicitly selected replacements and recheck their actual candidate.

    Caller-selected collision, language and publication scopes must be supplied
    as actual MigrationChecks; an empty inventory never approves a repair. The
    wrapper always adds attachment interpretation and the original lexical rules.
    All checks receive fresh owned snapshots through the ordinary migration API.
    Anonymous root/graph/exception correspondence across fresh snapshots is unknown;
    owned blank attachment chains under named roots remain supported. Callbacks
    are trusted in-process code with the existing migration execution boundary.
    """
    if not isinstance(checked, HygieneCheck):
        raise InputError("Lexical repair requires a typed hygiene check")
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    baseline = None
    try:
        selected = capture_items(repairs, AnnotationLiteralRepair, operation)
        publication = capture_items(after_checks, MigrationCheck, operation)
        baseline = check_annotation_hygiene(
            checked.graph, checked.rules, budget=operation
        )
        if not baseline.report.complete:
            retain_report(baseline.report, operation, issues)
            raise PatchUnavailable(
                "Repair requires complete selected lexical and attachment evidence"
            )
        if not selected:
            raise PatchUnavailable("A required lexical repair has no selected edits")
        for edit in selected:
            _bound(edit.path.root, operation)
            _bound(edit.path.statement, operation)
            for node in edit.path.attachments:
                bound_node(node, operation)
            if not any(
                _same_path(edit.path, finding.path, operation)
                for finding in baseline.findings
            ):
                raise PatchUnavailable(
                    "Repair lacks an actual selected hygiene finding"
                )
        roots: list[AnnotationStatement] = []
        for root in baseline.graph.roots:
            operation.consume("work")
            after = root
            for edit in selected:
                operation.consume("work")
                if not edit.path.attachments and edit.path.statement == root:
                    after = AnnotationStatement(
                        root.subject, root.predicate, edit.replacement
                    )
            operation.consume("states")
            roots.append(after)
        scope = baseline.graph.graph
        anonymous = isinstance(scope, BNode)
        for root in roots:
            operation.consume("work")
            anonymous |= isinstance(root.subject, BNode)
        for rule in baseline.rules:
            for excluded in rule.excluded_statements:
                operation.consume("work")
                anonymous |= isinstance(excluded.subject, BNode)

        def inspect(source: PreparedSource) -> Report:
            """Requalify the selected after-roots without matching blank labels."""
            if anonymous:
                return Report(
                    "annotations.hygiene.after",
                    issues=(
                        Issue(
                            "annotations.hygiene_unverified",
                            "Fresh callback snapshots do not establish "
                            "anonymous root/graph/exception correspondence",
                            kind="unverified",
                        ),
                    ),
                    requested=1,
                    complete=False,
                )
            return inspect_annotation_graph(
                source,
                tuple(roots),
                graph=scope,
                annotation_predicates=baseline.graph.annotation_predicates,
                budget=operation,
            ).report

        def lexical(source: PreparedSource) -> Report:
            """Run original explicit policies against actual candidate literals."""
            if anonymous:
                return inspect(source)
            selected_graph = inspect_annotation_graph(
                source,
                tuple(roots),
                graph=scope,
                annotation_predicates=baseline.graph.annotation_predicates,
                budget=operation,
            )
            return check_annotation_hygiene(
                selected_graph, baseline.rules, budget=operation
            ).report

        checks: tuple[MigrationCheck, ...] = (
            MigrationCheck("hygiene.attachments", inspect),
            MigrationCheck("hygiene.lexical", lexical),
        )
        if not publication:
            checks += (
                MigrationCheck(
                    "hygiene.publication",
                    lambda _: Report(
                        "annotations.hygiene.publication",
                        issues=(
                            Issue(
                                "annotations.hygiene_unverified",
                                "Explicit collision, language and publication "
                                "checks are required",
                                kind="unverified",
                            ),
                        ),
                        requested=1,
                        complete=False,
                    ),
                ),
            )
        return author_vocabulary(
            baseline.graph.source,
            selected,
            graph=scope,
            annotation_predicates=baseline.graph.annotation_predicates,
            before_checks=(
                MigrationCheck("hygiene.lexical", lambda _: baseline.report),
            ),
            after_checks=(*checks, *publication),
            budget=operation,
            timeout=timeout,
            cancel=cancel,
        )
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except PatchUnavailable as error:
        try:
            issues.append(
                Issue("annotations.hygiene_unverified", str(error), kind="unverified")
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return MigrationCandidate(
        None,
        None,
        (baseline.report,) if baseline is not None else (),
        (),
        Report(
            "annotations.hygiene.repair",
            issues=tuple(issues),
            requested=1,
            complete=False,
        ),
    )
