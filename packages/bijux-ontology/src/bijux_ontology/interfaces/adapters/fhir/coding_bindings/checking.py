"""Datatype-specific FHIR binding, identity and display checks on every repetition."""

import threading
from typing import cast

from rdflib.namespace import RDFS

from bijux_ontology._jsonpaths import SourcePath, bound_path
from bijux_ontology.absence import ABSENT
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.inspection import VocabularyView
from bijux_ontology.vocabulary.languages.selection import negotiate_annotations
from bijux_ontology.vocabulary.results import Annotation, Selection

from ..code_system.checking import check_code_membership
from ..code_system.types import FhirCodeMembership, FhirCodeSystem
from ..identity import FhirCodeIdentity
from ..json import UnsupportedJsonNumber, decode_fhir_json, same_json
from ..value_set.types import FhirValueSetPolicy, FhirValueSetSelection
from .preparation import (
    adopt,
    binding_contract,
    diagnostic,
    encode,
    local_systems,
    native_types,
    native_value,
    occurrences,
    reserve,
    selection,
)
from .types import (
    FhirBindingOccurrence,
    FhirCodingBinding,
    FhirCodingBindingCheck,
    FhirCodingEvidence,
    FhirDisplayPolicy,
    FhirExtensibilityDecision,
)


def _failure(error: Exception, issues: IssueCollector) -> None:
    """Keep ordinary caller errors separate from unavailable or exhausted work."""
    if isinstance(error, LimitExceeded | MemoryError | RecursionError):
        issues.exhausted(str(error) or type(error).__name__)
        return
    try:
        if isinstance(error, ResourceError | ImportError | UnsupportedJsonNumber):
            diagnostic(issues, "unverified", str(error), unknown=True)
        else:
            issues.append(
                Issue(
                    "adapter.fhir_coding_input_error",
                    str(error)[:256],
                    kind="input_error",
                )
            )
    except LimitExceeded as limit:
        issues.exhausted(str(limit))


def _decisions(
    values: tuple[FhirExtensibilityDecision, ...], operation: Operation
) -> dict[tuple[SourcePath, str], FhirExtensibilityDecision]:
    """Capture exact justified decisions before any key or value lookup."""
    if type(values) is not tuple:
        raise InputError("Extensibility decisions require a finite immutable tuple")
    reserve(operation, len(values))
    result = {}
    for item in values:
        if type(item) is not FhirExtensibilityDecision:
            raise InputError("Use explicit FhirExtensibilityDecision records")
        bound_path(item.path, operation)
        for text in (item.value_set_reference, item.value_json, item.justification):
            bound_text(text, operation)
        operation.consume("work", len(item.justification) + 1)
        operation.consume("bytes", 4 * len(item.justification))
        if type(item.representable) is not bool or not item.justification.strip():
            raise InputError("Clinical applicability needs a boolean and justification")
        key = item.path, item.value_set_reference
        if key in result:
            raise InputError("Conflicting decisions cannot be selected by input order")
        result[key] = item
    return result


def _profile(
    binding: FhirCodingBinding,
    selected: FhirValueSetSelection,
    reference_matches: bool,
    systems: tuple[FhirCodeSystem, ...],
    operation: Operation,
) -> Report:
    """Apply bare-code restrictions only to that declared datatype profile."""
    issues = IssueCollector(operation)
    checked = 0
    try:
        if not reference_matches:
            diagnostic(
                issues,
                "valueset_unverified",
                "The original ValueSet does not match the exact binding reference",
                binding.path,
                unknown=True,
            )
        if binding.datatype == "code" and binding.strength == "required":
            if binding.external_value_set and "|" not in binding.value_set_reference:
                diagnostic(
                    issues,
                    "bare_binding",
                    "Required bare code needs an external ValueSet version",
                    binding.path,
                )
            reserve(operation, len(selected.identities))
            codes = set()
            for identity in selected.identities:
                operation.consume("work")
                bound_text(identity.code, operation)
                if identity.code in codes:
                    diagnostic(
                        issues,
                        "bare_binding",
                        "Bare code strings must be distinct across systems/versions",
                        binding.path,
                    )
                codes.add(identity.code)
                if identity.version is None:
                    diagnostic(
                        issues,
                        "bare_version_unverified",
                        "Bare-code profile requires explicit CodeSystem versions",
                        binding.path,
                        unknown=True,
                    )
            affected = False
            for system in systems:
                operation.consume("work", len(selected.identities) + 1)
                if system.case_sensitive is False and any(
                    (item.system, item.version) == (system.system, system.version)
                    for item in selected.identities
                ):
                    affected = True
            if binding.mode == "compose" and affected:
                diagnostic(
                    issues,
                    "bare_comparison_unverified",
                    "Referenced case-insensitive bare codes need another profile",
                    binding.path,
                    unknown=True,
                )
        checked = 1
    except LimitExceeded as error:
        issues.exhausted(str(error))
    return Report(
        "adapter.fhir_binding_profile",
        checked,
        tuple(issues),
        requested=1,
        regime="fhir-r5-datatype-binding-profile",
    )


def _display(
    raw: dict[str, object],
    membership: FhirCodeMembership | None,
    system: FhirCodeSystem | None,
    policy: FhirDisplayPolicy,
    path: SourcePath,
    operation: Operation,
    completed: list[Report],
    views: dict[tuple[str, str | None], VocabularyView],
) -> tuple[Selection[Annotation] | None, Report]:
    """Check selected CodeSystem displays separately, preserving language ties."""
    issues = IssueCollector(operation)
    reports: list[Report] = []
    candidates = None
    checked = 0
    if policy.mode == "ignore":
        return None, Report("adapter.fhir_display", allow_empty=True)
    reserve(operation)
    display = raw.get("display")
    if display is None:
        if policy.require_display:
            diagnostic(
                issues,
                "display_missing",
                "Selected display contract requires Coding.display",
                path,
            )
    elif (
        membership is None
        or membership.matched_concept is None
        or system is None
        or system.vocabulary is None
    ):
        diagnostic(
            issues,
            "display_unverified",
            "Display requires an actual resolved CodeSystem concept",
            path,
            unknown=True,
        )
    else:
        if type(display) is not str:
            raise InputError("Coding.display requires original text")
        bound_text(display, operation)
        key = system.system, system.version
        if key not in views:
            reserve(operation)
            views[key] = VocabularyView(
                system.vocabulary, scopes=("local",), budget=operation
            )
        view = views[key]
        reserve(operation)
        if policy.mode == "language":
            candidates = negotiate_annotations(
                view,
                str(membership.matched_concept.iri),
                predicates=(str(RDFS.label),),
                ranges=policy.ranges,
                fallback=policy.fallback,
                source_priority=("local",),
                budget=operation,
            )
        else:
            candidates = view.annotations(
                str(membership.matched_concept.iri),
                predicates=(str(RDFS.label),),
                budget=operation,
            )
        completed.append(candidates.report)
        adopt(candidates.report, operation, issues, reports)
        if not candidates.report.complete:
            diagnostic(
                issues,
                "display_unverified",
                "Display candidate selection did not complete",
                path,
                unknown=True,
            )
        elif not any(item.value == display for item in candidates.items):
            diagnostic(
                issues,
                "display_mismatch",
                "Display is not one of the selected CodeSystem labels/designations",
                path,
            )
    checked = 1
    report = Report(
        "adapter.fhir_display",
        checked,
        tuple(issues),
        requested=1,
        children=tuple(reports),
        regime="fhir-r5-codesystem-display",
    )
    completed.append(report)
    return candidates, report


def _coding(
    raw: dict[str, object],
    path: SourcePath,
    systems: tuple[FhirCodeSystem, ...],
    policy: FhirValueSetPolicy,
    display_policy: FhirDisplayPolicy,
    operation: Operation,
    completed: list[Report],
    views: dict[tuple[str, str | None], VocabularyView],
) -> FhirCodingEvidence:
    """Resolve each original coding independently of the ValueSet obligation."""
    reserve(operation, 3)
    source = encode(raw, operation)
    issues = IssueCollector(operation)
    reports: list[Report] = []
    identity = None
    membership = None
    system = None
    uri, version, code = raw.get("system"), raw.get("version"), raw.get("code")
    if (
        type(uri) is not str
        or type(code) is not str
        or version is not None
        and type(version) is not str
    ):
        diagnostic(
            issues,
            "identity_unverified",
            "Coding needs an explicit system, optional version and code",
            path,
            unknown=True,
        )
    else:
        for text in (uri, code, version):
            if text is not None:
                bound_text(text, operation)
        try:
            absolute_iri(uri)
        except InputError:
            diagnostic(
                issues,
                "system_identity",
                "Coding.system must be an absolute CodeSystem URI",
                path,
            )
        else:
            identity = FhirCodeIdentity(uri, version, code)
            operation.consume("work", len(systems) + 1)
            candidates = [s for s in systems if (s.system, s.version) == (uri, version)]
            if version is None and not policy.allow_unversioned:
                diagnostic(
                    issues,
                    "identity_unverified",
                    "Selected version policy requires an explicit Coding.version",
                    path,
                    unknown=True,
                )
            elif len(candidates) != 1:
                diagnostic(
                    issues,
                    "identity_unverified",
                    "Exactly one supplied CodeSystem/version must resolve a coding",
                    path,
                    unknown=True,
                )
            else:
                system = candidates[0]
                reserve(operation)
                membership = check_code_membership(system, identity, budget=operation)
                completed.append(membership.report)
                adopt(membership.report, operation, issues, reports)
                if membership.status == "absent":
                    diagnostic(
                        issues,
                        "unknown_code",
                        "Code is absent from the complete selected CodeSystem",
                        path,
                    )
    identity_report = Report(
        "adapter.fhir_coding_identity",
        1,
        tuple(issues),
        requested=1,
        children=tuple(reports),
        regime="fhir-r5-exact-system-version-code",
    )
    completed.append(identity_report)
    selected_display, display_report = _display(
        raw, membership, system, display_policy, path, operation, completed, views
    )
    return FhirCodingEvidence(
        path,
        source,
        identity,
        membership,
        selected_display,
        identity_report,
        display_report,
        system,
    )


def _eligible(
    coding: FhirCodingEvidence,
    selected: FhirValueSetSelection,
    qualified: bool,
    operation: Operation,
) -> bool | None:
    """Distinguish known exclusion from missing identity/selection evidence."""
    if (
        not qualified
        or not coding.identity_report.verified
        or coding.membership is None
        or coding.membership.matched is None
    ):
        return None
    operation.consume("work", len(selected.identities) + 1)
    if coding.membership.matched in selected.identities:
        return True
    if coding.code_system is None or coding.code_system.case_sensitive is None:
        return None
    return False


def _binding(
    raw: object,
    codings: tuple[FhirCodingEvidence, ...],
    selected: FhirValueSetSelection,
    qualified: bool,
    contract: FhirCodingBinding,
    path: SourcePath,
    decisions: dict[tuple[SourcePath, str], FhirExtensibilityDecision],
    operation: Operation,
    recommendations: IssueCollector,
) -> tuple[bool | None, Report]:
    """Existential CodeableConcept membership differs from repeated field semantics."""
    issues = IssueCollector(operation)
    conforms: bool | None = None
    if contract.datatype == "code":
        operation.consume("work", len(selected.identities) + 1)
        member = (
            any(identity.code == raw for identity in selected.identities)
            if qualified
            else None
        )
    else:
        matches = [
            _eligible(coding, selected, qualified, operation) for coding in codings
        ]
        member = True if True in matches else None if None in matches else False
    if contract.strength in {"preferred", "example"}:
        conforms = True
        if contract.strength == "preferred" and member is False:
            diagnostic(
                recommendations,
                "preferred_recommendation",
                "Preferred binding recommends an applicable in-set coding",
                path,
                warning=True,
            )
        elif contract.strength == "preferred" and member is None:
            diagnostic(
                recommendations,
                "recommendation_unverified",
                "Preferred membership could not be established",
                path,
                unknown=True,
            )
    elif member is True:
        conforms = True
    elif (
        not codings
        and contract.datatype == "CodeableConcept"
        and contract.strength == "required"
    ):
        conforms = False
        diagnostic(
            issues,
            "required_binding",
            "Required CodeableConcept needs at least one qualifying Coding",
            path,
        )
    elif member is None:
        diagnostic(
            issues,
            "binding_unverified",
            "Incomplete selection/identity cannot prove binding exclusion",
            path,
            unknown=True,
        )
    elif contract.strength == "required":
        conforms = False
        diagnostic(
            issues,
            "required_binding",
            "No coding satisfies the required ValueSet binding",
            path,
        )
    else:
        decision = decisions.get((path, contract.value_set_reference))
        if decision is None or not same_json(
            decode_fhir_json(decision.value_json, operation), raw, operation
        ):
            diagnostic(
                issues,
                "extensible_unverified",
                "Extensible concept needs a justified decision for this value",
                path,
                unknown=True,
            )
        elif decision.representable:
            conforms = False
            diagnostic(
                issues,
                "extensible_binding",
                "Caller states that an in-set concept applies and must be used",
                path,
            )
        else:
            conforms = True
    return conforms, Report(
        "adapter.fhir_datatype_binding",
        1,
        tuple(issues),
        requested=1,
        regime="fhir-r5-" + contract.strength + "-" + contract.datatype,
    )


def check_coding_bindings(
    record: object,
    bindings: tuple[FhirCodingBinding, ...],
    *,
    systems: tuple[FhirCodeSystem, ...],
    decisions: tuple[FhirExtensibilityDecision, ...] = (),
    policy: FhirValueSetPolicy | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: threading.Event | None = None,
) -> FhirCodingBindingCheck:
    """Check every selected repetition without requiring every translation coding.

    Required CodeableConcept is existential within each occurrence; a repeated
    field must satisfy that obligation independently at every position. Preferred
    findings and example memberships are not required conformance failures.
    Extensibility judgments are explicit caller evidence, never clinical guesses.
    This is not complete resource/profile validation or automatic translation.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues, recommendations = IssueCollector(operation), IssueCollector(operation)
    required: list[Report] = []
    completed: list[Report] = []
    observed: list[FhirBindingOccurrence] = []
    views: dict[tuple[str, str | None], VocabularyView] = {}
    selected_policy = FhirValueSetPolicy() if policy is None else policy
    complete = False
    recommendation_count = 0
    try:
        if (
            type(bindings) is not tuple
            or not bindings
            or type(selected_policy) is not FhirValueSetPolicy
            or type(selected_policy.allow_unversioned) is not bool
            or type(selected_policy.include_inactive) is not bool
        ):
            raise InputError(
                "Use nonempty explicit bindings and finite version/activity policies"
            )
        reserve(operation, len(bindings))
        for contract in bindings:
            binding_contract(contract, operation)
        native = native_types()
        selected_decisions = _decisions(decisions, operation)
        prepared_systems = local_systems(systems, operation, completed)
        for contract in bindings:
            reserve(operation, 3)
            selected, reference_matches = selection(
                contract, prepared_systems, selected_policy, operation
            )
            completed.append(selected.report)
            profile = _profile(
                contract, selected, reference_matches, prepared_systems, operation
            )
            completed.append(profile)
            qualified = (
                reference_matches
                and profile.verified
                and selected.report.verified
                and selected.enumeration.closed
            )
            if contract.strength in {"required", "extensible"}:
                adopt(profile, operation, issues, required)
                adopt(selected.report, operation, issues, required)
            else:
                recommendations.retain_completed(profile)
                recommendations.retain_completed(selected.report)
            for where, value in occurrences(record, contract.path, operation, native):
                reserve(operation)
                group_issues = IssueCollector(operation)
                repeated = type(value) in {list, tuple}
                if value is ABSENT:
                    values: tuple[object, ...] = ()
                elif type(value) in {list, tuple}:
                    values = capture(
                        cast(list[object] | tuple[object, ...], value), operation
                    )
                else:
                    values = (value,)
                if (
                    len(values) < contract.minimum_occurrences
                    or contract.maximum_occurrences is not None
                    and len(values) > contract.maximum_occurrences
                ):
                    diagnostic(
                        group_issues,
                        "occurrence_count",
                        "Selected field repetitions violate explicit occurrence counts",
                        where,
                    )
                group = Report(
                    "adapter.fhir_field_occurrences",
                    1,
                    tuple(group_issues),
                    requested=1,
                )
                completed.append(group)
                adopt(group, operation, issues, required)
                for index, value in enumerate(values):
                    reserve(operation, 4)
                    path = (*where, index) if repeated else where
                    # The original terminal container, not its number of items,
                    # determines whether a concrete source array index exists.
                    if type(value) is tuple:
                        raise InputError(
                            "Nested FHIR element arrays are not a datatype value"
                        )
                    try:
                        raw, source_json = native_value(
                            value, contract.datatype, operation, native
                        )
                    except LimitExceeded:
                        raise
                    except (InputError, ValueError, TypeError, ResourceError) as error:
                        invalid = IssueCollector(operation)
                        diagnostic(
                            invalid,
                            "structure_unverified"
                            if isinstance(error, ResourceError)
                            else "structure",
                            str(error)[:192],
                            path,
                            unknown=isinstance(error, ResourceError),
                        )
                        malformed = Report(
                            "adapter.fhir_coding_structure",
                            1,
                            tuple(invalid),
                            requested=1,
                        )
                        completed.append(malformed)
                        adopt(malformed, operation, issues, required)
                        continue
                    codings: list[FhirCodingEvidence] = []
                    if contract.datatype != "code":
                        items = (
                            [raw]
                            if contract.datatype == "Coding"
                            else raw.get("coding", [])
                            if isinstance(raw, dict)
                            else []
                        )
                        reserve(operation, len(items))
                        for ci, item in enumerate(items):
                            if not isinstance(item, dict):
                                raise InputError(
                                    "Native Coding source must remain an object"
                                )
                            cp = (
                                path
                                if contract.datatype == "Coding"
                                else (*path, "coding", ci)
                            )
                            coding = _coding(
                                item,
                                cp,
                                prepared_systems,
                                selected_policy,
                                contract.display,
                                operation,
                                completed,
                                views,
                            )
                            codings.append(coding)
                            adopt(coding.identity_report, operation, issues, required)
                            if contract.display.mode != "ignore":
                                adopt(
                                    coding.display_report, operation, issues, required
                                )
                    reserve(operation)
                    conforms, binding_report = _binding(
                        raw,
                        tuple(codings),
                        selected,
                        qualified,
                        contract,
                        path,
                        selected_decisions,
                        operation,
                        recommendations,
                    )
                    completed.append(binding_report)
                    occurrence = FhirBindingOccurrence(
                        path,
                        contract.datatype,
                        source_json,
                        tuple(codings),
                        conforms,
                        binding_report,
                        contract,
                    )
                    observed.append(occurrence)
                    recommendation_count += contract.strength == "preferred"
                    adopt(binding_report, operation, issues, required)
        operation.consume("work", 0)
        complete = True
    except (
        InputError,
        ValueError,
        TypeError,
        ImportError,
        UnicodeError,
        MemoryError,
        RecursionError,
        UnsupportedJsonNumber,
    ) as error:
        for report in completed:
            issues.retain_completed(report)
        _failure(error, issues)
    report = Report(
        "adapter.fhir_coding_bindings",
        len(required),
        tuple(issues),
        requested=len(required) if complete else len(required) + 1,
        complete=complete,
        allow_empty=complete,
        children=tuple(required),
        regime="fhir-r5-selected-coding-bindings",
    )
    recommendation_report = Report(
        "adapter.fhir_binding_recommendations",
        recommendation_count,
        tuple(recommendations),
        requested=recommendation_count,
        allow_empty=True,
        regime="fhir-r5-nonrequired-recommendations",
    )
    return FhirCodingBindingCheck(
        tuple(observed), report, recommendation_report, tuple(completed)
    )
