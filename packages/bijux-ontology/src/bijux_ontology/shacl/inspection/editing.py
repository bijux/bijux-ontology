"""Replace explicit count bounds while preserving all other authored RDF."""

import threading
import time
from collections.abc import Sequence

from rdflib import BNode, URIRef
from rdflib.namespace import SH

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.processes.transport import checkpoint
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.reports import Issue, Report
from bijux_ontology.shacl.authoring.compiling import _Compiler
from bijux_ontology.shacl.authoring.types import ShaclCount, ShaclPropertyShape
from bijux_ontology.shacl.checking import ShaclResult
from bijux_ontology.shacl.coverage import ShaclCoverageContract
from bijux_ontology.shacl.profiles.comparison import _coverage
from bijux_ontology.shacl.profiles.resources import _capture, _owned
from bijux_ontology.shacl.session import ShaclSession

from .types import ShapeCountEdit, ShapeEdit, ShapeInspection


def edit_shape_counts(
    inspection: ShapeInspection,
    edits: Sequence[ShapeCountEdit],
    *,
    data: PreparedSource,
    allow_opaque: bool = False,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30.0,
    cancel: threading.Event | None = None,
) -> ShapeEdit:
    """Copy an authored graph and replace only selected ordinary count bounds.

    Every unrelated assertion, annotation, path, list and shape identity is kept.
    Explicit opaque edits retain unsupported assertions without claiming complete
    typed semantics. Maintained meta-SHACL and actual selected data validation
    remain separate evidence. Backend execution uses its existing per-call limits
    and the remaining wall-clock deadline; host edits share a cumulative budget.
    """
    if (
        not isinstance(inspection, ShapeInspection)
        or not isinstance(data, PreparedSource)
        or type(allow_opaque) is not bool
    ):
        raise InputError(
            "Shape editing requires typed inspection, data and opaque flag"
        )
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    source = None
    validation: ShaclResult | None = None
    checked = 0
    children: list[Report] = []
    try:
        retain_report(inspection.report, operation, issues)
        children.append(inspection.report)
        if not inspection.report.verified and not allow_opaque:
            issues.append(
                Issue(
                    "shacl.shape_edit_unverified",
                    "An incomplete typed projection requires "
                    "explicit opaque preservation",
                    kind="unverified",
                )
            )
        else:
            selected = _capture(edits, operation)
            if not selected:
                raise InputError("Shape edits require an explicit nonempty inventory")
            operation.consume("states", len(inspection.definitions))
            definitions = {d.identifier: d for d in inspection.definitions}
            compiler = _Compiler(operation)
            seen: set[URIRef | BNode] = set()
            for edit in selected:
                if not isinstance(edit, ShapeCountEdit) or not isinstance(
                    edit.count, ShaclCount
                ):
                    raise InputError("Shape edits require typed count replacements")
                bound_node(edit.shape, operation)
                definition = definitions.get(edit.shape)
                if not isinstance(definition, ShaclPropertyShape):
                    raise InputError(
                        "Count edits must select an inspected property shape"
                    )
                if (
                    isinstance(edit.shape, BNode)
                    and edit.shape is not definition.identifier
                ):
                    raise InputError(
                        "Blank edits require the actual inspected shape term"
                    )
                if edit.shape in seen:
                    raise InputError("Count edits cannot repeat a shape")
                operation.consume("states")
                seen.add(edit.shape)
                compiler.counts(edit.shape, edit.count.minimum, edit.count.maximum)
            original = inspection.source._read(inspection.graph_name, budget=operation)
            triples = []
            for triple in original:
                operation.consume("work")
                operation.consume("states")
                for term in triple:
                    bound_node(term, operation)
                if triple[0] in seen and triple[1] in (SH.minCount, SH.maxCount):
                    continue
                triples.append(triple)
            for triple in compiler.graph:
                operation.consume("work")
                operation.consume("states")
                triples.append(triple)
            source = _owned(
                inspection.source,
                original,
                triples,
                operation,
                selection=inspection.graph_name,
            )
            checked = len(seen)
            checkpoint(operation.deadline, cancel)
            remaining = operation.deadline - time.monotonic()
            if remaining <= 0:
                raise LimitExceeded("Shape edit validation exceeded its deadline")
            with ShaclSession(
                source,
                use_shapes=inspection.profile.roots,
                preflight=True,
                coverage=ShaclCoverageContract(minimum_focus=1),
                limits=operation.limits,
                timeout=remaining,
            ) as session:
                checkpoint(operation.deadline, cancel)
                session._timeout = operation.deadline - time.monotonic()
                result = session._validate(
                    data,
                    focus_nodes=(),
                    graph_name=None,
                    union=False,
                    source_usages=(),
                    scope="dataset",
                    cancel=cancel,
                    budget=operation,
                )
                retain_report(result.report, operation, issues)
                if _coverage(result, inspection.profile.roots, operation) != set(
                    inspection.profile.roots
                ):
                    issues.append(
                        Issue(
                            "shacl.shape_edit_unverified",
                            "Not every selected root has completed "
                            "candidate evaluation",
                            kind="unverified",
                        )
                    )
                validation = result
                children.append(result.report)
            checkpoint(operation.deadline, cancel)
    except LimitExceeded as error:
        issues.exhausted(str(error))
    if validation is None:
        try:
            issues.append(
                Issue(
                    "shacl.shape_edit_unverified",
                    "No actual candidate validation completed",
                    kind="unverified",
                )
            )
        except LimitExceeded as error:
            issues.exhausted(str(error))
    report = Report(
        "shacl-shape-edit",
        checked,
        tuple(issues),
        requested=max(1, checked),
        children=tuple(children),
        regime="explicit-count-edit-and-selected-validation",
    )
    return ShapeEdit(source, validation, report)
