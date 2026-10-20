"""Bounded opt-in lexical checks over requalified original annotation paths."""

import threading
import unicodedata
from collections.abc import Sequence

from rdflib import Literal
from rdflib.namespace import OWL

from bijux_ontology.absence import Absent
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.vocabulary.annotations.graphs.reading import (
    _bound,
    _physical,
    _Read,
    _same,
    inspect_annotation_graph,
)
from bijux_ontology.vocabulary.annotations.graphs.types import (
    AnnotationGraph,
    AnnotationPath,
    AnnotationStatement,
)

from .types import FindingKind, HygieneCheck, HygieneFinding, HygieneRule


def _policy(rule: HygieneRule, budget: Budget) -> None:
    """Requalify constructor-owned metadata against this operation's smaller bound."""
    bound_text(rule.predicate, budget)
    for inventory in (
        rule.roles,
        rule.forbidden_characters,
        rule.forbidden_categories,
        rule.allowed_characters,
    ):
        for value in capture_items(inventory, str, budget):
            bound_text(value, budget)
    if rule.languages is not None:
        for language in capture_items(rule.languages, object, budget):
            if isinstance(language, str):
                bound_text(language, budget)
    for statement in capture_items(
        rule.excluded_statements, AnnotationStatement, budget
    ):
        _bound(statement, budget)
    if rule.normalization is not None:
        bound_text(rule.normalization.form, budget)
        bound_text(rule.normalization.version, budget)


def _selected(
    path: AnnotationPath, rule: HygieneRule, graph: AnnotationGraph, operation: Budget
) -> bool:
    """Match exact selected role and language without regional fallback or inference."""
    statement = path.statement
    if statement.predicate != rule.predicate or not isinstance(
        statement.value, Literal
    ):
        return False
    for excluded in rule.excluded_statements:
        operation.consume("work")
        _bound(excluded, operation)
        _bound(statement, operation)
        if (
            _same(statement.subject, excluded.subject)
            and statement.predicate == excluded.predicate
            and _same(statement.value, excluded.value)
        ):
            return False
    if not path.attachments:
        role = "direct"
    else:
        kinds = []
        for attachment in graph.attachments:
            operation.consume("work")
            if _same(attachment.node, path.attachments[-1]):
                kinds.append(attachment.kind)
        if kinds == [OWL.Axiom]:
            role = "axiom"
        elif kinds == [OWL.Annotation]:
            role = "annotation"
        else:
            return False
    operation.consume("work", len(rule.roles))
    if role not in rule.roles:
        return False
    if rule.languages is None:
        return True
    language = statement.value.language
    for wanted in rule.languages:
        operation.consume("work")
        if isinstance(wanted, str) and language is not None:
            operation.consume("work", len(wanted) + len(language))
        if isinstance(wanted, Absent) and language is None:
            return True
        if (
            isinstance(wanted, str)
            and language is not None
            and wanted.lower() == language.lower()
        ):
            return True
    return False


def check_annotation_hygiene(
    graph: AnnotationGraph,
    rules: Sequence[HygieneRule],
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30.0,
    cancel: threading.Event | None = None,
) -> HygieneCheck:
    """Check only explicit annotation policies without changing any original term.

    Positions are half-open Unicode codepoint offsets, not UTF-8 or grapheme
    offsets. A normalization finding spans the complete original literal; it does
    not invent a character correspondence after composition. The actual running
    Unicode version is always recorded. A requested different version is unknown.
    An empty or unexercised required policy cannot establish acceptance.
    """
    if not isinstance(graph, AnnotationGraph):
        raise InputError("Hygiene checks require an inspected annotation graph")
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    owned = AnnotationGraph(
        graph.source,
        None,
        (),
        (),
        (),
        (),
        (),
        Report("annotations.graph", requested=1, complete=False),
    )
    selected: tuple[HygieneRule, ...] = ()
    findings: list[HygieneFinding] = []
    checked = 0
    children: tuple[Report, ...] = ()
    requested = 1
    try:
        supplied = capture_items(rules, HygieneRule, operation)
        requested = max(1, len(supplied))
        for rule in supplied:
            _policy(rule, operation)
        selected = supplied
        owned = inspect_annotation_graph(
            graph.source,
            graph.roots,
            graph=graph.graph,
            annotation_predicates=graph.annotation_predicates,
            budget=operation,
        )
        retain_report(owned.report, operation, issues)
        children = (owned.report,)
        _, physical = _physical(owned.source, owned.graph, operation)
        reader = _Read(physical, owned.annotation_predicates, operation)
        if not selected:
            issues.append(
                Issue(
                    "annotations.hygiene_unverified",
                    "No required hygiene policies were selected",
                    kind="unverified",
                )
            )
        for number, rule in enumerate(selected):
            operation.consume("work")
            if (
                rule.normalization is not None
                and rule.normalization.version != unicodedata.unidata_version
            ):
                issues.append(
                    Issue(
                        "annotations.hygiene_unverified",
                        "Requested Unicode database version is unavailable",
                        kind="unverified",
                    )
                )
                continue
            operation.consume(
                "states",
                len(rule.allowed_characters)
                + len(rule.forbidden_characters)
                + len(rule.forbidden_categories),
            )
            allowed, forbidden, categories = (
                frozenset(rule.allowed_characters),
                frozenset(rule.forbidden_characters),
                frozenset(rule.forbidden_categories),
            )
            matched = 0
            for path in owned.paths:
                operation.consume("work")
                if not _selected(path, rule, owned, operation):
                    continue
                if not reader.accepted(path.statement):
                    issues.append(
                        Issue(
                            "annotations.hygiene_unverified",
                            "Selected predicate lacks an annotation-property role",
                            kind="unverified",
                        )
                    )
                    continue
                literal = path.statement.value
                assert isinstance(literal, Literal)
                bound_text(literal, operation)
                matched += 1
                text = str(literal)

                def record(
                    kind: FindingKind,
                    start: int,
                    end: int,
                    selected_path: AnnotationPath = path,
                    selected_number: int = number,
                ) -> None:
                    """Retain this witnessed span before inspecting later text."""
                    operation.consume("states")
                    operation.consume("bytes", 96)
                    finding = HygieneFinding(
                        selected_path, selected_number, kind, start, end
                    )
                    issues.append(
                        Issue(
                            "annotations.hygiene_violation",
                            "Selected annotation violates its explicit lexical policy",
                            f"annotation[{selected_number}]/{start}:{end}",
                        )
                    )
                    findings.append(finding)

                if rule.edge_whitespace:
                    start, end = 0, len(text)
                    while start < end:
                        operation.consume("work")
                        if not text[start].isspace():
                            break
                        start += 1
                    while end > start:
                        operation.consume("work")
                        if not text[end - 1].isspace():
                            break
                        end -= 1
                    for left, right in ((0, start), (end, len(text))):
                        run = left
                        for offset in range(left, right):
                            operation.consume("work")
                            if text[offset] in allowed:
                                if run < offset:
                                    record("edge_whitespace", run, offset)
                                run = offset + 1
                        if run < right:
                            record("edge_whitespace", run, right)
                for offset, char in enumerate(text):
                    operation.consume("work")
                    if char not in allowed and (
                        char in forbidden or unicodedata.category(char) in categories
                    ):
                        record("forbidden_character", offset, offset + 1)
                if rule.normalization is not None:
                    operation.consume("work", 18 * len(text) + 1)
                    if not unicodedata.is_normalized(rule.normalization.form, text):
                        record("normalization", 0, len(text))
            if not matched:
                issues.append(
                    Issue(
                        "annotations.hygiene_unverified",
                        "A required hygiene policy selected no literal occurrences",
                        kind="unverified",
                    )
                )
            checked += 1
    except LimitExceeded as error:
        issues.exhausted(str(error))
    return HygieneCheck(
        owned,
        selected,
        tuple(findings),
        unicodedata.unidata_version,
        Report(
            "annotations.hygiene",
            checked,
            tuple(issues),
            requested=requested,
            children=children,
            regime="selected-annotation-lexical-policy",
            families=(
                "unicode:" + unicodedata.unidata_version,
                "whitespace:python-isspace",
            ),
        ),
    )
