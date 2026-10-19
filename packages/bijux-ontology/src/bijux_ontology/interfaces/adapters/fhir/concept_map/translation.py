"""Finite directed R5 translation through actual local terminology contracts."""

import threading
from dataclasses import dataclass, field
from importlib import import_module
from typing import Literal

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.reports import Report

from ..code_system.checking import check_code_membership
from ..code_system.reading import read_code_system
from ..code_system.types import FhirCodeSystem
from ..identity import FhirCodeBinding, FhirCodeIdentity
from ..json import UnsupportedJsonNumber, decode_fhir_json, same_json
from ..value_set.composition import compile_value_set
from ..value_set.types import FhirValueSet, FhirValueSetPolicy
from .reading import canonical, failure, note, obj, read_concept_map, reserve
from .types import (
    FhirConceptMap,
    FhirMapCondition,
    FhirMapContext,
    FhirMapGroup,
    FhirMapMatch,
    FhirMapValue,
    FhirMapValueSet,
    FhirTranslation,
)


def _identity(value: FhirCodeIdentity, budget: Budget) -> FhirCodeIdentity:
    """Qualify all source identity scalars before equality or keyed lookup."""
    if type(value) is not FhirCodeIdentity:
        raise InputError("Translation requires an exact FhirCodeIdentity")
    for text in (value.system, value.code, value.version):
        if text is not None:
            if type(text) is not str or not text:
                raise InputError("FHIR identity requires original nonempty text")
            bound_text(text, budget)
    absolute_iri(value.system)
    return value


@dataclass
class _Translation:
    """One shared budget, local resource catalogue and required evidence prefix."""

    operation: Operation
    issues: IssueCollector
    systems: tuple[FhirCodeSystem, ...]
    value_sets: tuple[FhirMapValueSet, ...]
    policy: FhirValueSetPolicy
    reports: list[Report] = field(default_factory=list)
    selections: dict[str, tuple[FhirCodeIdentity, ...] | None] = field(
        default_factory=dict
    )

    def adopt(self, report: Report) -> None:
        """Preserve this paid child immediately before fallible metadata adoption."""
        self.reports.append(report)
        self.issues.retain_completed(report)
        retain_report(report, self.operation, self.issues)

    def resolve(
        self, identity: FhirCodeIdentity, path: tuple[str | int, ...]
    ) -> FhirCodeIdentity | None:
        """Return the original matched code under actual identity and case evidence."""
        _identity(identity, self.operation)
        if identity.version is None and not self.policy.allow_unversioned:
            note(
                self.issues,
                "The selected version policy requires an explicit business version",
                path=path,
            )
            return None
        self.operation.consume("work", len(self.systems) + 1)
        candidates = [
            s
            for s in self.systems
            if s.system == identity.system and s.version == identity.version
        ]
        if len(candidates) != 1:
            note(
                self.issues,
                "Exactly one supplied system/version must resolve the requested code",
                path=path,
            )
            return None
        reserve(self.operation)
        result = check_code_membership(candidates[0], identity, budget=self.operation)
        self.adopt(result.report)
        if result.status == "unknown":
            return None
        if result.status == "absent":
            note(
                self.issues,
                "The declared code is absent from the complete selected CodeSystem",
                path=path,
                finding=True,
            )
            return None
        return result.matched

    def selection(self, reference: str) -> tuple[FhirCodeIdentity, ...] | None:
        """Compile the actual original ValueSet and its required complete scope."""
        uri, version = canonical(reference, self.operation)
        if reference in self.selections:
            return self.selections[reference]
        reserve(self.operation)
        candidates = []
        for supplied in self.value_sets:
            self.operation.consume("work")
            raw = obj(decode_fhir_json(supplied.value_set.payload, self.operation))
            if raw.get("url") == uri and raw.get("version") == version:
                candidates.append(supplied)
        selected = None
        if len(candidates) != 1:
            note(
                self.issues,
                "Require one original ValueSet with the exact canonical version",
            )
        else:
            bindings: list[FhirCodeBinding] = []
            for system in self.systems:
                reserve(self.operation, len(system.bindings))
                bindings.extend(system.bindings)
            reserve(self.operation)
            result = compile_value_set(
                candidates[0].value_set,
                mode=candidates[0].mode,
                systems=self.systems,
                bindings=tuple(bindings),
                policy=self.policy,
                budget=self.operation,
            )
            self.adopt(result.report)
            if result.report.verified and result.enumeration.closed:
                selected = result.identities
            else:
                note(
                    self.issues,
                    "Required ValueSet selection is not complete and qualified",
                )
        self.selections[reference] = selected
        return selected

    def scope(self, reference: str | None) -> tuple[FhirCodeIdentity, ...] | None:
        """An absent scope is unrestricted; a missing one stays unknown."""
        return self.selection(reference) if reference is not None else None

    def system(
        self, reference: str | None, scope: str | None, path: tuple[str | int, ...]
    ) -> tuple[str, str | None] | None:
        """Resolve omitted systems only from complete single-system scopes."""
        if reference is not None:
            return canonical(reference, self.operation)
        if scope is not None:
            identities = self.selection(scope)
            if identities is not None:
                reserve(self.operation, len(identities))
                pairs = {(i.system, i.version) for i in identities}
                if len(pairs) == 1:
                    return next(iter(pairs))
        note(
            self.issues,
            "An omitted group system requires a complete single-system scope",
            path=path,
        )
        return None

    def codes(
        self,
        code: str | None,
        value_set: str | None,
        system: tuple[str, str | None] | None,
        scope: str | None,
        path: tuple[str | int, ...],
    ) -> tuple[FhirCodeIdentity, ...] | None:
        """Expand only actual complete selectors and intersect the declared scope."""
        values = None
        if value_set is not None:
            values = self.selection(value_set)
            if values is not None and system is not None:
                reserve(self.operation, len(values))
                self.operation.consume("work", len(values) + 1)
                values = tuple(v for v in values if (v.system, v.version) == system)
        elif code is not None and system is not None:
            identity = self.resolve(FhirCodeIdentity(*system, code), path)
            values = None if identity is None else (identity,)
        elif code is not None:
            note(
                self.issues,
                "A target/source code has no resolved group system",
                path=path,
            )
        if scope is not None:
            allowed = self.selection(scope)
            if allowed is None:
                return None
            if values is not None:
                reserve(self.operation, len(allowed) + len(values))
                permitted = set(allowed)
                values = tuple(v for v in values if v in permitted)
        return values


def _context(
    context: tuple[FhirMapContext, ...], attributes: dict[str, str], state: _Translation
) -> dict[str, FhirMapValue]:
    """Qualify typed caller values through the maintained choice model."""
    if type(context) is not tuple:
        raise InputError("Context requires an immutable ordered tuple")
    reserve(state.operation, len(context))
    result = {}
    native = import_module(
        "fhir.resources.conceptmap"
    ).ConceptMapGroupElementTargetDependsOn
    for item in context:
        if type(item) is not FhirMapContext or type(item.value) is not FhirMapValue:
            raise InputError("Context requires exact typed additional attribute values")
        for text in (item.attribute, item.value.kind, item.value.value_json):
            bound_text(text, state.operation)
        if (
            item.attribute in result
            or attributes.get(item.attribute) != item.value.kind
        ):
            raise InputError(
                "Context attributes require unique declared matching types"
            )
        raw = decode_fhir_json(item.value.value_json, state.operation)
        field_name = "value" + item.value.kind[0].upper() + item.value.kind[1:]
        record = {"attribute": item.attribute, field_name: raw}
        reserve(state.operation)
        typed = native.model_validate(record, strict=True)
        if not same_json(
            record,
            decode_fhir_json(typed.model_dump_json(), state.operation),
            state.operation,
        ):
            raise ResourceError(
                "Native context coercion changes the original typed value"
            )
        result[item.attribute] = item.value
    return result


def _coding(raw: object, budget: Budget) -> FhirCodeIdentity | None:
    """Coding identity uses original system/version/code, independently of display."""
    data = obj(raw)
    system, code, version = data.get("system"), data.get("code"), data.get("version")
    if (
        type(system) is not str
        or type(code) is not str
        or version is not None
        and type(version) is not str
    ):
        return None
    return _identity(FhirCodeIdentity(system, version, code), budget)


def _conditions(
    conditions: tuple[FhirMapCondition, ...],
    context: dict[str, FhirMapValue],
    group: FhirMapGroup,
    state: _Translation,
    path: tuple[str | int, ...],
) -> bool | None:
    """AND all conditions, keeping unknown required context unresolved."""
    applicable: bool | None = True
    for condition in conditions:
        state.operation.consume("work")
        actual = context.get(condition.attribute)
        if actual is None:
            note(
                state.issues,
                "Required dependsOn attribute was not supplied: " + condition.attribute,
                path=path,
            )
            applicable = None
            continue
        raw = decode_fhir_json(actual.value_json, state.operation)
        if condition.value_set is not None:
            values = state.selection(condition.value_set)
            if actual.kind == "Coding":
                identity = _coding(raw, state.operation)
            elif actual.kind == "code" and type(raw) is str:
                system = state.system(group.source, None, path)
                identity = (
                    FhirCodeIdentity(*system, raw) if system is not None else None
                )
            else:
                identity = None
            if values is None or identity is None:
                note(
                    state.issues,
                    "dependsOn ValueSet requires a fully identified code or Coding",
                    path=path,
                )
                applicable = None
            else:
                matched = state.resolve(identity, path)
                if matched is None:
                    applicable = None
                else:
                    reserve(state.operation, len(values))
                    if matched not in set(values) and applicable is not None:
                        applicable = False
            continue
        expected = condition.value
        if (
            expected is None
            or expected.kind != actual.kind
            or actual.kind == "Quantity"
        ):
            note(
                state.issues,
                "The selected typed condition needs unsupported comparison",
                path=path,
            )
            applicable = None
            continue
        wanted = decode_fhir_json(expected.value_json, state.operation)
        if actual.kind == "Coding":
            first, second = (
                _coding(wanted, state.operation),
                _coding(raw, state.operation),
            )
            if (
                first is None
                or second is None
                or (first.version is None or second.version is None)
                and not state.policy.allow_unversioned
            ):
                note(
                    state.issues,
                    "Coding condition requires exact system/version/code identities",
                    path=path,
                )
                applicable = None
                continue
            equal = first == second
        elif actual.kind == "code":
            system = state.system(group.source, None, path)
            if system is None or type(raw) is not str or type(wanted) is not str:
                applicable = None
                continue
            first, second = (
                FhirCodeIdentity(*system, wanted),
                FhirCodeIdentity(*system, raw),
            )
            a, b = state.resolve(first, path), state.resolve(second, path)
            if a is None or b is None:
                applicable = None
                continue
            selected = next(s for s in state.systems if (s.system, s.version) == system)
            if selected.case_sensitive is None and wanted != raw:
                note(
                    state.issues,
                    "Missing case policy cannot qualify differing code values",
                    path=path,
                )
                applicable = None
                continue
            equal = a == b
        else:
            equal = same_json(wanted, raw, state.operation)
        if not equal and applicable is not None:
            applicable = False
    return applicable


def translate_concept_map(
    concept_map: FhirConceptMap,
    source: FhirCodeIdentity,
    *,
    context: tuple[FhirMapContext, ...] = (),
    systems: tuple[FhirCodeSystem, ...],
    value_sets: tuple[FhirMapValueSet, ...] = (),
    policy: FhirValueSetPolicy | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: threading.Event | None = None,
) -> FhirTranslation:
    """Return all established directed matches; never choose or apply a migration.

    Missing context/expansion yields required unknown even alongside known
    targets. noMap and not-related-to suppress group fallback. Only explicitly
    local use-source-code/fixed fallback is executed; other-map stays unknown.
    Native FHIR/RDF semantics are not replaced by inferred reverse mappings.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    selected_policy = FhirValueSetPolicy() if policy is None else policy
    state = _Translation(operation, issues, (), (), selected_policy)
    matches: list[FhirMapMatch] = []
    negative: list[FhirMapMatch] = []
    no_maps: list[tuple[str | int, ...]] = []
    checked, outside = 0, False
    try:
        _identity(source, operation)
        if (
            type(concept_map) is not FhirConceptMap
            or type(systems) is not tuple
            or type(value_sets) is not tuple
            or type(selected_policy) is not FhirValueSetPolicy
            or type(selected_policy.allow_unversioned) is not bool
            or type(selected_policy.include_inactive) is not bool
        ):
            raise InputError(
                "Translation requires owned resources and explicit finite policies"
            )
        reserve(operation)
        captured = read_concept_map(
            concept_map.payload, budget=operation, timeout=timeout, cancel=cancel
        )
        state.adopt(captured.report)
        if not captured.report.verified:
            raise ResourceError("Original ConceptMap interpretation is not qualified")
        captured_systems = []
        for supplied_system in systems:
            if type(supplied_system) is not FhirCodeSystem:
                raise InputError("Supply ordinary owned CodeSystem records")
            reserve(operation)
            reacquired = read_code_system(
                supplied_system.payload,
                bindings=supplied_system.bindings,
                case_comparison=supplied_system.case_comparison,
                budget=operation,
            )
            captured_systems.append(reacquired)
            state.adopt(reacquired.report)
        state.systems = tuple(captured_systems)
        reserve(operation, len(value_sets))
        for supplied in value_sets:
            if (
                type(supplied) is not FhirMapValueSet
                or type(supplied.value_set) is not FhirValueSet
                or supplied.mode not in {"compose", "expansion"}
            ):
                raise InputError(
                    "ValueSets need owned source and a selected compilation mode"
                )
            if len(supplied.value_set.payload) > operation.limits.max_bytes:
                raise LimitExceeded(
                    "Supplied ValueSet exceeds current source allowance"
                )
            operation.consume("bytes", len(supplied.value_set.payload))
        state.value_sets = value_sets
        reserve(operation, len(captured.attributes))
        attributes = dict(captured.attributes)
        actual_context = _context(context, attributes, state)
        source_scope = state.scope(captured.source_scope)
        state.scope(captured.target_scope)
        matched_source = state.resolve(source, ())
        if (
            captured.source_scope is not None
            and source_scope is not None
            and matched_source is not None
        ):
            reserve(operation, len(source_scope))
            outside = matched_source not in set(source_scope)
        if not outside and matched_source is not None:
            selected_groups = 0
            for group in captured.groups:
                operation.consume("work")
                system = state.system(group.source, captured.source_scope, group.path)
                if system != (source.system, source.version):
                    continue
                selected_groups += 1
                target_system = (
                    canonical(group.target, operation)
                    if group.target is not None
                    else None
                )
                if target_system is None and captured.target_scope is not None:
                    target_system = state.system(
                        None, captured.target_scope, group.path
                    )
                group_matches, suppress = 0, False
                no_map_start = len(no_maps)
                for element in group.elements:
                    if element.value_set is not None:
                        values = state.selection(element.value_set)
                        if values is None:
                            continue
                        reserve(operation, len(values))
                        selected = matched_source in set(values)
                    else:
                        selected = element.code == source.code
                        # The CodeSystem controls source code comparison.
                        current = next(
                            s for s in state.systems if (s.system, s.version) == system
                        )
                        if current.case_sensitive is False and element.code is not None:
                            operation.consume(
                                "work", len(element.code) + len(source.code)
                            )
                            operation.consume(
                                "bytes", len(element.code) + len(source.code)
                            )
                            selected = (
                                element.code.isascii()
                                and source.code.isascii()
                                and element.code.lower() == source.code.lower()
                            )
                    if not selected:
                        if element.code is not None and current.case_sensitive is None:
                            note(
                                issues,
                                "Missing case policy cannot exclude a lexical mapping",
                                path=element.path,
                            )
                            suppress = True
                        continue
                    if element.no_map:
                        reserve(operation)
                        no_maps.append(element.path)
                        suppress = True
                    for target in element.targets:
                        applicability = _conditions(
                            target.conditions, actual_context, group, state, target.path
                        )
                        if applicability is not True:
                            if applicability is None:
                                suppress = True
                            continue
                        for product in target.products:
                            if product.value is not None and product.value.kind in {
                                "code",
                                "Coding",
                            }:
                                raw_value = decode_fhir_json(
                                    product.value.value_json, operation
                                )
                                identity = (
                                    _coding(raw_value, operation)
                                    if product.value.kind == "Coding"
                                    else FhirCodeIdentity(*target_system, raw_value)
                                    if target_system is not None
                                    and type(raw_value) is str
                                    else None
                                )
                                if identity is None:
                                    note(
                                        issues,
                                        "Product requires a fully identified "
                                        "code or Coding",
                                        path=target.path,
                                    )
                                else:
                                    state.resolve(identity, target.path)
                            if product.value_set is not None:
                                state.selection(product.value_set)
                                if attributes[product.attribute] not in {
                                    "code",
                                    "Coding",
                                }:
                                    note(
                                        issues,
                                        "Product ValueSet needs code or Coding",
                                        path=target.path,
                                    )
                        if target.relationship == "not-related-to":
                            suppress = True
                            if target_system is None and target.value_set is None:
                                reserve(operation)
                                negative.append(
                                    FhirMapMatch(
                                        source,
                                        None,
                                        target.relationship,
                                        target.conditions,
                                        target.products,
                                        target.path,
                                    )
                                )
                                continue
                        metadata = obj(
                            decode_fhir_json(target.metadata_json, operation)
                        )
                        if metadata.get("property"):
                            note(
                                issues,
                                "Mapping properties need unsupported interpretation",
                                path=target.path,
                            )
                            continue
                        targets = state.codes(
                            target.code,
                            target.value_set,
                            target_system,
                            captured.target_scope,
                            target.path,
                        )
                        if targets is None:
                            continue
                        for identity in targets:
                            reserve(operation)
                            if state.resolve(identity, target.path) is None:
                                continue
                            match = FhirMapMatch(
                                source,
                                identity,
                                target.relationship,
                                target.conditions,
                                target.products,
                                target.path,
                            )
                            if target.relationship == "not-related-to":
                                negative.append(match)
                            else:
                                matches.append(match)
                                group_matches += 1
                if group_matches and len(no_maps) > no_map_start:
                    note(
                        issues,
                        "Selected group declares both noMap and a positive target",
                        path=group.path,
                        finding=True,
                    )
                if group_matches == 0 and not suppress:
                    if group.unmapped_json is None:
                        note(
                            issues,
                            "No mapping or noMap establishes this translation",
                            path=group.path,
                        )
                        continue
                    rule = obj(decode_fhir_json(group.unmapped_json, operation))
                    mode = rule["mode"]
                    if mode == "other-map":
                        note(
                            issues,
                            "other-map fallback is outside this finite profile",
                            path=group.path,
                        )
                        continue
                    relationship = str(rule["relationship"])
                    code = (
                        source.code if mode == "use-source-code" else rule.get("code")
                    )
                    value_set = rule.get("valueSet")
                    targets = state.codes(
                        code if isinstance(code, str) else None,
                        value_set if isinstance(value_set, str) else None,
                        target_system,
                        captured.target_scope,
                        group.path,
                    )
                    if targets is not None:
                        for identity in targets:
                            reserve(operation)
                            if state.resolve(identity, group.path) is not None:
                                match = FhirMapMatch(
                                    source,
                                    identity,
                                    relationship,
                                    (),
                                    (),
                                    (*group.path, "unmapped"),
                                    True,
                                )
                                (
                                    negative
                                    if relationship == "not-related-to"
                                    else matches
                                ).append(match)
            if not selected_groups:
                note(
                    issues,
                    "No forward group supplies the exact requested system/version",
                )
            operation.consume("work", 0)
            checked = 1
    except (
        InputError,
        ValueError,
        TypeError,
        UnicodeError,
        ImportError,
        MemoryError,
        RecursionError,
        UnsupportedJsonNumber,
    ) as error:
        failure(error, issues)
    report = Report(
        "adapter.fhir_concept_map_translation",
        checked,
        tuple(issues),
        requested=0 if outside else 1,
        allow_empty=outside,
        complete=outside or checked == 1,
        children=tuple(state.reports),
        regime="fhir-r5-directed-finite-translation",
    )
    status: Literal[
        "translated", "negative", "no_map", "unresolved", "not_applicable"
    ] = "unresolved"
    if outside:
        status = "not_applicable"
    elif report.verified:
        status = (
            "translated"
            if matches
            else "no_map"
            if no_maps
            else "negative"
            if negative
            else "unresolved"
        )
    return FhirTranslation(
        source, tuple(matches), tuple(negative), tuple(no_maps), status, report
    )
