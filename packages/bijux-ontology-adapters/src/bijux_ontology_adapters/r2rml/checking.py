"""Coordinate qualified source, native and output mapping stages."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

from bijux_ontology.data.foreign_references.types import (
    ForeignReferenceResult,
)
from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
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
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.sources.types import RdfSource

from .materialization import materialize, read_output
from .output import check_models, check_obligations
from .plan import _capture_populations, qualify_plan
from .sources import qualify_source
from .types import (
    MorphBackend,
    R2rmlCheck,
    R2rmlForeignCheck,
    R2rmlPopulation,
    R2rmlTable,
    SqliteSnapshot,
)


def check_r2rml(
    backend: MorphBackend,
    snapshot: SqliteSnapshot,
    mapping: PreparedSource,
    *,
    populations: Sequence[R2rmlPopulation],
    models: Sequence[ModelDeclaration],
    foreign_checks: Sequence[R2rmlForeignCheck] = (),
    graph_name: GraphSelection = "@default",
    mapping_graph: GraphSelection | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> R2rmlCheck:
    """Materialize the real engine and verify complete explicit row/output contracts.

    The finite profile uses one local immutable SQLite snapshot, ordinary table
    mappings, one-column IRI templates, literal column objects, integer/string
    types and constant physical graph selection. Source keys and expected
    subjects remain independent obligations. Every parent join must be checked
    against an explicit independent corpus using its actual emitted link.
    No URI template, mapping engine or SQL view/function is reimplemented.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children: list[Report] = []
    tables: list[R2rmlTable] = []
    foreign_results: list[ForeignReferenceResult] = []
    output = None
    nquads = None
    model_report = None
    native_done = finished = False
    owned_snapshot = None
    owned_mapping = None
    try:
        operation.consume("states", 16)
        operation.consume("bytes", 2048)
        selected = _capture_populations(populations, operation)
        declarations = capture_items(models, ModelDeclaration, operation)
        requests = capture_items(foreign_checks, R2rmlForeignCheck, operation)
        if type(backend) is not MorphBackend or backend.version != "2.10.0":
            raise InputError("Supply an actually prepared MorphBackend")
        if type(snapshot) is not SqliteSnapshot or type(snapshot.content) is not bytes:
            raise InputError("Supply an actually prepared SQLite snapshot")
        if len(snapshot.content) > operation.limits.max_bytes:
            raise LimitExceeded(
                "SQLite snapshot exceeds this operation's byte allowance"
            )
        operation.consume("work", len(snapshot.content) + 1)
        operation.consume("bytes", len(snapshot.content))
        for text in (snapshot.sha256, snapshot.source_sha256, snapshot.source_name):
            bound_text(text, operation)
        if hashlib.sha256(snapshot.content).hexdigest() != snapshot.sha256:
            raise InputError("SQLite bytes differ from the owned snapshot hash")
        owned_snapshot = snapshot
        if not isinstance(mapping, PreparedSource):
            raise InputError("Mapping input requires an owned PreparedSource")
        if mapping.union:
            raise ResourceError("Mapping input requires one physical RDF graph")
        owned_mapping = mapping
        plan = qualify_plan(
            backend,
            snapshot,
            mapping,
            selected,
            declarations,
            requests,
            graph_name,
            mapping_graph,
            operation,
        )
        backend.artifacts.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="r2rml-", dir=backend.artifacts) as job:
            directory = Path(job)
            qualify_source(plan, directory, operation, issues, tables, children)
            cap = materialize(plan, directory, operation, issues, children)
            native_done = True
            nquads = read_output(directory, cap)
            output = prepare_rdf(
                RdfSource(
                    nquads,
                    format="nquads",
                    document_iri="urn:bijux:ontology:r2rml:output",
                ),
                budget=operation,
            )
            model_report = check_models(plan, output, operation)
            children.append(model_report)
            retain_report(model_report, operation, issues)
            check_obligations(
                plan, output, operation, issues, children, foreign_results
            )
            finished = True
    except LimitExceeded as error:
        for report in children:
            issues.retain_completed(report)
        issues.exhausted(str(error), location="r2rml")
    except (InputError, OSError) as error:
        try:
            unknown = isinstance(error, ResourceError | OSError)
            issues.append(
                Issue(
                    "r2rml.unverified" if unknown else "r2rml.input_error",
                    str(error),
                    kind="unverified" if unknown else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted), location="r2rml")
    pending = tuple(issue for issue in issues if issue.kind != "finding")
    native_report = Report(
        "r2rml.native",
        int(native_done),
        () if native_done else pending,
        requested=1,
        complete=native_done,
        regime="morph-kgc-2.10.0-isolated-cli",
    )
    children.append(native_report)
    compared = finished and all(child.complete for child in children)
    report = Report(
        "r2rml.mapping_output",
        int(compared),
        tuple(issues),
        requested=1,
        complete=compared,
        children=tuple(children),
        regime="explicit-source-rows-and-actual-materialized-rdf",
    )
    return R2rmlCheck(
        owned_snapshot,
        owned_mapping,
        output,
        nquads,
        tuple(tables),
        tuple(foreign_results),
        model_report,
        native_report,
        report,
    )
