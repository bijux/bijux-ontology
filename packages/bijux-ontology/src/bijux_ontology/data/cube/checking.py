"""Explicit bounded Data Cube integrity, with every normative constraint accounted."""

from typing import Literal as Choice

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.sources.metadata import SnapshotMetadata
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource

from .facts import CubeFacts
from .integrity.code_lists import (
    check_code_membership,
    check_hierarchy_paths,
    check_inverse_hierarchy_paths,
)
from .integrity.definitions import (
    check_component_optionality,
    check_dataset_structures,
    check_datatype_consistency,
    check_declared_measures,
    check_dimension_code_lists,
    check_dimension_ranges,
    qualify_profile,
)
from .integrity.observations import (
    check_coordinate_uniqueness,
    check_measure_exclusivity,
    check_measure_groups,
    check_observation_datasets,
    check_observation_dimensions,
    check_ordinary_measures,
    check_required_attributes,
    check_selected_measures,
    check_slice_datasets,
)
from .integrity.progress import CubeIntegrity
from .integrity.slices import (
    check_slice_dimensions,
    check_slice_key_properties,
    check_slice_keys,
    check_slice_structures,
)
from .normalization import normalize
from .types import CubeCheck, CubeConstraintCoverage


def check_cube(
    source: PreparedSource,
    *,
    graph_name: GraphSelection | None = None,
    union: bool = False,
    complete: bool = False,
    code_lists_complete: tuple[str, ...] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> CubeCheck:
    """Evaluate IC0–IC21 over one explicitly selected, owned physical RDF graph.

    complete declares the supplied dataset/observation inventory; it never fills
    missing DSDs or code-list definitions. Named code lists close independently.
    Normalization and ordered-collection membership affect only the owned view.
    Each constraint records actual applicability, method and execution coverage.
    Unsupported datatype/schema/comparison profiles retain required uncertainty.
    """
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("Cube checking requires Limits")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Cube checking requires Budget")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Cube limits conflict with the supplied Budget")
    if type(complete) is not bool or type(union) is not bool:
        raise InputError("Cube inventory and union choices must be booleans")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    normalized = None
    selected = None
    metadata = None
    read = None
    methods = tuple(
        "builtin-literals-and-direct-datatype-ranges"
        if index == 0
        else "native-scoped-key-and-sparql-value-comparison"
        if index in (12, 17)
        else "owned-supplied-code-list-membership"
        if index >= 19
        else "normalized-rdf-integrity"
        for index in range(22)
    )
    procedures = (
        check_datatype_consistency,
        check_observation_datasets,
        check_dataset_structures,
        check_declared_measures,
        check_dimension_ranges,
        check_dimension_code_lists,
        check_component_optionality,
        check_slice_keys,
        check_slice_key_properties,
        check_slice_structures,
        check_slice_dimensions,
        check_observation_dimensions,
        check_coordinate_uniqueness,
        check_required_attributes,
        check_ordinary_measures,
        check_selected_measures,
        check_measure_exclusivity,
        check_measure_groups,
        check_slice_datasets,
        check_code_membership,
        check_hierarchy_paths,
        check_inverse_hierarchy_paths,
    )
    coverage = [
        CubeConstraintCoverage(f"IC-{index}", "not_executed", method, None)
        for index, method in enumerate(methods)
    ]
    checked = 0
    checkpoint = None
    try:
        operation.consume("states", 44)
        operation.consume("bytes", 22 * 512)
        checkpoint = issues.checkpoint()
        if type(source) is not PreparedSource:
            raise InputError("Cube checking requires an ordinary owned PreparedSource")
        metadata = SnapshotMetadata.capture(source, operation)
        if union or metadata.union:
            issues.append(
                Issue(
                    "cube.scope_unverified",
                    "Cube profile selects one physical graph",
                    kind="unverified",
                )
            )
        elif (
            metadata.literal_fidelity not in ("source_terms", "supplied_terms")
            or metadata.graph_fidelity != "complete"
        ):
            issues.append(
                Issue(
                    "cube.scope_unverified",
                    "Cube checking needs original literal and physical graph fidelity",
                    kind="unverified",
                )
            )
        else:
            if type(code_lists_complete) is not tuple:
                raise InputError(
                    "Cube code-list closure requires an immutable IRI tuple"
                )
            operation.consume("states", len(code_lists_complete))
            operation.consume("bytes", 64 * len(code_lists_complete))
            for iri in code_lists_complete:
                if type(iri) is not str:
                    raise InputError(
                        "Cube code-list closure requires plain IRI strings"
                    )
                bound_text(iri, operation)
                absolute_iri(iri)
            closed = frozenset(code_lists_complete)
            normalized, selected, metadata = normalize(
                source, graph_name, operation, issues
            )
            read = CubeFacts(normalized, selected, operation)
            checks = CubeIntegrity(read, closed)
            qualify_profile(checks)
            retain_report(
                Report("cube.profile", 1, tuple(checks.issues)), operation, issues
            )
            if not complete:
                issues.append(
                    Issue(
                        "cube.scope_unverified",
                        "Selected Cube inventory is not declared complete",
                        kind="unverified",
                    )
                )
            for index, method in enumerate(methods):
                checks.issues = IssueCollector(operation)
                procedure = procedures[index]
                count = procedure(checks)
                report = Report(
                    f"cube.IC-{index}",
                    count,
                    tuple(checks.issues),
                    allow_empty=True,
                    requested=count,
                )
                status: Choice["complete", "not_applicable", "unverified"] = (
                    "unverified"
                    if not report.complete
                    else "complete"
                    if count
                    else "not_applicable"
                )
                retain_report(report, operation, issues)
                coverage[index] = CubeConstraintCoverage(
                    f"IC-{index}", status, method, report
                )
                checked += 1
            if not metadata.same_scope(SnapshotMetadata.capture(source, operation)):
                raise InputError("Cube source metadata changed during checking")
    except LimitExceeded as error:
        if checkpoint is not None:
            issues.retain_since(checkpoint)
        issues.exhausted(str(error))
        if checked < 22:
            coverage[checked] = CubeConstraintCoverage(
                f"IC-{checked}", "exhausted", methods[checked], None
            )
    except InputError as error:
        try:
            issues.append(Issue("cube.input_error", str(error), kind="input_error"))
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return CubeCheck(
        Report(
            "data.cube",
            checked,
            tuple(issues),
            requested=22,
            complete=checked == 22
            and all(item.status in ("complete", "not_applicable") for item in coverage),
        ),
        normalized,
        () if read is None else read.structures,
        () if read is None else read.observations,
        tuple(coverage),
        () if read is None else read.datasets,
        metadata,
        selected,
    )
