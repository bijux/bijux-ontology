"""Finite local selection through qualified CodeSystems and existing set algebra."""

from dataclasses import dataclass, field
from typing import Literal

from rdflib import URIRef

from bijux_ontology._json import array_value
from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.execution.inputs import capture
from bijux_ontology.interfaces.adapters.vocabulary_bindings.controlled.algebra import (
    compile_allowed,
)
from bijux_ontology.interfaces.adapters.vocabulary_bindings.controlled.types import (
    AllowedDifference,
    AllowedUnion,
)
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.reports import Issue, Report

from ..code_system.checking import check_code_membership
from ..code_system.reading import _operation as operation
from ..code_system.reading import read_code_system
from ..code_system.types import FhirCodeSystem
from ..identity import FhirCodeBinding, FhirCodeIdentity, capture_code_bindings
from ..json import UnsupportedJsonNumber, decode_fhir_json
from .reading import _clauses, _entries, _object, _source_kinds, _text
from .types import (
    FhirExpansionEntry,
    FhirSelectionClause,
    FhirValueSet,
    FhirValueSetPolicy,
    FhirValueSetSelection,
)


@dataclass
class _Selection:
    """One operation's bounded positive/negative evidence and raw child reports."""

    budget: Budget
    issues: IssueCollector
    bindings: dict[FhirCodeIdentity, URIRef]
    systems: tuple[FhirCodeSystem, ...]
    policy: FhirValueSetPolicy
    reports: list[Report] = field(default_factory=list)

    def adopt(self, report: Report) -> None:
        """Attach a required raw report only after current-budget adoption succeeds."""
        try:
            retain_report(report, self.budget, self.issues)
        except LimitExceeded:
            self.issues.retain_completed(report)
            raise
        self.reports.append(report)

    def unknown(self, message: str) -> None:
        """Retain an actual unresolved obligation without inventing negative facts."""
        self.issues.append(
            Issue("adapter.fhir_valueset_unverified", message, kind="unverified")
        )

    def resolve(
        self,
        identity: FhirCodeIdentity,
        *,
        abstract: bool = False,
        inactive: bool | None = None,
    ) -> tuple[URIRef | None, bool, bool]:
        """Return mapped value, selectable status and resolved evidence boundary."""
        self.budget.consume("work")
        if identity.version is None and not self.policy.allow_unversioned:
            self.unknown(
                "The selected version contract requires explicit business versions"
            )
            return None, False, False
        self.budget.consume("work", len(self.systems))
        systems = [
            system
            for system in self.systems
            if system.system == identity.system and system.version == identity.version
        ]
        if len(systems) != 1:
            self.unknown(
                "Exactly one qualified local "
                "system/version must supply each selected code"
            )
            return None, False, False
        result = check_code_membership(systems[0], identity, budget=self.budget)
        self.adopt(result.report)
        if result.status == "unknown":
            return None, False, False
        if result.status == "absent":
            self.issues.append(
                Issue(
                    "adapter.fhir_valueset_code_invalid",
                    "An explicit ValueSet code is absent "
                    "from its supplied complete CodeSystem",
                )
            )
            return None, False, True
        concept = result.matched_concept
        if concept is None or concept.identity not in self.bindings:
            self.unknown(
                "Every selected original code requires its exact reversible RDF binding"
            )
            return None, False, False
        iri = self.bindings[concept.identity]
        if iri != concept.iri:
            raise InputError("CodeSystem and ValueSet RDF bindings disagree")
        meanings = {
            definition.code: definition.uri
            for definition in systems[0].property_definitions
        }
        nonselectable = abstract
        is_inactive = inactive is True
        for prop in concept.properties:
            self.budget.consume("work")
            meaning = meanings.get(prop.code)
            if meaning in {
                "http://hl7.org/fhir/concept-properties#notSelectable",
                "http://hl7.org/fhir/concept-properties#inactive",
            }:
                if prop.kind != "boolean":
                    self.unknown(
                        "Selectable/inactive property "
                        "meanings require qualified boolean evidence"
                    )
                    return iri, False, False
                if meaning.endswith("#notSelectable"):
                    nonselectable |= prop.value is True
                else:
                    is_inactive |= prop.value is True
        return (
            iri,
            not nonselectable and (self.policy.include_inactive or not is_inactive),
            True,
        )


def _clause(clause: FhirSelectionClause, state: _Selection) -> ValueEnumeration:
    """Compile one finite local leaf; imported/filter operands remain open."""
    values: list[URIRef] = []
    denied: list[URIRef] = []
    complete = True
    if clause.version is None and not state.policy.allow_unversioned:
        state.unknown(
            "The selected version contract requires an explicit clause version"
        )
        return ValueEnumeration((), closed=False)
    if (
        clause.filtered
        or clause.imported
        or clause.system is None
        or clause.version == "*"
    ):
        state.unknown(
            "Filters, imported ValueSets and "
            "wildcard versions require unsupported resolution"
        )
        return ValueEnumeration((), closed=False)
    codes = clause.codes
    if codes is None:
        supplied = [
            system
            for system in state.systems
            if system.system == clause.system and system.version == clause.version
        ]
        if len(supplied) != 1:
            state.unknown(
                "Whole-system selection requires one exact supplied system/version"
            )
            return ValueEnumeration((), closed=False)
        system = supplied[0]
        state.adopt(system.report)
        complete = system.report.verified and system.content == "complete"
        if not complete:
            state.unknown(
                "Open or supplement content cannot "
                "supply complete whole-system selection"
            )
        codes = tuple(concept.identity.code for concept in system.concepts)
    for code in codes:
        state.budget.consume("states")
        iri, selectable, resolved = state.resolve(
            FhirCodeIdentity(clause.system, clause.version, code)
        )
        complete &= resolved
        if iri is not None and resolved:
            (values if selectable else denied).append(iri)
    return ValueEnumeration(
        tuple(dict.fromkeys(values)),
        excluded=tuple(dict.fromkeys(denied)),
        closed=complete,
    )


def _expansion(raw: dict[str, object], state: _Selection) -> ValueEnumeration:
    """Qualify an unambiguous finite supplied expansion, retaining UI occurrences."""
    expansion = _object(raw.get("expansion"))
    entries = _entries(expansion, state.budget)
    meanings = {}
    for item in array_value(expansion.get("property", [])):
        record = _object(item)
        code = _text(record.get("code"))
        if code in meanings:
            raise InputError("Expansion property codes must have unambiguous meanings")
        meanings[code] = record.get("uri")
    complete = "offset" not in expansion and "next" not in expansion
    if not complete:
        state.unknown("Paged expansions never establish a complete local enumeration")
    for parameter in array_value(expansion.get("parameter", [])):
        record = _object(parameter)
        name = _text(record.get("name"))
        if name not in {
            "includeDesignations",
            "displayLanguage",
        }:
            complete = False
            state.unknown(
                "Expansion selection parameters outside the "
                "presentation/version subset remain unqualified"
            )
    seen: set[FhirCodeIdentity] = set()
    values: dict[URIRef, bool | None] = {}
    pending: list[FhirExpansionEntry] = list(reversed(entries))
    unambiguous_count = True
    while pending:
        state.budget.consume("work")
        state.budget.consume("states")
        entry = pending.pop()
        pending.extend(reversed(entry.children))
        if entry.identity is None:
            continue
        if entry.identity in seen or entry.abstract:
            unambiguous_count = False
        seen.add(entry.identity)
        abstract, inactive = entry.abstract, entry.inactive
        metadata = _object(decode_fhir_json(entry.metadata_json, state.budget))
        for item in array_value(metadata.get("property", [])):
            record = _object(item)
            meaning = meanings.get(_text(record.get("code")))
            if meaning in {
                "http://hl7.org/fhir/concept-properties#notSelectable",
                "http://hl7.org/fhir/concept-properties#inactive",
            }:
                if type(record.get("valueBoolean")) is not bool:
                    complete = False
                    state.unknown("Expansion selection flags require boolean evidence")
                    abstract = True
                elif meaning == "http://hl7.org/fhir/concept-properties#notSelectable":
                    abstract |= record["valueBoolean"] is True
                else:
                    inactive = inactive is True or record["valueBoolean"] is True
        if abstract:
            unambiguous_count = False
        iri, selectable, resolved = state.resolve(
            entry.identity, abstract=abstract, inactive=inactive
        )
        complete &= resolved
        if iri is not None:
            if iri in values:
                unambiguous_count = False
                if values[iri] is not selectable:
                    values[iri] = None
                    state.unknown(
                        "Repeated occurrences disagree about selectable status"
                    )
            else:
                values[iri] = selectable if resolved else None
    if (
        not unambiguous_count
        or type(expansion.get("total")) is not int
        or expansion["total"] != len(seen)
    ):
        complete = False
        state.unknown(
            "Expansion closure requires an unambiguous "
            "supplied ordinary-code count matching total"
        )
    return ValueEnumeration(
        tuple(iri for iri, selectable in values.items() if selectable is True),
        excluded=tuple(
            iri for iri, selectable in values.items() if selectable is False
        ),
        closed=complete,
    )


def compile_value_set(
    value_set: FhirValueSet,
    *,
    mode: Literal["compose", "expansion"],
    systems: tuple[FhirCodeSystem, ...],
    bindings: tuple[FhirCodeBinding, ...],
    policy: FhirValueSetPolicy | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> FhirValueSetSelection:
    """Compile an explicitly chosen source into ordinary checked allowed values.

    Composition reuses the finite three-valued set algebra. Expansion closure is
    limited to an unambiguous ordinary coded inventory and declared total, with
    no paging or unsupported restricting parameters. Both are scoped to supplied
    exact systems/versions; no terminology endpoint or implicit page is fetched.
    """
    owned_budget = operation(limits, budget)
    issues = IssueCollector(owned_budget)
    selected_policy = policy if policy is not None else FhirValueSetPolicy()
    state = _Selection(owned_budget, issues, {}, (), selected_policy)
    enumeration = ValueEnumeration((), closed=False)
    checked = 0
    try:
        if not isinstance(value_set, FhirValueSet) or mode not in {
            "compose",
            "expansion",
        }:
            raise InputError(
                "Select an owned ValueSet and explicit compose/expansion mode"
            )
        if (
            type(selected_policy) is not FhirValueSetPolicy
            or type(selected_policy.allow_unversioned) is not bool
            or type(selected_policy.include_inactive) is not bool
        ):
            raise InputError("ValueSet policies require explicit boolean choices")
        state.adopt(value_set.report)
        if not value_set.report.verified:
            state.unknown("The original ValueSet interpretation is not qualified")
        else:
            owned_bindings = capture_code_bindings(bindings, owned_budget)
            state.bindings = {
                binding.identity: binding.iri for binding in owned_bindings
            }
            supplied = capture(systems, owned_budget)
            if any(not isinstance(system, FhirCodeSystem) for system in supplied):
                raise InputError("Local systems require owned CodeSystem records")
            # Reacquire actual original metadata before any key comparison or
            # semantic use; public report metadata is bounded before attachment.
            captured_systems = []
            for system in supplied:
                state.adopt(system.report)
                captured = read_code_system(
                    system.payload,
                    bindings=system.bindings,
                    case_comparison=system.case_comparison,
                    budget=owned_budget,
                )
                state.adopt(captured.report)
                captured_systems.append(captured)
            state.systems = tuple(captured_systems)
            if len(value_set.payload) > owned_budget.limits.max_bytes:
                raise LimitExceeded("ValueSet exceeds the current source allowance")
            owned_budget.consume("bytes", 4 * len(value_set.payload))
            owned_budget.consume("work", 3 * len(value_set.payload))
            raw = _object(decode_fhir_json(value_set.payload, owned_budget))
            _source_kinds(raw, owned_budget)
            if mode == "expansion":
                enumeration = _expansion(raw, state)
            else:
                compose = _object(raw.get("compose"))
                if compose.get("inactive") is False:
                    state.policy = FhirValueSetPolicy(
                        selected_policy.allow_unversioned, False
                    )
                if "lockedDate" in compose:
                    state.unknown(
                        "Locked-date version resolution is "
                        "outside the finite supplied subset"
                    )
                includes = _clauses(compose, "include", owned_budget)
                excludes = _clauses(compose, "exclude", owned_budget)
                if not includes:
                    raise InputError("R5 composition requires include clauses")
                inclusion = AllowedUnion(
                    tuple(_clause(clause, state) for clause in includes)
                )
                exclusion = (
                    AllowedUnion(tuple(_clause(clause, state) for clause in excludes))
                    if excludes
                    else ValueEnumeration(())
                )
                enumeration = compile_allowed(
                    AllowedDifference(inclusion, exclusion), budget=owned_budget
                )
                state.adopt(
                    enumeration.report
                ) if enumeration.report is not None else None
            owned_budget.consume("work", 0)
            owned_budget.consume("work", 2 * len(state.bindings))
            owned_budget.consume("states", 4 * len(state.bindings))
            owned_budget.consume("bytes", 128 * len(state.bindings))
            checked = 1
    except UnsupportedJsonNumber as error:
        try:
            state.unknown(str(error))
        except LimitExceeded:
            issues.exhausted("Stopped retaining unsupported numeric source context")
    except (LimitExceeded, MemoryError, RecursionError) as error:
        issues.exhausted(str(error))
    except (InputError, ValueError, TypeError, KeyError):
        try:
            issues.append(
                Issue(
                    "adapter.fhir_valueset_input_error",
                    "ValueSet selection requires coherent "
                    "finite local source and identity contracts",
                    kind="input_error",
                )
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining ValueSet selection failure")
    report = Report(
        "adapter.fhir_value_set_selection",
        checked,
        tuple(issues),
        requested=1,
        complete=checked == 1
        and not any(issue.kind in {"unverified", "exhausted"} for issue in issues),
        children=tuple(state.reports),
        regime="fhir-r5-explicit-local-selection",
    )
    # Required raw reports qualify the compiled leaf rather than disappearing
    # when its ordinary enumeration is handed to application/RDF output checks.
    enumeration = ValueEnumeration(
        enumeration.values,
        excluded=enumeration.excluded,
        closed=enumeration.closed and report.verified,
        report=report,
    )
    permitted = set(enumeration.values)
    denied = set(enumeration.excluded)
    identities = tuple(
        identity for identity, iri in state.bindings.items() if iri in permitted
    )
    excluded = tuple(
        identity for identity, iri in state.bindings.items() if iri in denied
    )
    return FhirValueSetSelection(identities, excluded, enumeration, report)
