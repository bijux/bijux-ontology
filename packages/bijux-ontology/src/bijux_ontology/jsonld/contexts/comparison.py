"""Finite effective-definition changes, separate from sampled document behavior."""

from collections.abc import Sequence
from dataclasses import fields

from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.json.paths import bounded_location
from bijux_ontology.jsonld.contexts.preparation import (
    capture_context_json,
    capture_selections,
    context_budget,
    context_data,
    encode_context_json,
)
from bijux_ontology.jsonld.contexts.processing import inspect_context
from bijux_ontology.jsonld.processing.resources import DocumentLoader

from .types import (
    ContextChange,
    ContextComparison,
    ContextInspection,
    ContextScope,
    ContextView,
)


class _Comparison:
    """Retain only bounded effective facets and explicit uninstantiated scope."""

    def __init__(
        self,
        budget: Budget,
        terms: tuple[str, ...] | None,
        scopes: tuple[ContextScope, ...],
    ) -> None:
        """Own one comparison's selection and cumulative evidence."""
        self.budget = budget
        self.terms = terms
        self.scope_terms = {scope.term for scope in scopes}
        self.issues = IssueCollector(budget)
        self.changes: list[ContextChange] = []
        self.checked = 0
        self.incomplete = False

    def change(
        self, scope: str, term: str | None, facet: str, before: object, after: object
    ) -> None:
        """Compare already qualified scalars and retain exact changed data."""
        self.budget.consume("work")
        first, second = (
            encode_context_json(context_data(before, self.budget), self.budget),
            encode_context_json(context_data(after, self.budget), self.budget),
        )
        self.budget.consume("work", min(len(first), len(second)) + 1)
        self.checked += 1
        if first == second:
            return
        self.budget.consume("states")
        self.budget.consume("bytes", 128)
        where = bounded_location(
            (scope, "terms", term, facet)
            if term is not None
            else (scope, "defaults", facet),
            self.budget,
        )
        self.changes.append(ContextChange(scope, term, facet, first, second))
        self.issues.append(
            Issue(
                "context.definition_changed"
                if term is not None
                else "context.defaults_changed",
                "Effective context definition differs",
                where,
            )
        )

    def view(
        self,
        before: ContextView,
        after: ContextView,
        scope: str,
        defaults: bool,
        depth: int = 0,
    ) -> None:
        """Compare finite selected terms, defaults and explicit reversion states."""
        if depth > self.budget.limits.max_depth:
            raise LimitExceeded("Context comparison exceeds max_depth")
        if defaults:
            for item in fields(before.defaults):
                if (
                    item.name == "base_mapping"
                    and isinstance(before.defaults.base_mapping, str)
                    and isinstance(after.defaults.base_mapping, str)
                ):
                    # Effective resolution is compared separately from IRI spelling.
                    continue
                self.change(
                    scope,
                    None,
                    item.name,
                    getattr(before.defaults, item.name),
                    getattr(after.defaults, item.name),
                )
        self.budget.consume("states", len(before.terms) + len(after.terms))
        first = {item.term: item for item in before.terms}
        second = {item.term: item for item in after.terms}
        names = self.terms
        if names is None:
            self.budget.consume("states", len(first) + len(second))
            inventory = first.keys() | second.keys()
            self.budget.consume(
                "work",
                sum(len(name) + 1 for name in inventory)
                * max(1, len(inventory).bit_length()),
            )
            names = tuple(sorted(inventory))
        for name in names:
            self.budget.consume("work")
            a, b = first.get(name), second.get(name)
            if a is None or b is None:
                self.change(scope, name, "definition", a, b)
                continue
            for item in fields(a):
                if item.name in ("term", "scoped_context_json"):
                    continue
                self.change(
                    scope, name, item.name, getattr(a, item.name), getattr(b, item.name)
                )
            if a.scoped_context_json is not None or b.scoped_context_json is not None:
                self.change(
                    scope,
                    name,
                    "scoped_context_present",
                    a.scoped_context_json is not None,
                    b.scoped_context_json is not None,
                )
                if scope != "root" or name not in self.scope_terms:
                    self.incomplete = True
                    self.issues.append(
                        Issue(
                            "context.definition_unverified",
                            "Scoped definition has no selected effective instantiation",
                            bounded_location((scope, "terms", name), self.budget),
                            kind="unverified",
                        )
                    )
        if not defaults and not names:
            return
        if before.previous is None or after.previous is None:
            self.change(
                scope, None, "previous_context", before.previous, after.previous
            )
        else:
            self.budget.consume("bytes", len(scope) + 9)
            self.view(
                before.previous,
                after.previous,
                scope + "/previous",
                defaults,
                depth + 1,
            )


def compare_context_definitions(
    before: object,
    after: object,
    *,
    terms: Sequence[str] | None = None,
    scopes: Sequence[ContextScope] = (),
    processing_mode: str = "json-ld-1.1",
    before_base: str | None = None,
    after_base: str | None = None,
    before_inherited: object | Absent = ABSENT,
    after_inherited: object | Absent = ABSENT,
    before_loader: DocumentLoader | None = None,
    after_loader: DocumentLoader | None = None,
    compare_defaults: bool = True,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ContextComparison:
    """Compare effective definitions without a sample or all-document guarantee.

    Definition equality concerns only this finite selection and these exact
    processing inputs. Before/after resources remain independently owned even
    when the two loaders use the same logical URL.
    """
    operation = context_budget(limits, budget)
    selected: tuple[str, ...] | None = None
    selected_scopes: tuple[ContextScope, ...] = ()
    compare = _Comparison(operation, None, ())
    first = second = None
    children = []
    complete = False
    data_json = '{"changes":[]}'
    try:
        if type(compare_defaults) is not bool:
            raise InputError("compare_defaults requires explicit boolean intent")
        if terms is not None:
            selected = capture_selections(terms, operation)
            for term in selected:
                bound_text(term, operation)
            if len(set(selected)) != len(selected):
                raise InputError("Selected context term names must be unique")
        selected_scopes = capture_selections(scopes, operation)
        # inspect_context requalifies typed selections before using processor data.
        for item in selected_scopes:
            if not isinstance(item, ContextScope):
                raise InputError("Expected typed context scopes")
            bound_text(item.term, operation)
        compare = _Comparison(operation, selected, selected_scopes)
        # Capture both sides before either caller loader can run.
        owned_before, owned_after = (
            capture_context_json(before, operation),
            capture_context_json(after, operation),
        )
        inherited_before = (
            capture_context_json(before_inherited, operation)
            if before_inherited is not ABSENT
            else ABSENT
        )
        inherited_after = (
            capture_context_json(after_inherited, operation)
            if after_inherited is not ABSENT
            else ABSENT
        )
        first = inspect_context(
            owned_before,
            processing_mode=processing_mode,
            base=before_base,
            inherited_context=inherited_before,
            scopes=selected_scopes,
            document_loader=before_loader,
            budget=operation,
        )
        retain_report(first.report, operation, compare.issues)
        children.append(first.report)
        second = inspect_context(
            owned_after,
            processing_mode=processing_mode,
            base=after_base,
            inherited_context=inherited_after,
            scopes=selected_scopes,
            document_loader=after_loader,
            budget=operation,
        )
        retain_report(second.report, operation, compare.issues)
        children.append(second.report)
        if first.view is not None and second.view is not None:
            compare.view(first.view, second.view, "root", compare_defaults)
            for index in range(len(selected_scopes)):
                operation.consume("work")
                a, b = first.scopes[index][1], second.scopes[index][1]
                if a is not None and b is not None:
                    compare.view(a, b, f"scopes/{index}", compare_defaults)
        if not compare.checked:
            compare.issues.append(
                Issue(
                    "context.definition_unverified",
                    "No effective definition/default comparison was established",
                    kind="unverified",
                )
            )
        data_json = encode_context_json(
            {"changes": [context_data(item, operation) for item in compare.changes]},
            operation,
        )
        complete = (
            first.view is not None
            and second.view is not None
            and first.report.complete
            and second.report.complete
            and not compare.incomplete
            and compare.checked > 0
        )
    except (LimitExceeded, MemoryError, RecursionError):
        compare.issues.exhausted("Context definition comparison exceeded its limits")
    except InputError:
        try:
            compare.issues.append(
                Issue(
                    "context.input_error",
                    "Invalid context comparison selection",
                    kind="input_error",
                )
            )
        except LimitExceeded:
            compare.issues.exhausted(
                "Context comparison diagnostics exceeded their limit"
            )
    # A failed selection still returns required, explicitly unavailable inspections.
    if first is None:
        unavailable = Report("context.definitions", 0, requested=1, complete=False)
        first = ContextInspection(None, (), unavailable, '{"view":null,"scopes":[]}')
    if second is None:
        unavailable = Report("context.definitions", 0, requested=1, complete=False)
        second = ContextInspection(None, (), unavailable, '{"view":null,"scopes":[]}')
    return ContextComparison(
        first,
        second,
        tuple(compare.changes),
        Report(
            "context.definition_comparison",
            compare.checked,
            tuple(compare.issues),
            requested=max(1, compare.checked + int(not complete)),
            complete=complete,
            children=tuple(children),
        ),
        data_json,
    )
