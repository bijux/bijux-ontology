"""Finite maintained DOSDP syntactic matches against an expected inventory."""

from collections.abc import Mapping
from threading import Event
from typing import Literal, cast

from rdflib import Dataset, Graph, URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.queries import QueryScope
from bijux_ontology.rdf.queries.capture import selected_graphs
from bijux_ontology.rdf.sources.metadata import SnapshotMetadata
from bijux_ontology.rdf.sources.snapshots import PreparedSource, copy_graph
from bijux_ontology.rdf.sources.types import Payload, RdfSource
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.rdf.workloads.qualification import _engine, _plan
from bijux_ontology.rdf.workloads.types import EngineConfig, WorkloadQuery
from bijux_ontology.vocabulary.sources.imports import required_imports
from bijux_ontology.vocabulary.sources.types import OwnedSource

from .execution import generate_query
from .types import (
    DosdpPattern,
    DosdpQueryBackend,
    PatternCoverage,
    PatternFiller,
    PatternInstance,
    PatternQuery,
)


def _copy(graph: Graph, operation: Operation) -> Graph:
    """Prepay existing exact graph copy and document blank standardization."""
    for triple in graph:
        operation.consume("work", 4)
        operation.consume("triples")
        operation.consume("states", 8)
        operation.consume("bytes", 512)
        for node in triple:
            bound_node(node, operation)
    for prefix, iri in graph.namespaces():
        operation.consume("states")
        bound_text(prefix, operation)
        bound_node(iri, operation)
        operation.consume("bytes", 128)
    return copy_graph(
        graph, max_triples=operation.limits.max_triples, standardize_apart=True
    )


def _closure(
    source: PreparedSource,
    scope: QueryScope,
    imports: Mapping[str, RdfSource | Payload] | None,
    operation: Operation,
) -> tuple[dict[str, object], tuple[SnapshotMetadata, ...]]:
    """Reuse offline import resolution and one explicit selected physical root."""
    if scope.kind == "dataset":
        raise ResourceError(
            "DOSDP queries require default, named or explicit union scope"
        )
    graphs = tuple(selected_graphs(source, scope, operation))
    if len(graphs) != 1:
        raise ResourceError(
            "DOSDP root scope does not identify one complete projection"
        )
    root = OwnedSource(source, graphs[0][1])
    supplied = None
    if imports is not None:
        if type(imports) is not dict:
            raise InputError("DOSDP import bindings require an ordinary finite dict")
        supplied = {}
        for iri, value in imports.items():
            operation.consume("states")
            bound_text(iri, operation)
            absolute_iri(iri)
            supplied[iri] = value
    selected = required_imports(
        source, root, supplied, budget=operation, preserve_literals=True
    )
    metadata = tuple(
        SnapshotMetadata.capture(item.prepared, operation) for item in selected
    )
    dataset = Dataset(default_union=False)
    for owned in (root, *selected):
        operation.consume("resources")
        copied = _copy(owned.selected, operation)
        for triple in copied:
            operation.consume("work")
            operation.consume("states")
            operation.consume("bytes", 64)
            dataset.default_graph.add(triple)
    combined = PreparedSource._from_owned(
        dataset,
        is_dataset=False,
        document_iri=source.document_iri,
        effective_url=source.effective_url,
        literal_fidelity=source.literal_fidelity,
        max_triples=operation.limits.max_triples,
    )
    return _plan(combined, QueryScope("default"), operation), metadata


def _key(
    instance: PatternInstance, variables: tuple[str, ...], operation: Operation
) -> tuple[str, ...]:
    """Qualify exact complete assignment keys before hash or expectation lookup."""
    if type(instance) is not PatternInstance or not isinstance(
        instance.defined_class, URIRef
    ):
        raise InputError(
            "Expected DOSDP instances require typed named class assignments"
        )
    bound_text(instance.pattern_iri, operation)
    absolute_iri(instance.pattern_iri)
    bound_node(instance.defined_class, operation)
    if type(instance.fillers) is not tuple:
        raise InputError("Pattern fillers require a finite tuple")
    values: dict[str, str] = {}
    for filler in instance.fillers:
        operation.consume("states")
        operation.consume("bytes", 64)
        if (
            type(filler) is not PatternFiller
            or type(filler.variable) is not str
            or not isinstance(filler.value, URIRef)
        ):
            raise InputError("Pattern fillers require exact variable names and URIRefs")
        bound_text(filler.variable, operation)
        bound_node(filler.value, operation)
        if filler.variable in values:
            raise InputError("Pattern assignments cannot repeat a variable")
        values[filler.variable] = str(filler.value)
    if set(values) != set(variables):
        raise InputError(
            "Expected assignment must bind every selected pattern variable exactly"
        )
    operation.consume("states", len(variables) + 2)
    operation.consume("bytes", 32 * (len(variables) + 2))
    return (
        instance.pattern_iri,
        str(instance.defined_class),
        *(values[name] for name in variables),
    )


def _matches(
    pattern: DosdpPattern, answer: object, operation: Operation
) -> tuple[PatternInstance, ...]:
    """Retain each actual distinct native assignment, without picking one filler."""
    from bijux_ontology.rdf.answers.types import AnswerArtifact

    if (
        not isinstance(answer, AnswerArtifact)
        or answer.kind != "SELECT"
        or answer.external_complete is not True
    ):
        raise ResourceError("DOSDP requires an actually completed SELECT answer")
    required = ("defined_class", *pattern.variables)
    if set(answer.variables) != {*required, *(name + "_label" for name in required)}:
        raise ResourceError(
            "Native query omits the complete expected variable inventory"
        )
    indices = tuple(answer.variables.index(name) for name in required)
    result: list[PatternInstance] = []
    seen: set[tuple[str, ...]] = set()
    for row in answer.rows:
        operation.consume("work", len(required))
        operation.consume("states", 2 * len(required) + 2)
        operation.consume("bytes", 128 * (len(required) + 1))
        values = tuple(row[index] for index in indices)
        if any(not isinstance(value, URIRef) for value in values):
            raise ResourceError(
                "DOSDP named filler profile returned an unbound or unnamed term"
            )
        instance = PatternInstance(
            pattern.iri,
            cast(URIRef, values[0]),
            tuple(
                PatternFiller(name, cast(URIRef, value))
                for name, value in zip(pattern.variables, values[1:], strict=True)
            ),
        )
        key = _key(instance, pattern.variables, operation)
        if key not in seen:
            seen.add(key)
            result.append(instance)
    return tuple(result)


def _assignment_text(key: tuple[str, ...], operation: Operation) -> str:
    """Bound combined diagnostic text before joining individually valid fields."""
    operation.consume("work", len(key))
    characters = sum(len(value) for value in key)
    operation.consume("work", characters)
    operation.consume("bytes", 4 * characters)
    size = sum(len(value.encode("utf-8")) for value in key) + 3 * max(0, len(key) - 1)
    if size > operation.limits.max_bytes:
        raise LimitExceeded("Pattern assignment diagnostic exceeds max_bytes")
    operation.consume("work", size)
    operation.consume("bytes", 4 * size)
    return " | ".join(key)


def check_patterns(
    backend: DosdpQueryBackend,
    source: PreparedSource,
    patterns: tuple[DosdpPattern, ...],
    expected: tuple[PatternInstance, ...],
    *,
    scope: QueryScope,
    imports: Mapping[str, RdfSource | Payload] | None = None,
    extras: Literal["allow", "forbid"] = "forbid",
    matching: Literal["syntactic", "inferred"] = "syntactic",
    max_rows: int = 1000,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 60,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> PatternCoverage:
    """Compare exact expected assignments in one explicitly selected local closure.

    Syntactic absence is an inventory mismatch under the selected policy. It
    does not establish logical nonconformance or the absence of an OWL model.
    Inferred matching and unsupported pattern semantics remain required unknown.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    queries: list[PatternQuery] = []
    metadata = None
    imported: tuple[SnapshotMetadata, ...] = ()
    children: list[Report] = []
    checked = 0
    requested = len(patterns) if type(patterns) is tuple else 1
    try:
        if (
            type(backend) is not DosdpQueryBackend
            or type(source) is not PreparedSource
            or not isinstance(scope, QueryScope)
        ):
            raise InputError(
                "DOSDP requires prepared backend/source and an explicit QueryScope"
            )
        if type(patterns) is not tuple or type(expected) is not tuple:
            raise InputError("Patterns and expected instances require finite tuples")
        if extras not in ("allow", "forbid") or matching not in (
            "syntactic",
            "inferred",
        ):
            raise InputError(
                "Choose explicit supported inventory and matching policies"
            )
        if type(max_rows) is not int or max_rows <= 0:
            raise InputError("DOSDP max_rows must be a positive integer")
        if not patterns:
            raise ResourceError(
                "No selected pattern establishes required pattern coverage"
            )
        if matching == "inferred":
            raise ResourceError("Reasoner-expanded DOSDP matching is not implemented")
        operation.consume("states", len(patterns) + len(expected))
        operation.consume("bytes", 128 * (len(patterns) + len(expected)))
        catalog: dict[str, DosdpPattern] = {}
        required: dict[str, dict[tuple[str, ...], PatternInstance]] = {}
        for pattern in patterns:
            if type(pattern) is not DosdpPattern:
                raise InputError("Patterns must be captured with prepare_dosdp_pattern")
            bound_text(pattern.iri, operation)
            if len(pattern.content) > operation.limits.max_bytes:
                raise LimitExceeded(
                    "Captured pattern exceeds this operation's max_bytes"
                )
            operation.consume("bytes", len(pattern.content))
            operation.consume("work", len(pattern.content))
            operation.consume(
                "states",
                len(pattern.prefixes)
                + len(pattern.variables)
                + len(pattern.unsupported),
            )
            for prefix, iri in pattern.prefixes:
                bound_text(prefix, operation)
                bound_text(iri, operation)
            for text in (*pattern.variables, *pattern.unsupported):
                bound_text(text, operation)
            if pattern.iri in catalog:
                raise InputError("Selected DOSDP pattern identities must be distinct")
            catalog[pattern.iri] = pattern
            required[pattern.iri] = {}
        for instance in expected:
            if type(instance) is not PatternInstance:
                raise InputError("Expected pattern instances require typed assignments")
            bound_text(instance.pattern_iri, operation)
            if instance.pattern_iri not in catalog:
                raise ResourceError("Expected instance has no selected local pattern")
            key = _key(instance, catalog[instance.pattern_iri].variables, operation)
            if key in required[instance.pattern_iri]:
                raise InputError("Expected pattern assignments must be distinct")
            required[instance.pattern_iri][key] = instance
        metadata = SnapshotMetadata.capture(source, operation)
        plan, imported = _closure(source, scope, imports, operation)
        for pattern in patterns:
            operation.consume("states", 16)
            operation.consume("bytes", 1024)
            local = IssueCollector(operation)
            query = None
            identity = None
            engine = None
            matches: tuple[PatternInstance, ...] = ()
            complete = False
            try:
                if pattern.unsupported:
                    raise ResourceError(
                        "Selected DOSDP fields or constraints are unsupported"
                    )
                query, identity = generate_query(backend, pattern, operation, heap_mb)
                engine = _engine(
                    WorkloadQuery(pattern.iri, query, "set"),
                    EngineConfig("rdflib"),
                    plan,
                    [],
                    scope,
                    max_rows,
                    operation,
                )
                retain_report(engine.report, operation, local)
                if engine.status != "completed" or engine.answer is None:
                    raise ResourceError(
                        "The selected native pattern query did not complete"
                    )
                matches = _matches(pattern, engine.answer, operation)
                actual = {
                    _key(match, pattern.variables, operation): match
                    for match in matches
                }
                for key, instance in required[pattern.iri].items():
                    operation.consume("work")
                    if key not in actual:
                        local.append(
                            Issue(
                                "pattern.expected_missing",
                                "Required syntactic pattern assignment was not matched",
                                str(instance.defined_class),
                                expected=_assignment_text(key, operation),
                                kind="finding",
                            )
                        )
                if extras == "forbid":
                    for key, instance in actual.items():
                        operation.consume("work")
                        if key not in required[pattern.iri]:
                            local.append(
                                Issue(
                                    "pattern.unexpected_binding",
                                    "Unexpected additional syntactic assignment",
                                    str(instance.defined_class),
                                    actual=_assignment_text(key, operation),
                                    kind="finding",
                                )
                            )
                complete = True
                checked += 1
            except LimitExceeded as error:
                local.exhausted(str(error), location=pattern.iri)
                issues.retain_completed(Report("patterns.query", issues=tuple(local)))
                stopped = Report(
                    "patterns.query",
                    issues=tuple(local),
                    requested=1,
                    complete=False,
                    children=() if engine is None else (engine.report,),
                    regime="syntactic-dosdp",
                )
                queries.append(
                    PatternQuery(
                        pattern,
                        query,
                        backend.version,
                        identity,
                        matches,
                        engine,
                        stopped,
                    )
                )
                children.append(stopped)
                raise
            except (InputError, OSError) as error:
                local.append(
                    Issue(
                        "pattern.unverified"
                        if isinstance(error, ResourceError | OSError)
                        else "input.invalid",
                        str(error),
                        pattern.iri,
                        kind="unverified"
                        if isinstance(error, ResourceError | OSError)
                        else "input_error",
                    )
                )
            report = Report(
                "patterns.query",
                int(complete),
                tuple(local),
                requested=1,
                complete=complete,
                children=() if engine is None else (engine.report,),
                regime="syntactic-dosdp",
            )
            retain_report(report, operation, issues)
            queries.append(
                PatternQuery(
                    pattern, query, backend.version, identity, matches, engine, report
                )
            )
            children.append(report)
        if metadata is not None and not metadata.same_scope(
            SnapshotMetadata.capture(source, operation)
        ):
            raise ResourceError("DOSDP source metadata changed during query execution")
    except LimitExceeded as error:
        for child in children:
            issues.retain_completed(child)
        issues.exhausted(str(error))
    except (InputError, OSError) as error:
        try:
            issues.append(
                Issue(
                    "pattern.unverified"
                    if isinstance(error, ResourceError | OSError)
                    else "input.invalid",
                    str(error),
                    kind="unverified"
                    if isinstance(error, ResourceError | OSError)
                    else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return PatternCoverage(
        scope,
        metadata,
        imported,
        matching,
        tuple(queries),
        Report(
            "patterns.coverage",
            checked,
            tuple(issues),
            requested=max(1, requested),
            complete=checked == requested and requested > 0,
            children=tuple(children),
            regime="syntactic-dosdp",
        ),
    )
