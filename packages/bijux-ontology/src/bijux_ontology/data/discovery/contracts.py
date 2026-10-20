"""Observed proposals, actual corpus checks and explicit application acceptance."""

import time
from collections.abc import Sequence
from threading import Event
from typing import Literal

from rdflib import BNode, Graph, URIRef
from rdflib.namespace import RDF

from bijux_ontology.data.projection.reading import project_records
from bijux_ontology.data.projection.types import ReadBinding, ReadField
from bijux_ontology.declarations.path_fields import PathFieldDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    retain_report,
)
from bijux_ontology.models.path_fields.checking import check_path_fields
from bijux_ontology.models.path_fields.types import PathFieldUsage
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.shacl.authoring.compiling import compile_shapes
from bijux_ontology.shacl.authoring.types import (
    ShaclConstraint,
    ShaclCount,
    ShaclLogical,
    ShaclNodeShape,
    ShaclPropertyShape,
    ShaclShapeRef,
    ShaclValue,
)
from bijux_ontology.shacl.checking import validate_shapes
from bijux_ontology.shacl.coverage import ShaclCoverageContract

from .observation import capture, observe, reserve
from .types import (
    AcceptedContract,
    ContractCandidate,
    DiscoveryField,
    ObservedField,
    RecordObservation,
)

_EXCLUSIONS = (
    "universal-population-inference",
    "source-key-requiredness",
    "closed-ontology-vocabulary",
    "future-native-shacl-equivalence",
)


def _shapes(
    fields: tuple[ObservedField, ...], shape: URIRef, budget: Budget
) -> PreparedSource:
    """Compile supported typed Core rules without inferring ontology axioms.

    These ordinary SHACL rules are checked on the exact observed corpus. Native
    lexical validation remains independent; no claim equates both engines on all
    future literal inputs. Resource types require authored rdf:type values rather
    than SHACL class entailment.
    """
    definitions: list[ShaclNodeShape | ShaclPropertyShape] = []
    properties: list[ShaclShapeRef] = []

    def identifier() -> BNode:
        reserve(budget)
        return BNode()

    def retain(value: ShaclNodeShape | ShaclPropertyShape) -> ShaclShapeRef:
        reserve(budget)
        definitions.append(value)
        return ShaclShapeRef(value.identifier)

    for field in fields:
        budget.consume("work")
        if field.declaration is None:
            raise InputError("Missing observations cannot generate a value constraint")
        alternatives: list[ShaclShapeRef] = []
        for branch in field.alternatives:
            reserve(budget, len(branch.types) + 2)
            types = tuple(ShaclValue(has_value=URIRef(iri)) for iri in branch.types)
            type_refs = (
                (retain(ShaclPropertyShape(identifier(), RDF.type, types)),)
                if types
                else ()
            )
            kind: Literal["literal", "iri", "blank_or_iri"] = (
                "literal"
                if branch.kind == "literal"
                else "iri"
                if branch.kind == "iri"
                else "blank_or_iri"
            )
            alternatives.append(
                retain(
                    ShaclNodeShape(
                        identifier(),
                        type_refs,
                        (ShaclValue(datatype=branch.datatype, node_kind=kind),),
                    )
                )
            )
        constraints: list[ShaclConstraint] = [ShaclCount(field.minimum, field.maximum)]
        constraints.append(
            alternatives[0]
            if len(alternatives) == 1
            else ShaclLogical("or", tuple(alternatives))
        )
        properties.append(
            retain(
                ShaclPropertyShape(identifier(), field.field.path, tuple(constraints))
            )
        )
    reserve(budget, len(properties) + 1)
    definitions.append(ShaclNodeShape(shape, tuple(properties)))
    return compile_shapes(tuple(definitions), budget=budget)


def _evaluate(
    source: PreparedSource,
    graph: URIRef | BNode | None,
    records: tuple[URIRef | BNode, ...],
    declarations: tuple[PathFieldDeclaration, ...],
    shape: URIRef,
    shapes: PreparedSource,
    operation: Operation,
    issues: IssueCollector,
    children: list[Report],
) -> tuple[Report, Report]:
    """Require all native fields and all actual selected SHACL root evaluations."""
    reserve(operation, len(records) * len(declarations) + len(records) + 2)
    usages = tuple(
        PathFieldUsage(field, record) for record in records for field in declarations
    )
    native = check_path_fields(source, usages, graph_name=graph, budget=operation)
    children.append(native)
    retain_report(native, operation, issues)
    remaining = operation.deadline - time.monotonic()
    if remaining <= 0:
        raise LimitExceeded("Contract checking exceeded its deadline")
    shacl = validate_shapes(
        source,
        shapes,
        graph_name=graph,
        mode="pairs",
        focus_nodes=records,
        use_shapes=(shape,),
        preflight=True,
        coverage=ShaclCoverageContract(minimum_focus=len(records)),
        budget=operation,
        timeout=remaining,
    )
    children.append(shacl.report)
    retain_report(shacl.report, operation, issues)
    return native, shacl.report


def discover_contracts(
    source: PreparedSource | Graph,
    records: Sequence[URIRef | BNode],
    fields: Sequence[DiscoveryField],
    *,
    shape: URIRef,
    graph_name: GraphSelection | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> ContractCandidate:
    """Propose the full observed envelope; every selected exception stays required.

    Missing values count as zero. Every observed datatype and asserted resource
    type combination contributes an alternative, regardless of frequency. The
    proposal has no authority until explicitly accepted. An entirely missing
    field, unsupported path/literal, unrecognized record or incomplete population
    cannot establish complete candidate coverage. Shapes and native contracts
    have separate actual corpus reports. Core discovery does not install SHACL.
    Cancellation is cooperative during host work; the maintained SHACL backend
    has the remaining owned deadline.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children: list[Report] = []
    prepared = None
    selected_graph = None
    observations: tuple[RecordObservation, ...] = ()
    progress: list[RecordObservation] = []
    summaries: tuple[ObservedField, ...] = ()
    shapes = None
    native = None
    shacl = None
    requested = 1
    complete = False
    try:
        if not isinstance(shape, URIRef):
            raise InputError("Candidate shapes require an explicit named IRI")
        bound_node(shape, operation)
        selected, captured_fields = capture(records, fields, operation)
        requested = len(selected)
        prepared, selected_graph, observations, summaries = observe(
            source,
            selected,
            captured_fields,
            graph_name,
            operation,
            issues,
            children,
            progress,
        )
        qualified = len(observations) == requested and all(
            cell.qualified for record in observations for cell in record.fields
        )
        if (
            prepared is not None
            and summaries
            and all(item.declaration is not None for item in summaries)
        ):
            shapes = _shapes(summaries, shape, operation)
            declarations = tuple(
                item.declaration for item in summaries if item.declaration is not None
            )
            native, shacl = _evaluate(
                prepared,
                selected_graph,
                selected,
                declarations,
                shape,
                shapes,
                operation,
                issues,
                children,
            )
        complete = (
            qualified
            and native is not None
            and shacl is not None
            and all(child.complete for child in children)
        )
        operation.consume("work", 0)
    except LimitExceeded as error:
        for child in children:
            issues.retain_completed(child)
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(Issue("discovery.input", str(error), kind="input_error"))
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    # Completed record references were prepaid before observation. Publishing
    # that immutable prefix requires no new allowance after a bounded stop.
    observations = tuple(progress)
    if prepared is None and observations:
        prepared = observations[0].reference.source
        selected_graph = observations[0].reference.graph
    report = Report(
        "data.contract_discovery",
        len(observations),
        tuple(issues),
        requested=requested,
        complete=complete,
        regime="finite-observed-output-candidates",
        children=tuple(children),
        exclusions=_EXCLUSIONS,
    )
    return ContractCandidate._owned(
        prepared,
        selected_graph,
        observations,
        summaries,
        shape,
        shapes,
        native,
        shacl,
        report,
    )


def accept_candidate(
    candidate: ContractCandidate, *, accept_observed_bounds: bool
) -> AcceptedContract:
    """Record the consumer's decision without changing any observed truth status."""
    if type(accept_observed_bounds) is not bool or not accept_observed_bounds:
        raise InputError(
            "Acceptance requires an explicit observed-envelope policy decision"
        )
    if (
        type(candidate) is not ContractCandidate
        or not candidate.report.verified
        or candidate.shapes is None
    ):
        raise InputError("Only complete qualified candidate coverage can be accepted")
    return AcceptedContract._owned(candidate)


def check_contract(
    contract: AcceptedContract,
    source: PreparedSource | Graph,
    records: Sequence[URIRef | BNode],
    *,
    graph_name: GraphSelection | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> Report:
    """Apply explicitly accepted native and SHACL rules to every supplied record.

    This does not rediscover or expand alternatives when a held-out value changes.
    Absence outside the selected finite graph is never interpreted as OWL truth.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children: list[Report] = []
    checked = 0
    requested = 1
    complete = False
    try:
        if type(contract) is not AcceptedContract:
            raise InputError("Required checking needs an explicitly AcceptedContract")
        candidate = contract.candidate
        selected, fields = capture(
            records, tuple(item.field for item in candidate.fields), operation
        )
        requested = len(selected)
        reserve(operation, len(selected) * (len(fields) + 1))
        reads = tuple(
            ReadField(field.name, field.path, mode="many", minimum=0)
            for field in fields
        )
        bindings = tuple(
            ReadBinding(str(i), subject, reads) for i, subject in enumerate(selected)
        )
        projection = project_records(
            source, bindings, graph_name=graph_name, budget=operation
        )
        children.append(projection.report)
        retain_report(projection.report, operation, issues)
        checked = len(projection.records)
        if projection.report.verified and projection.records:
            reference = projection.records[0].reference
            declarations = tuple(
                item.declaration
                for item in candidate.fields
                if item.declaration is not None
            )
            if candidate.shapes is None or len(declarations) != len(candidate.fields):
                raise InputError("Accepted candidate lacks its qualified artifacts")
            _evaluate(
                reference.source,
                reference.graph,
                selected,
                declarations,
                candidate.shape,
                candidate.shapes,
                operation,
                issues,
                children,
            )
            complete = checked == requested
        operation.consume("work", 0)
    except LimitExceeded as error:
        for child in children:
            issues.retain_completed(child)
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(Issue("discovery.input", str(error), kind="input_error"))
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return Report(
        "data.accepted_contract",
        checked,
        tuple(issues),
        requested=requested,
        complete=complete,
        regime="consumer-accepted-observed-envelope",
        children=tuple(children),
        exclusions=_EXCLUSIONS,
    )
