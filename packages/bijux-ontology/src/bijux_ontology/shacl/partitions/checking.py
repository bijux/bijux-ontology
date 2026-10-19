"""Actual scoped SHACL obligations over primary records and supporting assertions."""

import threading
from collections.abc import Sequence

from rdflib import BNode, URIRef
from rdflib.namespace import SH

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
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, term_key
from bijux_ontology.shacl.analysis.checking import analyze_shape_profile
from bijux_ontology.shacl.coverage import ShaclCoverageContract
from bijux_ontology.shacl.execution.results import ShaclResult
from bijux_ontology.shacl.paths import decode_path
from bijux_ontology.shacl.profiles.execution import remaining_deadline
from bijux_ontology.shacl.profiles.extraction import extract_profile
from bijux_ontology.shacl.profiles.types import ShapeDependency, ShapeProfile
from bijux_ontology.shacl.session import ShaclSession

from .preparation import PartitionUnavailable, prepare_inputs
from .types import (
    PartitionObligation,
    PartitionPlan,
    PartitionResult,
    PartitionScope,
    PartitionValidation,
    capture,
)


def _unknown(issues: IssueCollector, message: str, name: str = "") -> None:
    """Retain required scope or a bounded final exhaustion marker."""
    try:
        issues.append(
            Issue("shacl.partition_scope_unverified", message, name, kind="unverified")
        )
    except LimitExceeded as error:
        issues.exhausted(str(error), location=name)


def _run(
    shapes: PreparedSource,
    roots: Sequence[str],
    data: PreparedSource,
    focus: tuple[URIRef | BNode, ...],
    operation: Operation,
    *,
    targets: bool = False,
) -> ShaclResult:
    """Use ordinary maintained execution and its actual selected-target coverage."""
    with ShaclSession(
        shapes,
        use_shapes=roots,
        mode="targets" if targets else "pairs",
        preflight=True,
        coverage=ShaclCoverageContract(),
        limits=operation.limits,
        timeout=remaining_deadline(operation),
    ) as session:
        result = session._validate(
            data,
            focus_nodes=() if targets else focus,
            graph_name=None,
            union=False,
            source_usages=(),
            scope="dataset",
            cancel=operation.cancel,
            budget=operation,
        )

    retained = IssueCollector(operation)
    try:
        retain_report(result.report, operation, retained)
    except LimitExceeded as error:
        retained.exhausted(str(error), location="shacl.partitions")
        result.report = Report(
            "shacl",
            result.report.checked,
            tuple(retained),
            requested=result.report.requested,
            complete=False,
            regime=result.report.regime,
        )
        result.conformance_report = result.report
        count = sum(issue.code == "shacl.nonconformance" for issue in retained)
        result.findings = result.findings[:count]
        result.root_findings = ()
        result.findings_complete = False
        result.coverage = None
    return result


def _full_inventory(
    result: ShaclResult,
    focus: tuple[URIRef | BNode, ...],
    roots: tuple[str, ...],
    operation: Operation,
    issues: IssueCollector,
) -> dict[str, tuple[URIRef, ...]] | None:
    """Require exact named record inventory from a completed full-primary run."""
    if result.coverage is None:
        _unknown(issues, "Primary target discovery has no completed coverage")
        return None
    retain_report(result.coverage.report, operation, issues)
    if not result.coverage.report.verified or any(
        isinstance(node, BNode) for node in focus
    ):
        _unknown(issues, "Global target identity or coverage is not established")
        return None
    expected = set()
    for node in focus:
        bound_node(node, operation)
        operation.consume("states")
        expected.add(term_key(node))
    actual = set()
    operation.consume("states", len(roots))
    grouped: dict[str, list[URIRef]] = {root: [] for root in roots}
    for target in result.coverage.targets:
        operation.consume("work")
        bound_node(target.shape, operation)
        bound_node(target.focus_node, operation)
        if (
            not isinstance(target.shape, URIRef)
            or not isinstance(target.focus_node, URIRef)
            or str(target.shape) not in grouped
        ):
            _unknown(
                issues,
                "Global target correspondence requires named roots and records",
            )
            return None
        operation.consume("states")
        actual.add(term_key(target.focus_node))
        grouped[str(target.shape)].append(target.focus_node)
    if actual != expected or any(not nodes for nodes in grouped.values()):
        _unknown(issues, "Required primary inventory differs from actual targets")
        return None
    return {root: tuple(nodes) for root, nodes in grouped.items()}


def _requires_full_primary(profile: ShapeProfile, budget: Budget) -> bool:
    """Qualify compound paths and nonpositive cross-record requirements."""
    if profile.source is None:
        raise PartitionUnavailable("Selected shapes have no established scope")
    graph = profile.source._read(budget=budget)
    for parameter in (SH.qualifiedMaxCount, SH.qualifiedValueShapesDisjoint):
        budget.consume("work")
        if (None, parameter, None) in graph:
            return True
    path_shapes = set()
    for shape, _, node in graph.triples((None, SH.path, None)):
        budget.consume("work")
        path = decode_path(graph, node, budget=budget, raise_limits=True)
        if path.kind == "unsupported":
            raise PartitionUnavailable("A property path has unsupported scope")
        if path.kind != "predicate":
            return True
        budget.consume("states")
        path_shapes.add(shape)
    outgoing: dict[URIRef | BNode, list[ShapeDependency]] = {}
    for edge in profile.dependencies:
        budget.consume("work")
        budget.consume("states")
        outgoing.setdefault(edge.shape, []).append(edge)
    positive = frozenset(
        (
            SH.path,
            SH.node,
            SH.property,
            SH["and"],
            SH["or"],
            SH.qualifiedValueShape,
            SH.minCount,
            SH.qualifiedMinCount,
            SH.hasValue,
            SH.name,
            SH.description,
            SH.message,
            SH.severity,
            SH.order,
            SH.deactivated,
        )
    )
    budget.consume("states", len(profile.roots))
    pending = [(root, 0) for root in profile.roots]
    seen = set()
    while pending:
        budget.consume("work")
        shape, reference_depth = pending.pop()
        key = (shape, reference_depth)
        if key in seen:
            continue
        budget.consume("states")
        seen.add(key)
        # Downstream constraints can depend on missing assertions about another
        # record. Only explicitly positive requirements establish local scope;
        # the native processor still owns all actual constraint semantics.
        if reference_depth >= 2:
            return True
        if reference_depth:
            for predicate, _ in graph.predicate_objects(shape):
                budget.consume("work")
                if (
                    str(predicate).startswith(str(SH))
                    and predicate not in positive
                    and not (predicate == SH["class"] and shape not in path_shapes)
                ):
                    return True
        for edge in outgoing.get(shape, ()):
            budget.consume("work")
            budget.consume("states")
            pending.append(
                (edge.dependency, reference_depth + int(shape in path_shapes))
            )

    return False


def _obligation(
    source: PreparedSource,
    plan: PartitionPlan,
    declaration: PartitionObligation,
    primary: PreparedSource,
    effective: PreparedSource,
    focus: tuple[URIRef | BNode, ...],
    operation: Operation,
) -> PartitionResult:
    """Keep raw data validation separate from required effective-scope qualification."""
    issues = IssueCollector(operation)
    children: list[Report] = []
    raw: list[ShaclResult] = []
    discovery = None
    name = ""
    effective_required = declaration.requires_complete_primary
    try:
        bound_text(declaration.name, operation)
        name = declaration.name
        roots = []
        for root in capture(declaration.use_shapes, operation):
            if not isinstance(root, str) or isinstance(root, BNode):
                raise InputError("Obligation roots require named authored shapes")
            bound_text(root, operation)
            roots.append(str(root))
        if not roots or len(set(roots)) != len(roots):
            raise PartitionUnavailable(
                "Obligations need distinct nonempty authored roots"
            )
        if (
            declaration.graph not in ("primary", "merged")
            or type(declaration.requires_complete_primary) is not bool
        ):
            raise InputError("Unknown effective graph obligation")
        profile = extract_profile(
            declaration.shapes,
            tuple(roots),
            budget=operation,
            timeout=remaining_deadline(operation),
            cancel=operation.cancel,
        )
        retain_report(profile.structural_report, operation, issues)
        children.append(profile.structural_report)
        if not profile.structural_report.verified or profile.source is None:
            raise PartitionUnavailable(
                "Only fully captured finite Core obligations are supported"
            )
        effective_required = effective_required or _requires_full_primary(
            profile, operation
        )
        analysis = analyze_shape_profile(profile, budget=operation)
        retain_report(analysis.report, operation, issues)
        children.append(analysis.report)
        if any(component.recursive for component in analysis.components):
            raise PartitionUnavailable(
                "Recursive SHACL interpretation is outside this partition profile"
            )
        grouped = None
        if effective_required:
            discovery = _run(
                profile.source, tuple(roots), primary, (), operation, targets=True
            )
            if discovery.coverage is not None:
                children.append(discovery.coverage.report)
            grouped = _full_inventory(discovery, focus, tuple(roots), operation, issues)
            if not plan.primary_complete or source.graph_fidelity != "complete":
                _unknown(
                    issues,
                    "The selected primary graph is not qualified as complete",
                    name,
                )
            if declaration.graph == "primary":
                # This is both full target discovery and the requested primary
                # obligation, so its actual conformance is required unchanged.
                retain_report(discovery.report, operation, issues)
                children.append(discovery.report)
                raw.append(discovery)
            elif grouped is not None:
                for root, nodes in grouped.items():
                    result = _run(profile.source, (root,), effective, nodes, operation)
                    raw.append(result)
                    retain_report(result.report, operation, issues)
                    children.append(result.report)
        else:
            result = _run(profile.source, tuple(roots), effective, focus, operation)
            raw.append(result)
            retain_report(result.report, operation, issues)
            children.append(result.report)
        if not raw:
            _unknown(issues, "No actual effective-graph obligation completed", name)
    except LimitExceeded as error:
        for result in raw:
            issues.retain_completed(result.report)
        issues.exhausted(str(error), location=name)
    except (PartitionUnavailable, ResourceError):
        _unknown(
            issues,
            "Required partition execution or effective scope is unavailable",
            name,
        )
    except InputError:
        try:
            issues.append(
                Issue(
                    "shacl.partition_input_error",
                    "Partition obligation metadata is invalid",
                    name,
                    kind="input_error",
                )
            )
        except LimitExceeded as error:
            issues.exhausted(str(error), location=name)
    scope = PartitionScope(
        plan.primary,
        plan.support,
        declaration.graph,
        focus,
        effective_required,
        plan.primary_complete,
        source.document_iri,
        source.effective_url,
    )
    report = Report(
        "shacl.partition.obligation",
        len(raw),
        tuple(issues),
        requested=max(1, len(raw)),
        children=tuple(children),
        regime="explicit-effective-graph",
    )
    return PartitionResult(
        name, scope, primary, effective, tuple(raw), discovery, report
    )


def check_partitions(
    source: PreparedSource,
    plan: PartitionPlan,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30.0,
    cancel: threading.Event | None = None,
) -> PartitionValidation:
    """Check only primary record obligations against each explicitly declared graph.

    Supporting assertions resolve references only for merged obligations. Primary
    output constraints never see them. Full-primary obligations run unfiltered
    maintained target discovery over the complete physical primary graph, require
    exact named record inventory, and retain unknown for partial corpus intent or
    anonymous cross-worker target identity. No unseen external partition is
    inferred present. Local blank focus and cross-partition blank links use the
    existing owned-term transport. No OWL consistency or whole-universe claim is
    made. Complex paths, qualified upper counts and nonpositive referenced
    constraints conservatively require full-primary qualification regardless of
    the caller flag. Direct local fields and positive reference witnesses remain
    usable without asserting whole-corpus completeness.
    Raw discovery conformance is evidence when its only role is primary
    target enumeration for a merged obligation; its coverage remains required.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    results: list[PartitionResult] = []
    children: list[Report] = []
    requested = 1
    try:
        if not isinstance(plan, PartitionPlan):
            raise InputError("Expected a typed partition plan")
        count = len(plan.obligations)
        requested = max(1, count)
        selected = capture(plan.obligations, operation, count=count)
        inputs = prepare_inputs(source, plan, operation)
        names = set()
        for declaration in selected:
            if not isinstance(declaration, PartitionObligation):
                raise InputError("Expected typed partition obligations")
            bound_text(declaration.name, operation)
            if declaration.name in names:
                raise InputError("Partition obligation names must be distinct")
            operation.consume("states")
            names.add(declaration.name)
            operation.consume("states")
            result = _obligation(
                source,
                plan,
                declaration,
                inputs.primary,
                inputs.primary if declaration.graph == "primary" else inputs.merged,
                inputs.focus,
                operation,
            )
            results.append(result)
            retain_report(result.report, operation, issues)
            children.append(result.report)
        if not selected:
            _unknown(issues, "No required partition obligations were supplied")
    except LimitExceeded as error:
        for result in results:
            issues.retain_completed(result.report)
        issues.exhausted(str(error), location="shacl.partitions")
    except (PartitionUnavailable, ResourceError):
        _unknown(issues, "Primary record or partition scope is unavailable")
    except InputError:
        try:
            issues.append(
                Issue(
                    "shacl.partition_input_error",
                    "Partition input metadata is invalid",
                    kind="input_error",
                )
            )
        except LimitExceeded as error:
            issues.exhausted(str(error), location="shacl.partitions")
    return PartitionValidation(
        tuple(results),
        Report(
            "shacl.partitions",
            len(results),
            tuple(issues),
            requested=requested,
            children=tuple(children),
            regime="primary-record-effective-graphs",
        ),
    )
