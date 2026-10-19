"""Independent emitted model, row/key and foreign endpoint obligations."""

from __future__ import annotations

from bijux_ontology.data.foreign_references.checking import check_foreign_references
from bijux_ontology.data.foreign_references.preparation import prepare_foreign_targets
from bijux_ontology.data.foreign_references.types import (
    ForeignReferenceResult,
)
from bijux_ontology.data.keys.extraction import _KeyExtraction
from bijux_ontology.data.keys.types import KeyRecord
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    bound_text,
    retain_report,
)
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_node, term_key

from .plan import _MappingPlan
from .types import (
    R2rmlPopulation,
)


def _records(
    output: PreparedSource,
    population: R2rmlPopulation,
    graph_name: GraphSelection,
    operation: Budget,
) -> tuple[KeyRecord, ...]:
    """Bound source handles before constructors can parse their long identifiers."""
    operation.consume("states", len(population.rows))
    operation.consume("bytes", 64 + 128 * len(population.rows))
    result = []
    for index, row in enumerate(population.rows):
        operation.consume("work")
        bound_node(row.usage.subject, operation)
        identity = str(index)
        bound_text(identity, operation)
        result.append(
            KeyRecord(identity, output, row.usage.subject, graph_name=graph_name)
        )
    return tuple(result)


def _emitted_population(
    output: PreparedSource,
    population: R2rmlPopulation,
    graph_name: GraphSelection,
    operation: Budget,
    issues: IssueCollector,
) -> Report:
    """Use actual owned emitted paths to correlate every required source key."""
    start = len(issues)
    operation.consume("states", len(population.rows) + 1)
    operation.consume("bytes", 128 + 128 * len(population.rows))
    records = _records(output, population, graph_name, operation)
    extraction = _KeyExtraction(
        records, population.emitted_key, operation, issues, len(records)
    )
    seen = set()
    by_subject = {row.usage.subject: row for row in population.rows}
    for key in extraction:
        operation.consume("work")
        seen.add(key.record.subject)
        row = by_subject[key.record.subject]
        if len(key.values) != 1 or term_key(key.values[0]) != term_key(row.key):
            issues.append(
                Issue(
                    "r2rml.emitted_key_mismatch",
                    "The actual emitted subject does not retain "
                    "this source row's required key",
                    row.usage.location,
                )
            )
    if len(seen) != len(records):
        issues.append(
            Issue(
                "r2rml.emitted_population_unverified",
                "Not every actual source row has a qualified "
                "expected emitted subject/key",
                str(population.triples_map),
                kind="unverified",
            )
        )
    return Report(
        "r2rml.emitted_population",
        len(seen),
        tuple(issues[start:]),
        requested=len(records),
        complete=len(seen) == len(records),
        regime="source-row-and-emitted-subject-correspondence",
    )


def check_models(
    plan: _MappingPlan, output: PreparedSource, operation: Budget
) -> Report:
    """Check every original model usage against the exact selected output graph."""
    count = sum(len(population.rows) for population in plan.populations)
    operation.consume("states", count + 2)
    operation.consume("bytes", 256 + 16 * count)
    usages = tuple(
        row.usage for population in plan.populations for row in population.rows
    )
    return check_rdf_emission(
        output,
        plan.models,
        usages,
        graph_name=plan.output_graph,
        budget=operation,
    )


def check_obligations(
    plan: _MappingPlan,
    output: PreparedSource,
    operation: Budget,
    issues: IssueCollector,
    children: list[Report],
    foreign_results: list[ForeignReferenceResult],
) -> None:
    """Publish each paid key or join result without a later-stage success barrier."""
    for population in plan.populations:
        operation.consume("states")
        operation.consume("bytes", 128)
        checked = _emitted_population(
            output, population, plan.output_graph, operation, issues
        )
        children.append(checked)
    for request, population in plan.foreign:
        operation.consume("states", 4 + len(population.rows))
        operation.consume("bytes", 512 + 64 * len(population.rows))
        targets = prepare_foreign_targets(
            request.targets,
            request.contract,
            complete=request.target_complete,
            budget=operation,
        )
        children.append(targets.report)
        retain_report(targets.report, operation, issues)
        source_records = _records(output, population, plan.output_graph, operation)
        checked_foreign = check_foreign_references(
            source_records,
            targets,
            source_complete=True,
            models=plan.models,
            budget=operation,
        )
        foreign_results.append(checked_foreign)
        children.append(checked_foreign.report)
        retain_report(checked_foreign.report, operation, issues)
