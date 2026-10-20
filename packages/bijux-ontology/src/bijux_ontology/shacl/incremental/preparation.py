"""Fixed resource capture, finite locality qualification and actual full baseline."""

from collections.abc import Sequence
from threading import Event

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF, SH

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.patches.planning import capture_source
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.shacl.coverage import ShaclCoverageContract, ShaclTarget
from bijux_ontology.shacl.execution.results import ShaclResult
from bijux_ontology.shacl.execution.session import ShaclSession
from bijux_ontology.shacl.profiles.execution import portable_roles, remaining_deadline
from bijux_ontology.shacl.profiles.extraction import extract_profile
from bijux_ontology.shacl.profiles.resources import _owned

from .types import IncrementalPreparation, IncrementalState

_LOCAL = frozenset(
    (
        RDF.type,
        SH.property,
        SH.path,
        SH.targetClass,
        SH.targetNode,
        SH.targetSubjectsOf,
        SH.targetObjectsOf,
        SH.severity,
        SH.deactivated,
        SH.message,
        SH.name,
        SH.description,
        SH.order,
        SH.group,
        SH.defaultValue,
        SH.minCount,
        SH.maxCount,
        SH.datatype,
        SH.nodeKind,
        SH["class"],
        SH.pattern,
        SH.flags,
        SH.minLength,
        SH.maxLength,
        SH.languageIn,
        SH.uniqueLang,
        SH["in"],
        SH.hasValue,
        SH.minInclusive,
        SH.maxInclusive,
        SH.minExclusive,
        SH.maxExclusive,
        SH.closed,
        SH.ignoredProperties,
    )
)


def _unknown(issues: IssueCollector, message: str) -> None:
    """Keep required unknown scope even when its ordinary issue slot is spent."""
    try:
        issues.append(Issue("shacl.incremental_unverified", message, kind="unverified"))
    except LimitExceeded as error:
        issues.exhausted(str(error), location="shacl.incremental")


def _fixed(source: Graph | PreparedSource, operation: Operation) -> PreparedSource:
    """Capture fixed selected resource metadata and graph using existing ownership."""
    original = (
        source
        if isinstance(source, PreparedSource)
        else prepare_source(source, budget=operation)
    )
    graph = original._read(budget=operation)
    count = len(graph)
    operation.consume("states", 2 * count)
    operation.consume("bytes", 64 + 184 * count)
    triples = []
    for triple in graph:
        operation.consume("work")
        triples.append(triple)
    return _owned(original, graph, triples, operation)


def _locality(
    shapes: PreparedSource,
    ontology: PreparedSource | None,
    data: PreparedSource,
    roots: tuple[str, ...],
    operation: Operation,
) -> tuple[bool, frozenset[URIRef], bool]:
    """Qualify only locality, never reinterpret or execute constraint semantics."""
    profile = extract_profile(
        shapes,
        roots,
        ontology=ontology,
        budget=operation,
        timeout=remaining_deadline(operation),
        cancel=operation.cancel,
    )
    portable = portable_roles(profile, data, operation)
    if profile.source is None or not profile.structural_report.verified:
        return False, frozenset(), portable
    graph = profile.source._read(budget=operation)
    predicates = set()
    local = True
    root_nodes = {URIRef(root) for root in roots}
    operation.consume("states", len(roots))
    properties = set()
    for root in root_nodes:
        for child in graph.objects(root, SH.property):
            operation.consume("work")
            operation.consume("states")
            properties.add(child)
    for edge in profile.dependencies:
        operation.consume("work")
        if edge.parameter == SH.property and edge.shape not in root_nodes:
            local = False
        if edge.parameter in (
            SH.node,
            SH["and"],
            SH["or"],
            SH["not"],
            SH.xone,
            SH.qualifiedValueShape,
        ):
            local = False
    for subject, predicate, value in graph:
        operation.consume("work")
        for node in (subject, predicate, value):
            bound_node(node, operation)
        if str(predicate).startswith(str(SH)) and predicate not in _LOCAL:
            local = False
        if predicate == SH.path and (
            subject not in properties or not isinstance(value, URIRef)
        ):
            local = False
        if predicate in (SH.targetSubjectsOf, SH.targetObjectsOf):
            if not isinstance(value, URIRef):
                local = False
            else:
                operation.consume("states")
                predicates.add(value)
    return local, frozenset(predicates), portable


def _run(
    shapes: PreparedSource,
    ontology: PreparedSource | None,
    roots: tuple[str, ...],
    data: PreparedSource,
    operation: Operation,
    *,
    trusted_sparql: bool,
    focus: tuple[URIRef, ...] = (),
    pairs: bool = False,
) -> ShaclResult:
    """Use an ordinary session and qualify its report before adoption."""
    operation.consume("resources")
    with ShaclSession(
        shapes,
        ontology=ontology,
        use_shapes=roots,
        trusted_sparql=trusted_sparql,
        mode="pairs" if pairs else "targets",
        preflight=True,
        coverage=ShaclCoverageContract(minimum_focus=1),
        limits=operation.limits,
        timeout=remaining_deadline(operation),
    ) as session:
        result = session._validate(
            data,
            focus_nodes=focus,
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
        retained.exhausted(str(error), location="shacl.incremental")
        return ShaclResult(
            Report(
                "shacl",
                result.report.checked,
                tuple(retained),
                requested=result.report.requested,
                complete=False,
            )
        )
    return result


def _targets(
    result: ShaclResult,
    roots: tuple[str, ...],
    operation: Operation,
) -> tuple[ShaclTarget, ...] | None:
    """Require actual completed execution for each selected named root."""
    if not result.report.complete or result.conforms is None or result.coverage is None:
        return None
    if not result.coverage.report.verified or not result.findings_complete:
        return None
    allowed = frozenset(roots)
    required = set(roots)
    operation.consume("states", len(roots))
    targets = []
    seen = set()
    for target in result.coverage.targets:
        operation.consume("work")
        bound_node(target.shape, operation)
        bound_node(target.focus_node, operation)
        if not isinstance(target.shape, URIRef) or str(target.shape) not in allowed:
            return None
        operation.consume("states")
        required.discard(str(target.shape))
        key = (target.shape, target.focus_node)
        if key not in seen:
            seen.add(key)
            targets.append(target)
    return tuple(targets) if targets and not required else None


def prepare_incremental(
    data: PreparedSource,
    shapes: Graph | PreparedSource,
    *,
    use_shapes: Sequence[str],
    ontology: Graph | PreparedSource | None = None,
    trusted_sparql: bool = False,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> IncrementalPreparation:
    """Run one actual full baseline over a finite explicit selected shape scope.

    Plain graphs and physically pinned datasets are supported. Reuse requires
    a complete passing result, named actual root/focus coverage and qualified
    direct-property Core locality. Other features retain full revalidation.
    Existing SHACL preparation has per-session limits; patch/report ownership
    is cumulative and execution uses the remaining operation deadline.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    state = None
    raw = None
    try:
        if (
            not isinstance(data, PreparedSource)
            or data.union
            or (data.is_dataset and data.graph_name is None)
        ):
            raise InputError("Incremental checks need an owned graph or physical pin")
        if type(trusted_sparql) is not bool:
            raise InputError("trusted_sparql requires a boolean")
        roots = []
        for root in capture_items(use_shapes, str, operation):
            if not isinstance(root, str) or isinstance(root, (BNode, Literal)):
                raise InputError("Incremental roots require named shape IRIs")
            bound_text(root, operation)
            roots.append(str(absolute_iri(root)))
        if not roots or len(set(roots)) != len(roots):
            raise InputError("Incremental roots must be nonempty and distinct")
        selected = tuple(roots)
        for metadata in (data.document_iri, data.effective_url, data.graph_name):
            if metadata is not None:
                if not isinstance(metadata, str):
                    raise InputError("Prepared source metadata requires text")
                bound_text(metadata, operation)
        capture_source(data, "baseline:", operation)
        fixed = _fixed(shapes, operation)
        fixed_ontology = _fixed(ontology, operation) if ontology is not None else None
        local, target_predicates, portable = _locality(
            fixed, fixed_ontology, data, selected, operation
        )
        raw = _run(
            fixed,
            fixed_ontology,
            selected,
            data,
            operation,
            trusted_sparql=trusted_sparql,
        )
        retain_report(raw.report, operation, issues)
        targets = _targets(raw, selected, operation)
        if not portable:
            _unknown(
                issues,
                "Incremental profile excludes shared anonymous identities "
                "across resource roles",
            )
        elif targets is None:
            _unknown(issues, "Selected baseline coverage or completion is unavailable")
        else:
            state = IncrementalState._owned(
                source=data,
                shapes=fixed,
                ontology=fixed_ontology,
                roots=selected,
                targets=targets,
                target_predicates=target_predicates,
                local=local,
                trusted_sparql=trusted_sparql,
                limits=operation.limits,
                report=raw.report,
            )
    except LimitExceeded as error:
        if raw is not None:
            issues.retain_completed(raw.report)
        issues.exhausted(str(error), location="shacl.incremental")
    except (InputError, ResourceError, RuntimeError):
        _unknown(issues, "Incremental preparation or selected execution is unavailable")
    report = Report(
        "shacl.incremental.prepare",
        int(state is not None),
        tuple(issues),
        requested=1,
        complete=state is not None,
    )
    return IncrementalPreparation(state, raw, report)
