"""Actual-data routing through closed local plans and mandatory baselines."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from rdflib import BNode, Graph, URIRef
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.workflows.projects.contracts import ContractResult
from bijux_ontology.workflows.projects.profiles.loading import _items, _text
from bijux_ontology.workflows.projects.routing.qualification import (
    Unavailable,
    bindings,
    plans,
)
from bijux_ontology.workflows.projects.routing.types import (
    ProfilePlan,
    ProfileRouting,
    RecordRoute,
    RoutedPlan,
)


def _unknown(scope: str, message: str, budget: Budget) -> Report:
    """Retain a required obligation with bounded trusted explanation text."""
    issues = IssueCollector(budget)
    issues.append(Issue("profile.routing_unverified", message, kind="unverified"))
    return Report(scope, issues=tuple(issues), requested=1, complete=False)


def _destination(path: Path, budget: Budget) -> str:
    """Bound caller path components before constructing its combined text."""
    parts = path.parts
    budget.consume("states", len(parts))
    size = 0
    for part in parts:
        bound_text(part, budget)
        size += len(part.encode("utf-8")) + 1
        if size > budget.limits.max_bytes:
            raise LimitExceeded("Routing destination exceeds max_bytes")
    text = str(path)
    bound_text(text, budget)
    return text


def _records(
    values: Sequence[URIRef | BNode], count: int, budget: Budget
) -> tuple[URIRef | BNode, ...]:
    """Capture one finite indexed record inventory before graph lookups."""
    budget.consume("states", count)
    budget.consume("bytes", 64 + 16 * count)
    result = []
    for index in range(count):
        budget.consume("work")
        try:
            value = values[index]
        except IndexError as error:
            raise InputError("Routing records changed during capture") from error
        if not isinstance(value, URIRef | BNode):
            raise InputError("Routed records require named IRIs or owned blanks")
        bound_node(value, budget)
        result.append(value)
    if len(
        {id(value) if isinstance(value, BNode) else value for value in result}
    ) != len(result):
        raise InputError("Routing record selections must be distinct")
    return tuple(result)


def _present(record: URIRef | BNode, graph: Graph, budget: Budget) -> bool:
    """Require an actual resource occurrence, with object identity for blanks."""
    if isinstance(record, BNode):
        for triple in graph:
            budget.consume("work")
            for node in triple:
                bound_node(node, budget)
                if node is record:
                    return True
        return False
    budget.consume("work", 2)
    return (
        next(graph.triples((record, None, None)), None) is not None
        or next(graph.triples((None, None, record)), None) is not None
    )


def _adopt(result: ContractResult, budget: Budget) -> ContractResult:
    """Keep a completed diagnostic prefix if later raw-report adoption exhausts."""
    retained = IssueCollector(budget)
    raw: list[tuple[str, Report]] = []
    try:
        retain_report(result.report, budget, retained)
        budget.consume("states", len(result.raw_reports))
        for name, report in result.raw_reports:
            _text(name, budget)
            retain_report(report, budget, retained)
            raw.append((name, report))
    except LimitExceeded as error:
        retained.exhausted(str(error))
        partial = Report(
            "profile-routing-plan",
            len(raw),
            tuple(retained),
            requested=max(1, len(raw)),
            complete=False,
        )
        return ContractResult(partial, tuple(raw), result.policy)
    return result


def _execute(
    plan: ProfilePlan,
    baseline: bool,
    record: URIRef | BNode,
    source: PreparedSource,
    present: bool,
    destination: Path,
    budget: Budget,
) -> RoutedPlan:
    """Pass exact actual data into the ordinary profile/project/SHACL runner."""
    from bijux_ontology.workflows.projects.inputs import ShaclJobInput

    iri = ""
    result = None
    try:
        iri = _text(plan.profile.iri, budget, iri=True)
        selected = bindings(plan, budget)
        if not present:
            raise Unavailable(
                "The selected record has no established actual source occurrence"
            )
        if isinstance(record, BNode):
            raise Unavailable(
                "Owned blank focus lacks supported named-pair coverage evidence"
            )
        budget.consume("resources")
        inputs = {}
        for layer, jobs in selected.items():
            budget.consume("states", len(jobs))
            inputs[layer] = {
                name: ShaclJobInput(source, (record,), binding.shapes)
                for name, binding in jobs.items()
            }
        result = _adopt(plan.profile.check(destination, job_inputs=inputs), budget)
        report = result.report
    except LimitExceeded:
        raise
    except (InputError, OSError, UnicodeError) as error:
        message = (
            str(error)
            if isinstance(error, Unavailable)
            else "Selected profile execution failed"
        )
        unknown = _unknown("profile-routing-plan", message, budget)
        report = Report(
            "profile-routing-plan",
            requested=1,
            complete=False,
            children=(plan.profile.report, unknown),
        )
    return RoutedPlan(iri, baseline, result, report)


def _one(
    record: URIRef | BNode,
    index: int,
    source: PreparedSource,
    graph: Graph,
    predicates: tuple[URIRef, ...],
    registry: dict[str, ProfilePlan],
    baseline: tuple[ProfilePlan, ...],
    multiple: str,
    required: bool,
    destination: Path,
    budget: Budget,
) -> RecordRoute:
    """Always select baselines independently of actual declared applicability."""
    issues = IssueCollector(budget)
    declarations: list[Node] = []
    seen: set[Node] = set()
    routed: list[RoutedPlan] = []
    budget.consume("states", len(baseline))
    selected: list[tuple[ProfilePlan, bool]] = [(plan, True) for plan in baseline]
    started = 0
    extra: list[Report] = []
    applicable = False
    try:
        present = _present(record, graph, budget)
        if not present:
            issues.append(
                Issue(
                    "profile.record_unverified",
                    "Selected record is absent or its blank identity is not owned",
                    kind="unverified",
                )
            )
        else:
            for predicate in predicates:
                budget.consume("work")
                for value in graph.objects(record, predicate):
                    budget.consume("work")
                    bound_node(value, budget)
                    if value not in seen:
                        budget.consume("states")
                        seen.add(value)
                        declarations.append(value)
            if not declarations and required:
                issues.append(
                    Issue(
                        "profile.declaration_missing",
                        "Required profile declaration is absent",
                        kind="unverified",
                    )
                )
            if len(declarations) > 1 and multiple == "reject":
                issues.append(
                    Issue(
                        "profile.declaration_ambiguous",
                        "Multiple profile declarations require an explicit all policy",
                        kind="unverified",
                    )
                )
            else:
                for value in declarations:
                    budget.consume("work")
                    plan = (
                        registry.get(str(value)) if isinstance(value, URIRef) else None
                    )
                    if plan is None:
                        issues.append(
                            Issue(
                                "profile.declaration_unknown",
                                "Declared profile is not a named entry "
                                "in the closed local registry",
                                kind="unverified",
                            )
                        )
                    else:
                        budget.consume("states")
                        selected.append((plan, False))
        applicable = True
        if not baseline:
            extra.append(
                _unknown(
                    "profile-routing-baseline",
                    "A nonempty mandatory baseline is required",
                    budget,
                )
            )
        for plan_index, (plan, is_baseline) in enumerate(selected):
            budget.consume("work")
            # Generated run names contain no caller metadata and cannot turn
            # profile identifiers or blank labels into filesystem paths.
            prefix = str(destination)
            bound_text(prefix, budget)
            suffix = f"/record-{index}/plan-{plan_index}"
            if len(prefix) + len(suffix) > budget.limits.max_bytes:
                raise LimitExceeded("Routing destination exceeds max_bytes")
            bound_text(prefix + suffix, budget)
            run = destination / f"record-{index}" / f"plan-{plan_index}"
            routed.append(
                _execute(plan, is_baseline, record, source, present, run, budget)
            )
            started += 1
    except LimitExceeded as error:
        issues.exhausted(str(error))
    application = Report(
        "profile-applicability",
        int(applicable),
        tuple(issues),
        requested=1,
        complete=applicable,
    )
    pending = len(selected) - started
    if pending:
        extra.append(
            Report(
                "profile-routing-plans-remaining",
                issues=(
                    Issue(
                        "profile.routing_unverified",
                        "Required plans remain unstarted",
                        kind="unverified",
                    ),
                ),
                requested=pending,
                complete=False,
            )
        )
    report = Report(
        "profile-routing-record",
        len(routed),
        requested=max(1, len(selected)),
        complete=applicable and not pending,
        children=(application, *(item.report for item in routed), *extra),
    )
    return RecordRoute(record, tuple(declarations), application, tuple(routed), report)


def check_profile_routes(
    source: PreparedSource,
    records: Sequence[URIRef | BNode],
    *,
    predicates: Sequence[URIRef],
    registry: Mapping[str, ProfilePlan],
    baseline: Sequence[ProfilePlan],
    destination: Path,
    multiple: Literal["reject", "all"] = "reject",
    require_declaration: bool = True,
    input_report: Report | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ProfileRouting:
    """Apply mandatory baselines plus actual declarations from one selected graph.

    Only local composed plans dispatch; profile identifiers never cause network
    retrieval. Entire selected graphs, shared children and exact owned focus
    terms pass through the ordinary project runner. The finite executable subset
    supports explicitly bound SHACL and static vocabulary/model/ontology/SKOS
    jobs. Other selected data jobs remain required and unstarted.

    Routing capture, dispatch and report adoption share the supplied Budget.
    Ordinary profile jobs retain their own configured backend/stage limits; no
    global backend deadline or CPU counter is implied. JSON-LD callers supply
    the actual prepared RDF and its required conversion input_report.
    """
    if not isinstance(source, PreparedSource) or not isinstance(destination, Path):
        raise InputError("Routing requires actual prepared RDF and a destination Path")
    if multiple not in ("reject", "all") or type(require_declaration) is not bool:
        raise InputError("Routing requires explicit multiple and declaration policies")
    if (
        limits is not None
        and not isinstance(limits, Limits)
        or budget is not None
        and not isinstance(budget, Budget)
    ):
        raise InputError("Routing requires typed Limits and Budget")
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("Routing limits conflict with the shared Budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    routes: list[RecordRoute] = []
    selected: tuple[URIRef | BNode, ...] = ()
    children = []
    count = 0
    try:
        if not isinstance(records, Sequence) or isinstance(records, str | bytes):
            raise InputError("Routing records require a finite sequence")
        count = len(records)
        selected = _records(records, count, operation)
        if not selected:
            raise Unavailable("Required routing record selection is empty")
        _destination(destination, operation)
        if destination.exists():
            raise InputError("Routing destination must be new")
        chosen = _items(predicates, operation)
        if not chosen or any(not isinstance(value, URIRef) for value in chosen):
            raise InputError(
                "Declaration predicates require a nonempty named inventory"
            )
        for value in chosen:
            assert isinstance(value, URIRef)
            bound_node(value, operation)
        registered, mandatory = plans(registry, baseline, operation)
        if input_report is not None:
            if not isinstance(input_report, Report):
                raise InputError("Routing input evidence requires a Report")
            retain_report(input_report, operation, issues)
            children.append(input_report)
        graph = source._read(budget=operation)
        for index, record in enumerate(selected):
            operation.consume("work")
            route = _one(
                record,
                index,
                source,
                graph,
                tuple(value for value in chosen if isinstance(value, URIRef)),
                registered,
                mandatory,
                multiple,
                require_declaration,
                destination,
                operation,
            )
            routes.append(route)
            if route.report.outcome == "exhausted":
                break
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except (InputError, OSError, UnicodeError):
        try:
            issues.append(
                Issue(
                    "profile.routing_unverified",
                    "Routing input or closed plan inventory is unavailable",
                    kind="unverified",
                )
            )
        except LimitExceeded as error:
            issues.exhausted(str(error))
    pending = count - len(routes)
    if pending:
        children.append(
            Report(
                "profile-routing-records-remaining",
                issues=(
                    Issue(
                        "profile.routing_unverified",
                        "Required records remain unstarted",
                        kind="unverified",
                    ),
                ),
                requested=pending,
                complete=False,
            )
        )
    report = Report(
        "profile-routing",
        len(routes),
        tuple(issues),
        requested=max(1, count),
        complete=not pending,
        children=(*(route.report for route in routes), *children),
        regime="closed-local-actual-data-routing",
    )
    return ProfileRouting(tuple(routes), pending, report)
