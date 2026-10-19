"""Explicit maintained markup inspection and selected XML canonical comparison."""

import time
from threading import Event
from typing import Literal as Choice

from rdflib import Literal
from rdflib.namespace import RDF

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.terms import bound_node

from .protocol import run_markup
from .types import (
    MarkupCanonicalization,
    MarkupComparison,
    MarkupInspection,
    MarkupRelation,
)


def inspect_markup_literal(
    literal: Literal,
    *,
    canonicalization: MarkupCanonicalization | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 10,
    cancel: Event | None = None,
) -> MarkupInspection:
    """Inspect original self-contained markup without creating RDF statements.

    XML uses strict maintained fragment parsing without DTD/entity resources,
    recovery, inherited language/base or supplied namespace context. The selected
    canonical profile is exclusive XML C14N 1.0 with comments and empty inclusive
    prefix list. A neutral synthetic envelope supports text and multiple siblings;
    only that fixed envelope is removed from actual canonical output.

    HTML is lxml/libxml2 fragment inspection, not the HTML5 RDF value algorithm or
    a preservation/validity claim. Documented recovery remains parser behavior;
    native resource/depth truncation is exhausted, without a partial result.
    Empty fragments are real successfully processed literal values. Counters cover
    owned operations; native internals have an actual isolated deadline/cancel.
    """
    if not isinstance(literal, Literal):
        raise InputError("Markup inspection requires an actual RDF Literal")
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    original = None
    kind: Choice["xml", "html"] | None = None
    canonical = None
    text = ""
    names: tuple[str, ...] = ()
    comments = 0
    backend = ""
    checked = 0
    complete = False
    regime = "markup-fragment-inspection"
    try:
        bound_node(literal, operation)
        original = literal
        if canonicalization not in (None, "xml-exc-c14n-with-comments"):
            raise ResourceError("Requested markup canonicalization is unavailable")
        if literal.datatype == RDF.XMLLiteral and literal.language is None:
            kind = "xml"
        elif literal.datatype == RDF.HTML and literal.language is None:
            kind = "html"
        else:
            raise InputError("Selected literal needs rdf:XMLLiteral or rdf:HTML")
        regime = (
            "xml-exc-c14n-1.0-with-comments"
            if canonicalization is not None and kind == "xml"
            else "lxml-" + kind + "-fragment-inspection"
        )
        reply = run_markup(str(literal), kind, canonicalization, operation)
        backend = reply.get("backend", "")
        if reply["status"] == "input_error":
            checked, complete = 1, True
            issues.append(
                Issue(
                    "literal.markup_invalid",
                    "Maintained markup processor rejected the fragment syntax",
                    kind="input_error",
                )
            )
        elif reply["status"] == "unavailable":
            raise ResourceError("Markup inspection requires bijux-ontology[markup]")
        elif reply["status"] != "result":
            raise ResourceError(
                "Markup declarations, resolution or canonical conditions "
                "are unsupported"
            )
        else:
            # All returned storage and typed fields were qualified by run_markup.
            canonical_text = reply["canonical"]
            canonical = (
                canonical_text.encode("utf-8") if canonical_text is not None else None
            )
            text, names, comments = (
                reply["text"],
                tuple(reply["names"]),
                reply["comments"],
            )
            checked, complete = 1, True
    except (LimitExceeded, MemoryError, RecursionError):
        issues.exhausted("Markup literal inspection exceeded its operation limits")
    except (InputError, ResourceError) as error:
        try:
            issues.append(
                Issue(
                    "literal.markup_unverified"
                    if isinstance(error, ResourceError)
                    else "literal.markup_invalid",
                    str(error),
                    kind="unverified"
                    if isinstance(error, ResourceError)
                    else "input_error",
                )
            )
        except LimitExceeded:
            issues.exhausted("Markup literal diagnostic allowance exhausted")
    return MarkupInspection(
        original,
        Report(
            "rdf.markup_literal",
            checked,
            tuple(issues),
            requested=1,
            complete=complete,
            regime=regime,
        ),
        kind,
        canonical,
        text,
        names,
        comments,
        backend,
    )


def compare_markup_values(
    before: Literal,
    after: Literal,
    *,
    regime: MarkupCanonicalization,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 10,
    cancel: Event | None = None,
) -> MarkupComparison:
    """Compare selected canonical XML values, never exact RDF term identity.

    Raw before/after inspection reports remain available. Different canonical
    bytes establish a difference only within the explicit supported profile;
    unavailable or incomplete parsing/canonicalization is indeterminate. HTML
    has no supported canonical value comparison in this operation.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    left = right = None
    children: list[Report] = []
    relation: MarkupRelation = "indeterminate"
    checked = 0
    try:
        bound_text(regime, operation)
        if regime != "xml-exc-c14n-with-comments":
            raise ResourceError("Requested markup comparison regime is unavailable")
        for label, literal in (("before", before), ("after", after)):
            operation.consume("states")
            operation.consume("bytes", 64)
            result = inspect_markup_literal(
                literal,
                canonicalization=regime,
                budget=operation,
                timeout=max(0.000001, operation.deadline - time.monotonic()),
                cancel=cancel,
            )
            if label == "before":
                left = result
            else:
                right = result
            children.append(result.report)
            retain_report(result.report, operation, issues)
        if (
            left is not None
            and right is not None
            and left.report.verified
            and right.report.verified
            and left.canonical is not None
            and right.canonical is not None
        ):
            operation.consume("work", len(left.canonical) + len(right.canonical) + 1)
            relation = "equal" if left.canonical == right.canonical else "different"
            checked = 1
            if relation == "different":
                issues.append(
                    Issue(
                        "literal.markup_value_changed",
                        "Selected canonical XML fragment values differ",
                    )
                )
    except (LimitExceeded, MemoryError, RecursionError):
        issues.exhausted("Markup value comparison exceeded its operation limits")
    except (InputError, ResourceError):
        try:
            issues.append(
                Issue(
                    "literal.markup_unverified",
                    "Requested markup comparison regime is unavailable",
                    kind="unverified",
                )
            )
        except LimitExceeded:
            issues.exhausted("Markup comparison diagnostic allowance exhausted")
    return MarkupComparison(
        left,
        right,
        relation,
        Report(
            "rdf.markup_values",
            checked,
            tuple(issues),
            requested=1,
            complete=bool(checked),
            children=tuple(children),
            regime="xml-exc-c14n-1.0-with-comments",
        ),
    )
