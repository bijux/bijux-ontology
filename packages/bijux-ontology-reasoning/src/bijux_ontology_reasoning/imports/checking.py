"""Checked syntactic MIREOT scope and independent finite consumer obligations."""

from threading import Event

from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.reports import Issue, Report

from ..axioms.inspection import inspect_axioms
from ..axioms.types import (
    AnnotationAssertion,
    AxiomInspection,
    DataProperty,
    Declaration,
    IriValue,
    NamedEntity,
    ObjectProperty,
    ProjectedAxiom,
)
from ..backend.types import OwlProfileBackend
from ..documents.sources.native import prepare_owl_document
from ..documents.types import OwlDocument
from ..entailment.checking import check_entailments
from ..relationships.checking import query_inferred_contracts
from ..relationships.types import (
    InferredContracts,
    InferredQuery,
    RelationshipValue,
)
from ..robot.types import RobotBackend
from .backend import extract_native
from .preparation import MireotPlan, prepare_import
from .types import MireotBoundary, MireotImport, MireotRetention


def _focus(entity: NamedEntity) -> RelationshipValue:
    """Keep actual property direction and entity role in hierarchy comparisons."""
    if entity.kind == "object_property":
        return ObjectProperty(entity.iri)
    if entity.kind == "data_property":
        return DataProperty(entity.iri)
    return entity


def _queries(plan: MireotPlan, operation: Operation) -> tuple[InferredQuery, ...]:
    """Ask the existing maintained hierarchy primitive, including equivalence."""
    operation.consume("bytes", 128 + len(plan.signature) * 512)
    operation.consume("states", len(plan.signature) * 2)
    result: list[InferredQuery] = []
    for entity in plan.signature:
        focus = _focus(entity)
        result.extend(
            (InferredQuery(focus, "super", False), InferredQuery(focus, "equivalent"))
        )
    return tuple(result)


def _hierarchy(
    observed: InferredContracts,
    plan: MireotPlan,
    operation: Operation,
    issues: IssueCollector,
) -> bool:
    """Require actual selected membership/ancestry, not an EXIT0 declaration."""
    if not observed.report.verified or observed.consistent is not True:
        return False
    if len(observed.answers) != len(plan.signature) * 2:
        raise ResourceError("MIREOT hierarchy did not cover every selected role")
    operation.consume("bytes", 128 + len(plan.signature) * 256)
    by_entity: dict[NamedEntity, set[RelationshipValue]] = {}
    for index, entity in enumerate(plan.signature):
        values: set[RelationshipValue] = set()
        for answer in observed.answers[index * 2 : index * 2 + 2]:
            operation.consume("work")
            if entity.kind == "class" and answer.satisfiable is not True:
                issues.append(
                    Issue(
                        "owl.mireot_unusable_class",
                        "A selected class is not established satisfiable",
                        entity.iri,
                        kind="unverified",
                    )
                )
                return False
            for group in answer.groups:
                if not group.complete:
                    raise ResourceError("MIREOT hierarchy membership is incomplete")
                operation.consume("bytes", 128 * len(group.members))
                operation.consume("work", len(group.members))
                for member in group.members:
                    values.add(member.value)
        by_entity[entity] = values
    valid = True
    for boundary in plan.boundaries:
        operation.consume("work")
        if (
            boundary.lower != boundary.upper
            and _focus(boundary.upper) not in by_entity[boundary.lower]
        ):
            valid = False
            issues.append(
                Issue(
                    "owl.mireot_boundary",
                    "The selected upper is not an actual ancestor/equivalent",
                    boundary.lower.iri,
                    expected=boundary.upper.iri,
                )
            )
    return valid


def _inventory(
    inspection: AxiomInspection, operation: Operation
) -> set[ProjectedAxiom]:
    """Bound the transient native structural comparison inventory before hashing."""
    if not inspection.report.verified or len(inspection.documents) != 1:
        raise ResourceError("MIREOT needs one fully represented native ontology")
    if inspection.documents[0].imports:
        raise ResourceError("The initial MIREOT profile requires an import-free source")
    operation.consume("bytes", 128 + len(inspection.axioms) * 128)
    operation.consume("work", (operation.limits.max_depth + 1) * len(inspection.axioms))
    return {record.axiom for record in inspection.axioms}


def _original(
    inspection: AxiomInspection,
    plan: MireotPlan,
    operation: Operation,
    issues: IssueCollector,
) -> bool:
    """Require independent module identity and source-supported exact retention."""
    original = _inventory(inspection, operation)
    header = inspection.documents[0]
    operation.consume("bytes", 256 + len(plan.signature) * 64)
    forbidden = {header.ontology_iri, header.version_iri}
    operation.consume("bytes", 128 * len(original))
    forbidden.update(
        axiom.entity.iri for axiom in original if isinstance(axiom, Declaration)
    )
    forbidden.update(entity.iri for entity in plan.signature)
    if plan.module_iri in forbidden or (
        plan.module_version_iri is not None
        and (
            plan.module_version_iri in forbidden
            or plan.module_version_iri == plan.module_iri
        )
    ):
        raise InputError("The derived ontology needs its own ontology/version identity")
    if header.version_iri is not None and plan.module_version_iri is None:
        raise InputError("A versioned original requires an explicit new module version")
    valid = True
    for axiom in plan.retention.required_axioms:
        operation.consume("work", operation.limits.max_depth + 1)
        if axiom not in original:
            valid = False
            issues.append(
                Issue(
                    "owl.mireot_required_axiom_absent",
                    "A requested retained axiom is not asserted by the original",
                )
            )
    return valid


def _retention(
    original: AxiomInspection,
    current: AxiomInspection,
    plan: MireotPlan,
    operation: Operation,
    issues: IssueCollector,
) -> bool:
    """Check exact original entity annotations, selected properties and axioms."""
    before, after = _inventory(original, operation), _inventory(current, operation)
    header, old_header = current.documents[0], original.documents[0]
    if (header.ontology_iri, header.version_iri) != (
        plan.module_iri,
        plan.module_version_iri,
    ):
        raise ResourceError(
            "Native MIREOT output has the wrong ontology/version identity"
        )
    operation.consume("work", len(header.annotations) + len(old_header.annotations))
    expected_annotations = (
        old_header.annotations if plan.retention.ontology_annotations == "copy" else ()
    )
    operation.consume(
        "bytes", 256 + 128 * (len(header.annotations) + len(expected_annotations))
    )
    if set(header.annotations) != set(expected_annotations):
        raise ResourceError("Native MIREOT did not implement root annotation retention")
    operation.consume(
        "bytes",
        256 + 128 * (len(after) + len(before) + len(plan.retention.required_axioms)),
    )
    retained_iris = {
        axiom.entity.iri for axiom in after if isinstance(axiom, Declaration)
    }
    required: set[ProjectedAxiom] = set(plan.retention.required_axioms)
    required.update(
        Declaration(entity, ()) for entity in plan.retention.required_properties
    )
    for axiom in before:
        operation.consume("work", operation.limits.max_depth + 1)
        if (
            isinstance(axiom, AnnotationAssertion)
            and isinstance(axiom.subject, IriValue)
            and axiom.subject.iri in retained_iris
        ):
            required.add(axiom)
    valid = True
    for axiom in required:
        operation.consume("work", operation.limits.max_depth + 1)
        if axiom not in after:
            valid = False
            issues.append(
                Issue(
                    "owl.mireot_retention_failed",
                    "Native MIREOT omitted a required property, axiom "
                    "or entity annotation",
                )
            )
    for axiom in after:
        operation.consume("work", operation.limits.max_depth + 1)
        if not isinstance(axiom, Declaration) and axiom not in before:
            raise ResourceError(
                "Native MIREOT added an unqualified non-declaration axiom"
            )
    return valid


def extract_mireot(
    runtime: RobotBackend,
    backend: OwlProfileBackend,
    source: OwlDocument,
    boundaries: tuple[MireotBoundary, ...],
    retention: MireotRetention,
    *,
    module_iri: str,
    module_version_iri: str | None = None,
    postconditions: tuple[ProjectedAxiom, ...],
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> MireotImport:
    """Extract an explicit syntactic import and recheck chosen native consumers.

    The initial profile accepts an import-free original Functional document.
    Selected class/object/data lower-upper boundaries use actual maintained
    hierarchy queries. Required properties/axioms and retained entity/root
    annotations are checked structurally. A supplied module identity/version
    replaces the root header explicitly. Paired actual Functional/Turtle outputs
    must describe the same annotated ontology, and actual nonempty entailment
    obligations pass on both original and output. Native success alone proves
    none of these contracts. Unsupported work or any stop publishes no output.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    original = candidate = None
    rdf_content = None
    selected: tuple[MireotBoundary, ...] = ()
    policy = None
    original_inspection = inspection = rdf_inspection = None
    original_hierarchy = hierarchy = None
    before = after = None
    native_identity = None
    reports: list[Report] = []
    complete = False

    def retain(report: Report) -> bool:
        """Save the prepaid child reference before any report adoption can stop."""
        reports.append(report)
        retain_report(report, operation, issues)
        return report.verified

    try:
        plan = prepare_import(
            source,
            boundaries,
            retention,
            postconditions,
            module_iri,
            module_version_iri,
            operation,
        )
        original, selected, policy = plan.source, plan.boundaries, plan.retention
        operation.consume("bytes", 4096)
        operation.consume("states", 32)
        queries = _queries(plan, operation)
        original_inspection = inspect_axioms(
            backend,
            original,
            budget=operation,
            timeout=timeout,
            heap_mb=heap_mb,
            cancel=cancel,
        )
        if not retain(original_inspection.report) or not _original(
            original_inspection, plan, operation, issues
        ):
            raise ResourceError("Original MIREOT source/retention is unqualified")
        original_hierarchy = query_inferred_contracts(
            backend,
            original,
            queries,
            budget=operation,
            timeout=timeout,
            heap_mb=heap_mb,
            cancel=cancel,
        )
        if not retain(original_hierarchy.report) or not _hierarchy(
            original_hierarchy, plan, operation, issues
        ):
            raise ResourceError("Original MIREOT boundaries are unqualified")
        before = check_entailments(
            backend,
            original,
            plan.obligations,
            budget=operation,
            timeout=timeout,
            heap_mb=heap_mb,
            cancel=cancel,
        )
        if not retain(before.report):
            raise ResourceError("Original selected consumer entailments are not proved")
        native = extract_native(runtime, plan, operation, heap_mb)
        native_identity = native.identity
        content, rdf = native.outputs
        if content is None or rdf is None:
            raise ResourceError("Native MIREOT outputs are missing")
        proposed = prepare_owl_document(
            content,
            format="functional",
            document_iri=plan.module_iri,
            effective_url=plan.module_iri,
            budget=operation,
        )
        inspection = inspect_axioms(
            backend,
            proposed,
            budget=operation,
            timeout=timeout,
            heap_mb=heap_mb,
            cancel=cancel,
        )
        if not retain(inspection.report) or not _retention(
            original_inspection, inspection, plan, operation, issues
        ):
            raise ResourceError("Required MIREOT output retention is unqualified")
        rdf_source = prepare_source(
            RdfSource(rdf, format="turtle", document_iri=plan.module_iri),
            budget=operation,
        )
        rdf_inspection = inspect_axioms(
            backend,
            rdf_source,
            budget=operation,
            timeout=timeout,
            heap_mb=heap_mb,
            cancel=cancel,
        )
        if not retain(rdf_inspection.report):
            raise ResourceError("Ordinary RDF MIREOT output is unqualified")
        if _inventory(inspection, operation) != _inventory(rdf_inspection, operation):
            raise ResourceError(
                "Ordinary MIREOT serializations differ in annotated axioms"
            )
        a, b = inspection.documents[0], rdf_inspection.documents[0]
        if (a.ontology_iri, a.version_iri, set(a.annotations)) != (
            b.ontology_iri,
            b.version_iri,
            set(b.annotations),
        ):
            raise ResourceError("Ordinary MIREOT serializations have different headers")
        hierarchy = query_inferred_contracts(
            backend,
            proposed,
            queries,
            budget=operation,
            timeout=timeout,
            heap_mb=heap_mb,
            cancel=cancel,
        )
        if not retain(hierarchy.report) or not _hierarchy(
            hierarchy, plan, operation, issues
        ):
            raise ResourceError("Output MIREOT boundaries are unqualified")
        after = check_entailments(
            backend,
            proposed,
            plan.obligations,
            budget=operation,
            timeout=timeout,
            heap_mb=heap_mb,
            cancel=cancel,
        )
        if not retain(after.report):
            raise ResourceError(
                "Standalone selected consumer entailments are not proved"
            )
        operation.consume("work", 0)
        complete = True
        candidate, rdf_content = proposed, rdf
    except LimitExceeded as error:
        issues.exhausted(str(error), location="owl.mireot")
    except (InputError, OSError) as error:
        try:
            unknown = isinstance(error, ResourceError | OSError)
            issues.append(
                Issue(
                    "owl.mireot_unverified" if unknown else "owl.mireot_input_error",
                    str(error),
                    kind="unverified" if unknown else "input_error",
                )
            )
        except LimitExceeded as stopped:
            issues.exhausted(str(stopped), location="owl.mireot")
    for report in reports:
        issues.retain_completed(report)
    return MireotImport(
        candidate,
        rdf_content,
        original,
        selected,
        policy,
        original_inspection,
        inspection,
        rdf_inspection,
        original_hierarchy,
        hierarchy,
        before,
        after,
        native_identity,
        Report(
            "owl.mireot",
            int(complete),
            tuple(issues),
            requested=1,
            complete=complete,
            children=tuple(reports),
            regime="robot-mireot-explicit-boundaries-selected-consumer-proofs",
        ),
    )
