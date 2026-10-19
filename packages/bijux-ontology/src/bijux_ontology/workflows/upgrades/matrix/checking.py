"""Bound selected deployment combinations and retain every required raw check."""

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import cast

from rdflib import Graph

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary
from bijux_ontology.workflows.upgrades.applications.checking import check_application
from bijux_ontology.workflows.upgrades.applications.preparation import _preflight
from bijux_ontology.workflows.upgrades.applications.types import (
    ApplicationVersion,
    _mapping,
    _sequence,
)

from .types import (
    CellCompatibility,
    CompatibilityMatrix,
    ConsumerCompatibility,
    ConsumerVersion,
    MatrixCell,
    MatrixContext,
    _name,
)


def _consumer(value: ConsumerVersion, budget: Budget) -> None:
    """Qualify the exact pair before hashing or retaining it in this operation."""
    _name(value.consumer, budget)
    _name(value.version, budget)


def _cell_name(value: MatrixCell, budget: Budget) -> None:
    """Bound all axis labels before membership tests or public retention."""
    for name in (value.application, value.ontology, value.context):
        _name(name, budget)


def _applications(
    values: Mapping[str, Mapping[ConsumerVersion, ApplicationVersion]], budget: Budget
) -> Mapping[str, Mapping[ConsumerVersion, ApplicationVersion]]:
    """Capture explicit code-to-consumer evidence without guessing any binding."""
    captured = _mapping(values, object, budget)
    result = {}
    for application, item in captured.items():
        _name(application, budget)
        if not isinstance(item, Mapping):
            raise InputError(
                "Application versions require explicit consumer registries"
            )
        registry = cast(Mapping[ConsumerVersion, ApplicationVersion], item)
        count = len(registry)
        budget.consume("states", count)
        budget.consume("bytes", 64 + 64 * count)
        consumers: dict[ConsumerVersion, ApplicationVersion] = {}
        for consumer in registry:
            budget.consume("work")
            if not isinstance(consumer, ConsumerVersion):
                raise InputError("Application registry needs ConsumerVersion keys")
            _consumer(consumer, budget)
            version = registry[consumer]
            if not isinstance(version, ApplicationVersion):
                raise InputError("Consumer registry needs ApplicationVersion evidence")
            if consumer in consumers or len(consumers) >= count:
                raise InputError("Consumer registry changed during capture")
            consumers[consumer] = version
        if len(consumers) != count:
            raise InputError("Consumer registry changed during capture")
        result[application] = MappingProxyType(consumers)
    return MappingProxyType(result)


def _unknown(scope: str, message: str, budget: Budget) -> Report:
    """Retain required missing evidence without manufacturing a comparison."""
    issues = IssueCollector(budget)
    issues.append(Issue("upgrade.matrix_unverified", message, kind="unverified"))
    return Report(scope, issues=tuple(issues), requested=1, complete=False)


def _compose(
    version: ApplicationVersion, context: MatrixContext, budget: Budget
) -> ApplicationVersion:
    """Use ordinary version construction with one explicit context/resource axis."""
    _preflight(version, budget)
    if context._context_json is not None:
        bound_text(context._context_json, budget)
    for url, raw in context._resources_json.items():
        bound_text(url, budget)
        bound_text(raw, budget)
    return ApplicationVersion(
        terms=version.terms,
        models=version.models,
        fields=version.fields,
        role_uses=version.role_uses,
        cases=version.cases,
        outputs=version.outputs,
        context=context.context,
        resources=context.resources,
        base=version.base,
        rdf_direction=version.rdf_direction,
    )


def _check_cell(
    cell: MatrixCell,
    consumers: tuple[ConsumerVersion, ...],
    required: frozenset[ConsumerVersion],
    applications: Mapping[str, Mapping[ConsumerVersion, ApplicationVersion]],
    ontologies: Mapping[str, Vocabulary],
    contexts: Mapping[str, MatrixContext],
    budget: Budget,
) -> CellCompatibility:
    """Check every required pair, retaining earlier findings if a later tail stops."""
    budget.consume("states", len(consumers))
    budget.consume("bytes", 64 + 64 * len(consumers))
    outcomes = [ConsumerCompatibility(consumer) for consumer in consumers]
    reports: list[Report] = []
    issues = IssueCollector(budget)
    checked = 0
    try:
        registry = applications.get(cell.application)
        if registry is not None:
            for consumer in registry:
                budget.consume("work")
                if consumer not in required:
                    budget.consume("states")
                    budget.consume("bytes", 64)
                    outcomes.append(ConsumerCompatibility(consumer, required=False))
        vocabulary = ontologies.get(cell.ontology)
        context = contexts.get(cell.context)
        for index, consumer in enumerate(consumers):
            budget.consume("work")
            version = registry.get(consumer) if registry is not None else None
            actual = None
            if version is None or vocabulary is None or context is None:
                report = _unknown(
                    "compatibility.consumer",
                    "Required version input is missing",
                    budget,
                )
            elif not version.cases and not version.outputs:
                report = _unknown(
                    "compatibility.consumer",
                    "Required consumer has no representative actual corpus",
                    budget,
                )
            else:
                selected = _compose(version, context, budget)
                actual = check_application(vocabulary, selected, budget=budget)
                report = actual.report
            outcomes[index] = ConsumerCompatibility(consumer, report, actual)
            retain_report(report, budget, issues)
            reports.append(report)
            checked += 1
            if any(issue.kind == "exhausted" for issue in report.issues):
                raise LimitExceeded("Matrix stopped after exhausted application check")
    except LimitExceeded as error:
        issues.exhausted(str(error), location="compatibility.cell")
    return CellCompatibility(
        cell,
        True,
        tuple(outcomes),
        Report(
            "compatibility.cell",
            checked,
            tuple(issues),
            requested=max(1, len(consumers)),
            complete=bool(consumers),
            children=tuple(reports),
            regime="finite-application-context-ontology",
        ),
    )


def check_compatibility_matrix(
    applications: Mapping[str, Mapping[ConsumerVersion, ApplicationVersion]],
    ontologies: Mapping[str, Vocabulary | Graph],
    contexts: Mapping[str, MatrixContext],
    cells: Sequence[MatrixCell],
    *,
    required_consumers: Sequence[ConsumerVersion],
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> CompatibilityMatrix:
    """Check explicit deployment cells for every required consumer contract version.

    Each application registry supplies the actual code/output corpus under each
    consumer's contracts; neither consumer matching nor version compatibility is
    inferred. Context versions own their complete closed resource registry.
    Known unselected combinations remain visibly untested, without affecting the
    required selected assessment. No majority or version-string rule can hide a
    failed or unknown selected obligation. All explicit work shares one Budget;
    serializer callback lifetimes retain their ordinary caller-owned contract.
    """
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("Matrix limits require Limits")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Matrix budget requires Budget")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Matrix limits conflict with the shared operation budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    outcomes: list[CellCompatibility] = []
    reports: list[Report] = []
    required: tuple[ConsumerVersion, ...] = ()
    requested = 1
    requested_consumers = 0
    checked = 0
    try:
        selected = _sequence(cells, MatrixCell, operation)
        requested = max(1, len(selected))
        supplied_consumers = _sequence(required_consumers, ConsumerVersion, operation)
        requested_consumers = len(supplied_consumers)
        for consumer in supplied_consumers:
            _consumer(consumer, operation)
        required = supplied_consumers
        operation.consume("states", len(required))
        operation.consume("bytes", 64 + 32 * len(required))
        required_keys = frozenset(required)
        if len(required_keys) != len(required):
            raise InputError("Required consumer versions must be unique")
        for cell in selected:
            _cell_name(cell, operation)
        operation.consume("states", len(selected))
        operation.consume("bytes", 64 + 32 * len(selected))
        chosen = set(selected)
        if len(chosen) != len(selected):
            raise InputError("Selected matrix cells must be unique")
        apps = _applications(applications, operation)
        vocabularies = _mapping(ontologies, object, operation)
        for vocabulary in vocabularies.values():
            if not isinstance(vocabulary, Vocabulary | Graph):
                raise InputError("Ontology versions require Vocabulary or RDF Graph")
        context_versions = _mapping(contexts, MatrixContext, operation)
        for registry in (vocabularies, context_versions):
            for name in registry:
                _name(name, operation)
        total = len(apps) * len(vocabularies) * len(context_versions)
        operation.consume("states", total + len(selected))
        operation.consume("bytes", 128 * (total + len(selected)))
        positions = {}
        for application, ontology, context in (
            (a, o, c) for a in apps for o in vocabularies for c in context_versions
        ):
            operation.consume("work")
            for name in (application, ontology, context):
                bound_text(name, operation)
            cell = MatrixCell(application, ontology, context)
            positions[cell] = len(outcomes)
            outcomes.append(CellCompatibility(cell, cell in chosen))
        for cell in selected:
            if cell not in positions:
                positions[cell] = len(outcomes)
                outcomes.append(CellCompatibility(cell, True))
        # Own every selected ontology before the first caller callback can run.
        loaded = {}
        for cell in selected:
            operation.consume("work")
            if cell.ontology not in loaded and cell.ontology in vocabularies:
                vocabulary = vocabularies[cell.ontology]
                loaded[cell.ontology] = (
                    vocabulary
                    if isinstance(vocabulary, Vocabulary)
                    else load_vocabulary(cast(Graph, vocabulary), budget=operation)
                )
        if not selected or not required:
            report = _unknown(
                "compatibility.matrix",
                "Explicit nonempty cells and required consumer versions are missing",
                operation,
            )
            retain_report(report, operation, issues)
            reports.append(report)
        else:
            for cell in selected:
                operation.consume("work")
                result = _check_cell(
                    cell,
                    required,
                    required_keys,
                    apps,
                    loaded,
                    context_versions,
                    operation,
                )
                outcomes[positions[cell]] = result
                assert result.report is not None
                retain_report(result.report, operation, issues)
                reports.append(result.report)
                checked += 1
                if any(issue.kind == "exhausted" for issue in result.report.issues):
                    raise LimitExceeded("Matrix stopped after exhausted selected cell")
    except LimitExceeded as error:
        issues.exhausted(str(error), location="compatibility.matrix")
    except InputError as error:
        try:
            issues.append(Issue("input.invalid", str(error), kind="input_error"))
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted), location="compatibility.matrix")
    return CompatibilityMatrix(
        tuple(outcomes),
        required,
        Report(
            "compatibility.matrix",
            checked,
            tuple(issues),
            requested=requested,
            children=tuple(reports),
            regime="finite-application-context-ontology",
        ),
        requested_consumers=requested_consumers,
    )
