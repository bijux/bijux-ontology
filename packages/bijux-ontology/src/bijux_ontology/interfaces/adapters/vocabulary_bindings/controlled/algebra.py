"""Three-valued finite membership over existing controlled-vocabulary evidence."""

from dataclasses import dataclass

from rdflib import Literal, URIRef

from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import operation
from bijux_ontology.execution.inputs import capture
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.terms import bound_node, term_key
from bijux_ontology.skos.subsets.types import ConceptSubset

from .types import (
    AllowedDifference,
    AllowedExpression,
    AllowedIntersection,
    AllowedUnion,
)

_Key = tuple[str | None, ...]


@dataclass(frozen=True)
class _Leaf:
    """Captured known positives/negatives and a qualified absence boundary."""

    values: frozenset[_Key]
    excluded: frozenset[_Key]
    closed: bool
    qualified: bool


def _leaf(
    value: ValueEnumeration | ConceptSubset,
    budget: Budget,
    issues: IssueCollector,
    children: list[Report],
    universe: dict[_Key, URIRef | Literal],
) -> _Leaf:
    """Guard raw reports and exact term metadata before key creation/retention."""
    if type(value.closed) is not bool:
        raise InputError("Allowed-set closure must be explicit")
    source_report = value.report
    if source_report is not None:
        retain_report(source_report, budget, issues)
        children.append(source_report)
    elif isinstance(value, ConceptSubset):
        raise InputError("A selected concept subset requires its raw report")
    keys = []
    for raw in (value.values, value.excluded):
        members = capture(raw, budget)
        budget.consume("states", 3 * len(members))
        budget.consume("bytes", 192 * len(members))
        selected = set()
        for term in members:
            if not isinstance(term, URIRef | Literal):
                raise InputError(
                    "Allowed values require named IRIs or literal RDF terms"
                )
            bound_node(term, budget)
            # The intrinsic key allocates lexical/metadata text only after its
            # original RDF term has passed the current operation's scalar bound.
            size = len(term)
            if isinstance(term, Literal):
                size += len(term.datatype or "") + len(term.language or "")
            budget.consume("bytes", 128 + 4 * size)
            budget.consume("work", size + 1)
            key = term_key(term)
            selected.add(key)
            universe.setdefault(key, term)
        keys.append(frozenset(selected))
    if keys[0] & keys[1]:
        raise InputError("Allowed-set evidence cannot permit and exclude one term")
    return _Leaf(
        keys[0],
        keys[1],
        value.closed,
        value.closed and (source_report is None or source_report.verified),
    )


def compile_allowed(
    expression: AllowedExpression,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ValueEnumeration:
    """Compile finite source selections and explicit terms without graph traversal.

    Use select_concepts for supplied schemes, collections or rooted subsets,
    then compose those actual ConceptSubset results with explicit ValueEnumeration
    leaves. Known values/exclusions survive incomplete evidence. In particular,
    left-minus-right permits a left member only with a proved right exclusion;
    absence from an open right operand remains unknown, never a negative fact.

    Every leaf report remains required. Closure is conservative: all leaves must
    declare closure and their reports must verify before complete qualification.
    This preserves ValueEnumeration.qualified and never makes an open allowed set
    suitable for a closed SHACL list by discarding its report.
    """
    owned_budget = operation(limits, budget)
    issues = IssueCollector(owned_budget)
    reports: list[Report] = []
    universe: dict[_Key, URIRef | Literal] = {}
    leaves: dict[int, _Leaf] = {}
    operands: dict[int, tuple[AllowedExpression, ...]] = {}
    ordered: list[AllowedExpression] = []
    values: tuple[URIRef | Literal, ...] = ()
    excluded: tuple[URIRef | Literal, ...] = ()
    closed = complete = False
    checked = 0
    try:
        pending = [(expression, False, 0)]
        active: set[int] = set()
        finished: set[int] = set()
        while pending:
            node, leaving, depth = pending.pop()
            owned_budget.consume("work")
            if depth > owned_budget.limits.max_depth:
                raise LimitExceeded("Allowed-set expression exceeds max_depth")
            identity = id(node)
            if leaving:
                active.remove(identity)
                finished.add(identity)
                ordered.append(node)
                checked += 1
                continue
            if identity in finished:
                continue
            if identity in active:
                raise InputError("Allowed-set expressions cannot contain cycles")
            owned_budget.consume("states", 4)
            owned_budget.consume("bytes", 256)
            if isinstance(node, ValueEnumeration | ConceptSubset):
                leaves[identity] = _leaf(node, owned_budget, issues, reports, universe)
                ordered.append(node)
                finished.add(identity)
                checked += 1
                continue
            if isinstance(node, AllowedUnion | AllowedIntersection):
                children = capture(node.operands, owned_budget)
                if not children:
                    raise InputError("Allowed-set operators require nonempty operands")
            elif isinstance(node, AllowedDifference):
                children = (node.left, node.right)
            else:
                raise InputError("Unsupported allowed-set expression")
            owned_budget.consume("states", 3 * len(children))
            owned_budget.consume("bytes", 192 * len(children))
            operands[identity] = children
            active.add(identity)
            pending.append((node, True, depth))
            pending.extend((child, False, depth + 1) for child in reversed(children))
        width = len(universe)
        cost = sum(max(1, len(operands.get(id(node), ()))) for node in ordered)
        owned_budget.consume("work", 8 * cost * max(1, width))
        owned_budget.consume("states", 8 * len(ordered) * width)
        owned_budget.consume("bytes", 256 * len(ordered) * width)
        all_keys = frozenset(universe)
        truths: dict[int, tuple[frozenset[_Key], frozenset[_Key]]] = {}
        for node in ordered:
            owned_budget.consume("work", 0)
            identity = id(node)
            if identity in leaves:
                leaf = leaves[identity]
                truths[identity] = (
                    leaf.values,
                    all_keys - leaf.values if leaf.qualified else leaf.excluded,
                )
                continue
            selected = [truths[id(child)] for child in operands[identity]]
            if isinstance(node, AllowedUnion):
                allowed = frozenset().union(*(item[0] for item in selected))
                denied = frozenset.intersection(*(item[1] for item in selected))
            elif isinstance(node, AllowedIntersection):
                allowed = frozenset.intersection(*(item[0] for item in selected))
                denied = frozenset().union(*(item[1] for item in selected))
            else:
                left, right = selected
                allowed = left[0] & right[1]
                denied = left[1] | right[0]
            truths[identity] = allowed, denied
        allowed, denied = truths[id(expression)]
        values = tuple(term for key, term in universe.items() if key in allowed)
        excluded = tuple(term for key, term in universe.items() if key in denied)
        closed = all(leaf.closed for leaf in leaves.values())
        complete = all(leaf.qualified for leaf in leaves.values())
        if not complete:
            issues.append(
                Issue(
                    "vocabulary.allowed_scope_unverified",
                    "Open or incomplete operands cannot prove allowed-set coverage",
                    kind="unverified",
                )
            )
    except (LimitExceeded, MemoryError, RecursionError):
        issues.exhausted("Controlled-set compilation exceeded its operation allowance")
        complete = closed = False
        values = excluded = ()
    except InputError:
        try:
            issues.append(
                Issue(
                    "vocabulary.allowed_invalid",
                    "Allowed sets need finite typed membership evidence",
                    kind="input_error",
                )
            )
        except LimitExceeded:
            issues.exhausted("Controlled-set diagnostic allowance exhausted")
        complete = closed = False
        values = excluded = ()
    report = Report(
        "vocabulary.allowed_set",
        checked,
        tuple(issues),
        requested=checked if complete else checked + 1,
        complete=complete,
        children=tuple(reports),
        regime="finite-controlled-set-algebra",
    )
    try:
        result = ValueEnumeration(
            values, excluded=excluded, closed=closed, report=report
        )
        owned_budget.consume("work", 0)
        return result
    except (InputError, LimitExceeded):
        issues.exhausted("Controlled-set intrinsic storage profile exceeded")
        return ValueEnumeration(
            (),
            closed=False,
            report=Report(
                "vocabulary.allowed_set",
                checked,
                tuple(issues),
                requested=checked + 1,
                complete=False,
                children=tuple(reports),
                regime="finite-controlled-set-algebra",
            ),
        )
