"""Compile memberships once and select by an actual captured record's text trait."""

import json
from collections.abc import Sequence

from bijux_ontology._json import owned_json
from bijux_ontology._jsonpaths import SourcePath, at_path, bound_path, location
from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import operation
from bijux_ontology.execution.inputs import capture
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)

from .algebra import compile_allowed
from .types import BindingCase, BindingSelection, CompiledBindingCase, CompiledBindings


def _text(value: str, budget: Budget) -> str:
    """Policy text is exact native text, without RDF-kind erasure or normalization."""
    if type(value) is not str:
        raise InputError("Binding policy names and discriminator values require text")
    bound_text(value, budget)
    return value


def compile_bindings(
    cases: Sequence[BindingCase],
    *,
    discriminator: SourcePath,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> CompiledBindings:
    """Compile a finite exact-text discriminator table and all required sources.

    Membership operands come from existing selections/explicit enumerations.
    Names and discriminator values must be unique. There is no default case,
    predicate inference, regex condition or arbitrary callback interpretation.
    """
    owned_budget = operation(limits, budget)
    issues = IssueCollector(owned_budget)
    children: list[Report] = []
    compiled: list[CompiledBindingCase] = []
    path: SourcePath = ()
    checked = requested = 0
    complete = False
    try:
        bound_path(discriminator, owned_budget)
        if not discriminator:
            raise InputError("A record discriminator needs a concrete nonroot path")
        path = tuple(discriminator)
        original = capture(cases, owned_budget)
        requested = len(original)
        if not original:
            raise InputError("Conditional binding requires a nonempty case inventory")
        owned_budget.consume("states", 8 * requested)
        owned_budget.consume("bytes", 512 * requested)
        captured = []
        names: set[str] = set()
        values: set[str] = set()
        for case in original:
            if not isinstance(case, BindingCase):
                raise InputError("Expected typed binding cases")
            name, equals = (
                _text(case.name, owned_budget),
                _text(case.equals, owned_budget),
            )
            if not name or name in names or equals in values:
                raise InputError("Binding names and trait values must be unique")
            names.add(name)
            values.add(equals)
            captured.append((name, equals, case.allowed))
        for name, equals, expression in captured:
            enumeration = compile_allowed(expression, budget=owned_budget)
            compiled.append(CompiledBindingCase(name, equals, enumeration))
            checked += 1
            assert enumeration.report is not None
            children.append(enumeration.report)
            retain_report(enumeration.report, owned_budget, issues)
        complete = all(case.enumeration.qualified for case in compiled)
        owned_budget.consume("work", 0)
    except (LimitExceeded, MemoryError, RecursionError):
        issues.exhausted("Conditional binding compilation exceeded its allowance")
    except InputError:
        try:
            issues.append(
                Issue(
                    "vocabulary.binding_invalid",
                    "Binding cases require unique text traits and finite sets",
                    kind="input_error",
                )
            )
        except LimitExceeded:
            issues.exhausted("Conditional binding diagnostic allowance exhausted")
    report = Report(
        "vocabulary.binding_cases",
        checked,
        tuple(issues),
        requested=requested,
        complete=complete and checked == requested,
        children=tuple(children),
        regime="finite-text-discriminator-bindings",
    )
    return CompiledBindings._owned(path, tuple(compiled), report)


def select_binding(
    bindings: CompiledBindings,
    record: object,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> BindingSelection:
    """Capture an actual JSON object and select exactly one compiled text case.

    Missing, non-text or multiple-valued traits remain required unknown; a known
    text value with no case is a finding. No permissive fallback exists. Native
    JSON fields and concrete paths are used without guessing JSON-LD term roles.
    The finite snapshot profile reserves conservative ASCII escaping before
    serialization; oversized snapshots produce exhaustion before allocation.

    The returned document is independent of caller mutation. Its enumeration
    carries all required case/condition reports and can be reused unchanged in
    ValueConstraints for native output/RDF checks. closed_values() guards the
    ordinary ShaclValue(in_values=...) bridge; no new validator/compiler is used.
    Membership alone does not reselect a case for unrelated later data. Check the
    captured document, or retain an ordinary emission condition/SHACL hasValue
    constraint for this exact trait when reusing the membership on other inputs.
    """
    owned_budget = operation(limits, budget)
    issues = IssueCollector(owned_budget)
    children: list[Report] = []
    payload = None
    where = ""
    chosen = None
    trait = None
    enumeration = None
    checked = 0
    complete = False
    try:
        if not isinstance(bindings, CompiledBindings):
            raise InputError("Record selection requires compiled binding cases")
        retain_report(bindings.report, owned_budget, issues)
        children.append(bindings.report)
        bound_path(bindings.discriminator, owned_budget)
        where = location(bindings.discriminator)
        owned_budget.consume("states", len(bindings.cases) + 4)
        owned_budget.consume("bytes", 128 * len(bindings.cases) + 256)
        for case in bindings.cases:
            _text(case.name, owned_budget)
            _text(case.equals, owned_budget)
        owned, cost = owned_json(record, owned_budget)
        if type(owned) is not dict:
            raise InputError("An actual record requires a JSON object")
        capacity = 12 * cost + 2
        if capacity > owned_budget.limits.max_bytes:
            raise LimitExceeded("Record snapshot exceeds its conservative escape bound")
        owned_budget.consume("bytes", 2 * capacity)
        owned_budget.consume("work", capacity)
        payload = json.dumps(
            owned, ensure_ascii=True, allow_nan=False, separators=(",", ":")
        ).encode("ascii")
        checked = 1
        try:
            actual = at_path(owned, bindings.discriminator)
        except KeyError:
            actual = None
        if type(actual) is not str:
            issues.append(
                Issue(
                    "vocabulary.binding_trait_unverified",
                    "Record trait is missing, non-text or multiple-valued",
                    where,
                    kind="unverified",
                )
            )
        else:
            trait = actual
            matches = []
            for case in bindings.cases:
                owned_budget.consume("work", len(actual) + len(case.equals) + 1)
                if actual == case.equals:
                    matches.append(case)
            if len(matches) != 1:
                known_missing = not matches and bindings.report.verified
                issues.append(
                    Issue(
                        "vocabulary.binding_no_case"
                        if known_missing
                        else "vocabulary.binding_cases_unverified"
                        if not matches
                        else "vocabulary.binding_ambiguous",
                        "The actual trait does not select exactly one qualified case",
                        where,
                        kind="finding" if known_missing else "unverified",
                    )
                )
                complete = known_missing
            else:
                chosen = matches[0].name
                # Memberships are already compiled. Requalification here only
                # checks their exact stored terms under this operation's limits.
                enumeration = compile_allowed(
                    matches[0].enumeration, budget=owned_budget
                )
                assert enumeration.report is not None
                children.append(enumeration.report)
                retain_report(enumeration.report, owned_budget, issues)
                complete = enumeration.qualified
                owned_budget.consume("work", 0)
    except (LimitExceeded, MemoryError, RecursionError):
        issues.exhausted("Record binding exceeded its operation allowance")
        complete = False
    except InputError:
        try:
            issues.append(
                Issue(
                    "vocabulary.binding_record_invalid",
                    "Record binding needs a compiled plan and finite JSON object",
                    kind="input_error",
                )
            )
        except LimitExceeded:
            issues.exhausted("Record binding diagnostic allowance exhausted")
    report = Report(
        "vocabulary.record_binding",
        checked,
        tuple(issues),
        requested=1,
        complete=complete,
        children=tuple(children),
        regime="actual-text-discriminator-binding",
    )
    if enumeration is not None:
        try:
            enumeration = ValueEnumeration(
                enumeration.values,
                excluded=enumeration.excluded,
                closed=enumeration.closed,
                report=report,
            )
            owned_budget.consume("work", 0)
        except (InputError, LimitExceeded):
            issues.exhausted("Record binding intrinsic enumeration profile exceeded")
            enumeration = None
            report = Report(
                "vocabulary.record_binding",
                checked,
                tuple(issues),
                requested=1,
                complete=False,
                children=tuple(children),
                regime="actual-text-discriminator-binding",
            )
    return BindingSelection._owned(
        chosen, trait, enumeration, report, payload, owned_budget.limits
    )
