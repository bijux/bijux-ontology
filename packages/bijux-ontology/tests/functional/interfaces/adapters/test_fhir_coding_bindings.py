"""Actual R5 datatype bindings, repeated elements and independent source evidence."""

import importlib.util
import json
from dataclasses import dataclass, replace
from importlib import import_module
from typing import Any, Literal

import pytest
from rdflib import URIRef

from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.interfaces.adapters.fhir.code_system import (
    FhirCodeSystem,
    read_code_system,
)
from bijux_ontology.interfaces.adapters.fhir.coding_bindings import (
    FhirCodingBinding,
    FhirDisplayPolicy,
    FhirExtensibilityDecision,
    check_coding_bindings,
)
from bijux_ontology.interfaces.adapters.fhir.identity import (
    FhirCodeBinding,
    FhirCodeIdentity,
)
from bijux_ontology.interfaces.adapters.fhir.value_set import (
    FhirValueSet,
    read_value_set,
)

NATIVE = importlib.util.find_spec("fhir") is not None
native = pytest.mark.skipif(
    not NATIVE, reason="Actual optional FHIR R5 models required"
)


def _system(
    system: str = "urn:one", version: str = "1", codes: tuple[str, ...] = ("A", "B")
) -> FhirCodeSystem:
    """Supply small native systems and explicit independent RDF identities."""
    bindings = tuple(
        FhirCodeBinding(
            FhirCodeIdentity(system, version, code),
            URIRef("urn:bound:" + system[4:] + ":" + code),
        )
        for code in codes
    )
    raw = {
        "resourceType": "CodeSystem",
        "url": system,
        "version": version,
        "status": "active",
        "content": "complete",
        "caseSensitive": True,
        "concept": [
            {
                "code": code,
                "display": "Alpha" if code == "A" else "Beta",
                "designation": [
                    {"language": "fr", "value": "Alphabète"},
                    {"language": "fr", "value": "Premier"},
                ],
            }
            for code in codes
        ],
    }
    result = read_code_system(json.dumps(raw).encode(), bindings=bindings)
    assert result.report.verified, result.report.to_dict()
    return result


def _value_set(
    entries: tuple[tuple[str, str | None, str], ...] = (("urn:one", "1", "A"),),
    *,
    version: str | None = "1",
    paged: bool = False,
) -> FhirValueSet:
    """Reuse actual178 expansion rules rather than a claimed complete enumeration."""
    raw: dict[str, Any] = {
        "resourceType": "ValueSet",
        "url": "urn:allowed",
        "status": "active",
        "expansion": {
            "timestamp": "2026-01-01T00:00:00Z",
            "total": len(entries),
            "contains": [
                {"system": s, "code": c, **({"version": v} if v else {})}
                for s, v, c in entries
            ],
        },
    }
    if version is not None:
        raw["version"] = version
    if paged:
        raw["expansion"]["offset"] = 0
    result = read_value_set(json.dumps(raw).encode())
    assert result.report.verified, result.report.to_dict()
    return result


def _binding(
    datatype: Literal["code", "Coding", "CodeableConcept"] = "CodeableConcept",
    **changes: Any,
) -> FhirCodingBinding:
    """Use explicit field occurrence and external ValueSet version contracts."""
    return FhirCodingBinding(
        ("value",),
        datatype,
        "required",
        _value_set(),
        "urn:allowed|1",
        minimum_occurrences=1,
        **changes,
    )


def _coding(
    system: str = "urn:one", version: str = "1", code: str = "A", **fields: Any
) -> Any:
    """Produce actual maintained native Coding instances."""
    return import_module("fhir.resources.coding").Coding(
        system=system, version=version, code=code, **fields
    )


def _concept(*codings: Any, text: str | None = None) -> Any:
    """Retain all original translation codings in an actual native concept."""
    return import_module("fhir.resources.codeableconcept").CodeableConcept(
        coding=list(codings) or None, text=text
    )


@native
def test_required_concept_accepts_one_qualifying_coding_and_legitimate_extras() -> None:
    """At least one coding qualifies; other system translations need not be in-set."""
    systems = (_system(), _system("urn:other", "2", ("A", "X")))
    additional = _coding("urn:other", "2", "X")
    value = _concept(_coding(), additional)
    result = check_coding_bindings({"value": value}, (_binding(),), systems=systems)
    assert result.report.verified, result.report.to_dict()
    occurrence = result.occurrences[0]
    assert occurrence.conforms and len(occurrence.codings) == 2
    assert all(c.identity_report.verified for c in occurrence.codings)
    assert occurrence.codings[1].identity == FhirCodeIdentity("urn:other", "2", "X")
    wrong = check_coding_bindings(
        {"value": _concept(additional)}, (_binding(),), systems=systems
    )
    assert wrong.occurrences[0].conforms is False
    assert any(i.code == "adapter.fhir_coding_required_binding" for i in wrong.report)
    text_only = check_coding_bindings(
        {"value": _concept(text="Alpha")}, (_binding(),), systems=systems
    )
    assert text_only.occurrences[0].conforms is False


@native
def test_every_parent_and_terminal_repetition_retains_concrete_paths() -> None:
    """One valid repetition cannot cover another invalid field element."""
    systems = (_system(),)
    contract = replace(_binding(), path=("components", "code"))
    record = {
        "components": [
            {"code": [_concept(_coding())]},
            {"code": [_concept(_coding(code="B")), _concept(_coding())]},
        ]
    }
    result = check_coding_bindings(record, (contract,), systems=systems)
    assert len(result.occurrences) == 3
    assert [o.path for o in result.occurrences] == [
        ("components", 0, "code", 0),
        ("components", 1, "code", 0),
        ("components", 1, "code", 1),
    ]
    assert [o.conforms for o in result.occurrences] == [True, False, True]
    assert any(
        i.details and i.details.path == ("components", 1, "code", 0)
        for i in result.report
    )

    @dataclass
    class Record:
        """An ordinary containing model without a serializer prerequisite."""

        value: object

    assert check_coding_bindings(
        Record(_concept(_coding())), (_binding(),), systems=systems
    ).report.verified
    optional = replace(_binding(), minimum_occurrences=0)
    assert check_coding_bindings({}, (optional,), systems=systems).report.verified
    assert not check_coding_bindings({}, (_binding(),), systems=systems).report.verified


@native
def test_preferred_is_recommendation_and_extensible_uses_bound_caller_decisions() -> (
    None
):
    """No clinical meaning is inferred from an out-of-set but valid code."""
    systems = (_system(),)
    value = _concept(_coding(code="B"))
    preferred = replace(_binding(), strength="preferred")
    result = check_coding_bindings({"value": value}, (preferred,), systems=systems)
    assert result.report.verified and result.occurrences[0].conforms
    assert any(
        i.code == "adapter.fhir_coding_preferred_recommendation"
        and i.severity == "warning"
        for i in result.recommendations
    )
    assert not any(
        i.code == "adapter.fhir_coding_required_binding" for i in result.report
    )
    example = replace(preferred, strength="example")
    assert check_coding_bindings(
        {"value": value}, (example,), systems=systems
    ).report.verified
    extensible = replace(preferred, strength="extensible")
    unresolved = check_coding_bindings({"value": value}, (extensible,), systems=systems)
    assert unresolved.occurrences[0].conforms is None and not unresolved.report.verified
    decision = FhirExtensibilityDecision(
        ("value",),
        "urn:allowed|1",
        value.model_dump_json(),
        False,
        "Human review: the selected set does not represent this concept",
    )
    accepted = check_coding_bindings(
        {"value": value}, (extensible,), systems=systems, decisions=(decision,)
    )
    assert accepted.report.verified
    applicable = check_coding_bindings(
        {"value": value},
        (extensible,),
        systems=systems,
        decisions=(replace(decision, representable=True),),
    )
    assert applicable.occurrences[0].conforms is False
    changed = check_coding_bindings(
        {"value": _concept(text="different")},
        (extensible,),
        systems=systems,
        decisions=(decision,),
    )
    assert not changed.report.verified and changed.occurrences[0].conforms is None


@native
def test_required_bare_code_rejects_ambiguous_strings_without_restricting_coding() -> (
    None
):
    """Only the bare profile loses system identity and needs independent uniqueness."""
    systems = (_system(), _system("urn:other", "2", ("A",)))
    values = _value_set((("urn:one", "1", "A"), ("urn:other", "2", "A")))
    bare = replace(_binding("code"), value_set=values)
    ambiguous = check_coding_bindings({"value": "A"}, (bare,), systems=systems)
    assert not ambiguous.report.verified
    assert any(i.code == "adapter.fhir_coding_bare_binding" for i in ambiguous.report)
    explicit = replace(bare, datatype="Coding")
    assert check_coding_bindings(
        {"value": _coding()}, (explicit,), systems=systems
    ).report.verified
    unversioned = replace(
        _binding("code"),
        value_set=_value_set(version=None),
        value_set_reference="urn:allowed",
    )
    result = check_coding_bindings({"value": "A"}, (unversioned,), systems=systems)
    assert any(i.code == "adapter.fhir_coding_bare_binding" for i in result.report)
    assert check_coding_bindings(
        {"value": _coding()},
        (replace(unversioned, datatype="Coding"),),
        systems=systems,
    ).report.verified


@native
def test_missing_versions_and_incomplete_expansion_cannot_prove_exclusion() -> None:
    """A missing version is not silently selected from the supplied inventory."""
    systems = (_system(),)
    missing = import_module("fhir.resources.coding").Coding(system="urn:one", code="A")
    result = check_coding_bindings(
        {"value": missing}, (_binding("Coding"),), systems=systems
    )
    assert result.occurrences[0].conforms is None and not result.report.verified
    paged = replace(_binding("Coding"), value_set=_value_set(paged=True))
    result = check_coding_bindings(
        {"value": _coding(code="B")}, (paged,), systems=systems
    )
    assert result.occurrences[0].conforms is None
    assert not any(
        i.code == "adapter.fhir_coding_required_binding" for i in result.report
    )
    ambiguous = _system(codes=("A", "a"))
    raw = json.loads(ambiguous.payload)
    raw.pop("caseSensitive")
    ambiguous = read_code_system(json.dumps(raw).encode(), bindings=ambiguous.bindings)
    result = check_coding_bindings(
        {"value": _coding(code="a")}, (_binding("Coding"),), systems=(ambiguous,)
    )
    assert result.occurrences[0].conforms is None


@native
def test_display_is_a_separate_codesystem_language_selection_with_ties() -> None:
    """Binding and selected display are separate; language ties stay visible."""
    systems = (_system(),)
    policy = FhirDisplayPolicy("language", ("fr-CA",), ("untagged",), True)
    contract = replace(_binding("Coding"), display=policy)
    result = check_coding_bindings(
        {"value": _coding(display="Alphabète")}, (contract,), systems=systems
    )
    assert result.report.verified, result.report.to_dict()
    evidence = result.occurrences[0].codings[0]
    assert evidence.display and len(evidence.display.items) == 2
    assert (
        evidence.membership
        and evidence.membership.matched_concept
        and evidence.membership.matched_concept.designations
    )
    wrong = check_coding_bindings(
        {"value": _coding(display="ValueSet-only label")}, (contract,), systems=systems
    )
    assert (
        wrong.occurrences[0].conforms and wrong.occurrences[0].binding_report.verified
    )
    assert not wrong.report.verified and any(
        i.code == "adapter.fhir_coding_display_mismatch" for i in wrong.report
    )


@native
def test_native_structure_mutation_and_shared_remaining_budget_are_honest() -> None:
    """Native model_construct is revalidated; later repeats still execute."""
    bad = import_module("fhir.resources.coding").Coding.model_construct(
        system="urn:one", version="1", code=True
    )
    result = check_coding_bindings(
        {"value": [bad, _coding()]}, (_binding("Coding"),), systems=(_system(),)
    )
    assert not result.report.verified and result.occurrences[-1].conforms
    assert any(i.code == "adapter.fhir_coding_structure" for i in result.report)
    empty = import_module("fhir.resources.codeableconcept").CodeableConcept()
    invalid_preferred = check_coding_bindings(
        {"value": empty},
        (replace(_binding(), strength="preferred"),),
        systems=(_system(),),
    )
    assert not invalid_preferred.report.verified
    assert any(
        i.code == "adapter.fhir_coding_structure" for i in invalid_preferred.report
    )
    budget = Budget(Limits(max_states=500))
    budget.consume("states", 499)
    stopped = check_coding_bindings(
        {"value": _coding()}, (_binding("Coding"),), systems=(_system(),), budget=budget
    )
    assert not stopped.report.verified and any(
        i.kind == "exhausted" for i in stopped.report
    )


def test_core_optional_absence_is_actionable() -> None:
    """Ordinary adapter import needs no optional native models."""
    if NATIVE:
        pytest.skip("Requires genuine core installation")
    # A valid owned ValueSet is unavailable without its optional interpreter;
    # import/type surfaces remain usable and no binding claims success.
    source = read_value_set(b'{"resourceType":"ValueSet","status":"active"}')
    contract = FhirCodingBinding(
        ("value",), "Coding", "required", source, "urn:allowed|1"
    )
    result = check_coding_bindings(
        {"value": {"system": "urn:one", "code": "A"}}, (contract,), systems=()
    )
    assert not result.report.verified and any(
        "[fhir]" in i.message for i in result.report
    )


@native
def test_composed_consumers_prepare_each_system_once_without_merging_codings() -> None:
    """Real native consumers share preparation within each operation only."""
    import sys
    from types import FrameType

    from bijux_ontology.interfaces.adapters.fhir.concept_map import (
        FhirMapValueSet,
        read_concept_map,
        translate_concept_map,
    )
    from bijux_ontology.interfaces.adapters.fhir.value_set import compile_value_set

    system = _system()
    value_set = _value_set()
    contract = replace(_binding("Coding"), value_set=value_set)
    contracts = (
        replace(contract, path=("first",)),
        replace(contract, path=("second",)),
    )
    record = {"first": [_coding(), _coding()], "second": _coding()}
    raw = {
        "resourceType": "ConceptMap",
        "status": "active",
        "sourceScopeCanonical": "urn:allowed|1",
        "targetScopeCanonical": "urn:allowed|1",
        "group": [
            {
                "source": "urn:one|1",
                "target": "urn:one|1",
                "element": [
                    {
                        "code": "A",
                        "target": [{"code": "A", "relationship": "equivalent"}],
                    }
                ],
            }
        ],
    }
    producer = import_module("fhir.resources.conceptmap").ConceptMap.model_validate(raw)
    mapping = read_concept_map(producer.model_dump_json(exclude_none=True).encode())
    assert mapping.report.verified
    reads = {"coding": 0, "selection": 0, "translation": 0}
    selected = "coding"

    def observe(frame: FrameType, event: str, argument: object) -> None:
        if (
            event == "call"
            and frame.f_code.co_name == "read_code_system"
            and frame.f_globals.get("__name__")
            == "bijux_ontology.interfaces.adapters.fhir.code_system.reading"
        ):
            reads[selected] += 1

    previous = sys.getprofile()
    sys.setprofile(observe)
    try:
        checked = check_coding_bindings(record, contracts, systems=(system,))
        selected = "selection"
        allowed = compile_value_set(
            value_set, mode="expansion", systems=(system,), bindings=system.bindings
        )
        selected = "translation"
        translated = translate_concept_map(
            mapping,
            FhirCodeIdentity("urn:one", "1", "A"),
            systems=(system,),
            value_sets=(FhirMapValueSet(value_set, "expansion"),),
        )
    finally:
        sys.setprofile(previous)
    assert reads == {"coding": 1, "selection": 1, "translation": 1}
    assert checked.report.verified and len(checked.occurrences) == 3
    assert [item.path for item in checked.occurrences] == [
        ("first", 0),
        ("first", 1),
        ("second",),
    ]
    identity = FhirCodeIdentity("urn:one", "1", "A")
    assert allowed.report.verified and allowed.identities == (identity,)
    assert translated.successful
    assert [item.target for item in translated.matches] == [identity]
