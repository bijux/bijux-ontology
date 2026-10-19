"""Checked closure-wide native role transformations and explicit interfaces."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import cast

from bijux_ontology._json import object_value
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.workflows.ontology.modules.types import ModuleContract

from ...axioms.changes.preconditions import qualify_base, source_base
from ...axioms.changes.qualification import retain_error, retain_losses
from ...axioms.changes.types import AxiomBase
from ...axioms.codec.decoding import ProjectionDecoder
from ...backend.owlapi.bundles import PROFILES, qualify_backend
from ...backend.owlapi.execution import invoke_worker, qualify_capability
from ...backend.owlapi.protocol import result_counter
from ...backend.types import OwlProfile, OwlProfileBackend
from ...documents.receipts import native_profiles
from ...documents.sources.capture import capture_document, capture_imports
from ...documents.sources.native import (
    native_iri,
    prepare_owl_document,
    stage_native_documents,
)
from ...documents.types import OwlDocument
from ...theories.interfaces import (
    ModuleInterfaceChange,
    ModuleSet,
    capture_contract,
    capture_interfaces,
)
from .annotations import annotation_policy
from .types import (
    AnnotationRole,
    ModuleCheck,
    RenameCandidate,
    RenamedModule,
    RoleRename,
)


def _rename(value: RoleRename, budget: Budget) -> tuple[str, str, tuple[str, ...]]:
    """Capture typed identity roles once before compilation or callbacks."""
    if type(value) is not RoleRename:
        raise InputError("Role renaming requires an ordinary explicit record")
    source, target, roles = value.source, value.target, value.roles
    old, new = native_iri(source, budget), native_iri(target, budget)
    selected = capture_items(roles, str, budget)
    allowed = (
        "class",
        "object_property",
        "data_property",
        "annotation_property",
        "datatype",
        "named_individual",
        "all",
    )
    if (
        old == new
        or not selected
        or any(type(role) is not str or role not in allowed for role in selected)
    ):
        raise InputError(
            "Choose different identities and explicit supported entity roles"
        )
    if len(set(selected)) != len(selected) or (
        "all" in selected and selected != ("all",)
    ):
        raise InputError("All roles is a separate choice; duplicate roles are invalid")
    return old, new, selected


def _records(raw: object, maximum: int, budget: Budget) -> list[object]:
    """Validate bounded native inventories before creating retained references."""
    if type(raw) is not list or len(raw) > maximum:
        raise ResourceError("Invalid native rename inventory")
    budget.consume("states", len(raw))
    budget.consume("bytes", 64 + len(raw) * 64)
    return cast(list[object], raw)


def _checks(
    values: Sequence[ModuleCheck],
    documents: tuple[OwlDocument, ...],
    interfaces: tuple[tuple[str, ModuleContract], ...],
    operation: Operation,
    issues: IssueCollector,
    retained: list[Report],
) -> None:
    """Actual nonempty checks receive defensive whole-set witnesses and one budget."""
    selected = capture_items(values, ModuleCheck, operation)
    operation.consume("bytes", 128 + len(selected) * 128)
    if not selected:
        issues.append(
            Issue(
                "owl.rename_postconditions_missing",
                "Renaming requires selected whole-module-set postconditions",
                kind="unverified",
            )
        )
    names: set[str] = set()
    for value in selected:
        if type(value) is not ModuleCheck or type(value.name) is not str:
            raise InputError("Postconditions require ordinary named module checks")
        bound_text(value.name, operation)
        if not value.name or value.name in names or not callable(value.check):
            raise InputError("Postconditions need distinct names and callable checks")
        operation.consume("states")
        names.add(value.name)
        operation.consume("bytes", 128 + len(documents) * 64 + len(interfaces) * 64)
        witness = ModuleSet(
            tuple(capture_document(document, operation) for document in documents),
            tuple(
                (key, capture_contract(contract, operation))
                for key, contract in interfaces
            ),
        )
        checked = value.check(witness, operation)
        if not isinstance(checked, Report):
            raise InputError("Whole-module-set checks must return Reports")
        try:
            retain_report(checked, operation, issues)
        except LimitExceeded:
            issues.retain_completed(checked)
            retained.append(checked)
            raise
        retained.append(checked)
        if not checked.checked:
            issues.append(
                Issue(
                    "owl.rename_postcondition_unverified",
                    "A required postcondition completed no actual comparison",
                    value.name,
                    kind="unverified",
                )
            )


def prepare_role_rename(
    backend: OwlProfileBackend,
    source: OwlDocument,
    rename: RoleRename,
    *,
    expected: Mapping[str, AxiomBase],
    owned: Sequence[str],
    imports: Mapping[str, OwlDocument] | None = None,
    annotations: Sequence[AnnotationRole] = (),
    interfaces: Mapping[str, ModuleContract] | None = None,
    interface_changes: Sequence[ModuleInterfaceChange] = (),
    profiles: Sequence[OwlProfile] = (),
    after_checks: Sequence[ModuleCheck] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> RenameCandidate:
    """Plan one complete structural rename over a supplied connected module closure.

    Every supplied module must be actually loaded. Exact bytes, syntax and bases
    are preconditions for every module, and required edits in an unowned module
    reject the entire transaction. Existing target entities are conservatively
    rejected. Annotation positions need explicit policy when the old IRI occurs.
    Rules and anonymous-individual correspondence remain unsupported. Profile
    receipts do not prove consistency; selected actual after checks own logical
    and application acceptance. This returns reviewable bytes without publishing
    files or implying atomic replacement of multiple destinations.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    decoder = ProjectionDecoder(operation)
    modules: tuple[RenamedModule, ...] = ()
    original_interfaces: tuple[tuple[str, ModuleContract], ...] = ()
    updated_interfaces: tuple[tuple[str, ModuleContract], ...] = ()
    checks: list[Report] = []
    capability = None
    finished = False
    try:
        original = capture_document(source, operation)
        supplied = capture_imports(imports, operation)
        old, new, roles = _rename(rename, operation)
        annotation_wire = annotation_policy(annotations, operation)
        selected = capture_items(profiles, str, operation)
        if any(
            type(profile) is not str or profile not in PROFILES for profile in selected
        ) or len(set(selected)) != len(selected):
            raise InputError("Choose distinct supported native profiles")
        selected_profiles = cast(tuple[OwlProfile, ...], selected)
        selected_owned = capture_items(owned, str, operation)
        operation.consume("bytes", 128 + len(selected_owned) * 128)
        selected_identities = {
            native_iri(identity, operation) for identity in selected_owned
        }
        if not selected_owned or len(selected_identities) != len(selected_owned):
            raise InputError("Choose distinct explicitly owned document identities")
        if not isinstance(expected, Mapping):
            raise InputError(
                "Expected bases require an exact effective-document mapping"
            )
        expected_size = len(expected)
        operation.consume("states", expected_size)
        operation.consume("bytes", 128 + expected_size * 192)
        bases: dict[str, AxiomBase] = {}
        for identity, base in expected.items():
            if len(bases) >= expected_size:
                raise InputError("Expected bases changed during capture")
            bases[native_iri(identity, operation)] = qualify_base(base, operation)
        if len(bases) != expected_size:
            raise InputError("Expected bases changed during capture")
        qualify_backend(backend, operation)
        backend.artifacts.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(
            prefix="entity-renaming-", dir=backend.artifacts
        ) as job:
            directory = Path(job)
            inputs, request = stage_native_documents(
                original, supplied, directory, operation
            )
            operation.consume("bytes", 128 + len(inputs) * 192)
            by_identity = {
                document.effective_url: index for index, document in enumerate(inputs)
            }
            if set(bases) != set(by_identity) or any(
                bases[document.effective_url] != source_base(document)
                for document in inputs
            ):
                issues.append(
                    Issue(
                        "owl.rename_base_mismatch",
                        "Original module bytes or interpretation context "
                        "differ from required bases",
                    )
                )
            elif not selected_identities <= set(by_identity):
                raise InputError("An owned module is outside the supplied inventory")
            else:
                original_interfaces, updated_interfaces = capture_interfaces(
                    interfaces,
                    interface_changes,
                    set(by_identity),
                    selected_identities,
                    operation,
                )
                allowance = operation.remaining("states")
                diagnostic_allowance = operation.remaining("issues")
                if not allowance or not diagnostic_allowance:
                    raise LimitExceeded(
                        "Rename has no remaining native inventory allowance"
                    )
                operation.consume("bytes", 64 + len(selected_identities) * 32)
                owned_indices = {
                    by_identity[identity] for identity in selected_identities
                }
                request.update(
                    operation="role_rename",
                    source=old,
                    target=new,
                    roles=roles,
                    owned=sorted(owned_indices),
                    annotations=annotation_wire,
                    profiles=selected_profiles,
                    max_bytes=operation.limits.max_bytes,
                    max_nodes=allowance,
                    max_issues=diagnostic_allowance,
                    max_resources=operation.limits.max_resources,
                )
                data = invoke_worker(backend, request, directory, operation, heap_mb)
                capability = qualify_capability(data, backend, operation)
                retain_losses(data.get("changes"), decoder, issues)
                failure = data.get("failure")
                if failure is not None:
                    reason = decoder.text(failure)
                    detail = (
                        decoder.text(data.get("failure_value"))
                        if data.get("failure_value") is not None
                        else None
                    )
                    issues.append(
                        Issue(
                            "owl.rename_" + reason,
                            "Native role refactoring cannot establish "
                            "a complete safe transaction",
                            actual=detail,
                            kind="unverified",
                        )
                    )
                elif data.get("qualified") is not True:
                    issues.append(
                        Issue(
                            "owl.rename_unverified",
                            "Native role refactoring or complete reread did not finish",
                            kind="unverified",
                        )
                    )
                else:
                    operation.consume("bytes", 128 + len(owned_indices) * 256)
                    edits: dict[int, tuple[tuple[str, ...], tuple[str, ...]]] = {}
                    for raw in _records(
                        data.get("edits"), len(owned_indices), operation
                    ):
                        item = object_value(raw, location="native module edit")
                        index = result_counter(
                            item.get("document"), operation, len(inputs) - 1
                        )
                        if index not in owned_indices or index in edits:
                            raise ResourceError(
                                "Native edits have an unselected or duplicate module"
                            )
                        additions = tuple(
                            decoder.text(value)
                            for value in _records(
                                item.get("additions"),
                                operation.limits.max_states,
                                operation,
                            )
                        )
                        removals = tuple(
                            decoder.text(value)
                            for value in _records(
                                item.get("removals"),
                                operation.limits.max_states,
                                operation,
                            )
                        )
                        edits[index] = additions, removals
                    candidates: dict[int, RenamedModule] = {}
                    for raw in _records(
                        data.get("modules"), len(owned_indices), operation
                    ):
                        item = object_value(raw, location="native checked module")
                        index = result_counter(
                            item.get("document"), operation, len(inputs) - 1
                        )
                        if index not in edits or index in candidates:
                            raise ResourceError(
                                "Native candidates have an unselected "
                                "or duplicate module"
                            )
                        observations = native_profiles(
                            item.get("after_profiles"), selected_profiles, operation
                        )
                        for observation in observations:
                            if not observation.in_profile:
                                issues.append(
                                    Issue(
                                        "owl.rename_profile_violation",
                                        "Candidate module violates "
                                        "a selected native profile",
                                        inputs[index].effective_url,
                                        actual=observation.profile,
                                    )
                                )
                        size = result_counter(
                            item.get("bytes"), operation, operation.limits.max_bytes
                        )
                        operation.consume("bytes", size)
                        path = directory / f"candidate-{index}.owl"
                        if (
                            path.is_symlink()
                            or not path.is_file()
                            or path.stat().st_size != size
                        ):
                            raise ResourceError(
                                "Native module byte receipt is unavailable"
                            )
                        candidate = prepare_owl_document(
                            path.read_bytes(),
                            format="functional",
                            document_iri=inputs[index].document_iri,
                            effective_url=inputs[index].effective_url,
                            budget=operation,
                        )
                        additions, removals = edits[index]
                        candidates[index] = RenamedModule(
                            inputs[index],
                            candidate,
                            source_base(inputs[index]),
                            additions,
                            removals,
                            observations,
                        )
                    if set(candidates) != owned_indices or set(edits) != owned_indices:
                        raise ResourceError(
                            "Native transaction omitted selected modules"
                        )
                    operation.consume("bytes", 128 + len(inputs) * 64)
                    all_documents = tuple(
                        candidates[index].source if index in candidates else document
                        for index, document in enumerate(inputs)
                    )
                    modules = tuple(candidates[index] for index in sorted(candidates))
                    _checks(
                        after_checks,
                        all_documents,
                        updated_interfaces,
                        operation,
                        issues,
                        checks,
                    )
                    finished = True
                if data["status"] == "exhausted":
                    raise LimitExceeded(
                        "Native role refactoring exhausted its allowance"
                    )
    except LimitExceeded as error:
        issues.exhausted(str(error), location="owl.role_rename")
    except (InputError, OSError) as error:
        retain_error(issues, error, "owl.role_rename")
    return RenameCandidate(
        modules,
        updated_interfaces,
        original_interfaces,
        tuple(checks),
        capability,
        Report(
            "owl.role_rename",
            int(finished),
            tuple(issues),
            requested=1,
            complete=finished,
            children=tuple(checks),
            regime="checked-structural-role-rename-owned-native-closure",
        ),
    )
