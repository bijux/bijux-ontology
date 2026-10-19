"""Actual optional DL datatype reasoning and original consumer branch coverage."""

from __future__ import annotations

import os
from pathlib import Path
from threading import Event
from typing import Literal as TypingLiteral
from typing import cast

import pytest
from bijux_ontology_reasoning.axioms import (
    DatatypeFacet,
    DatatypeRestriction,
    NamedEntity,
    NativeLiteral,
)
from bijux_ontology_reasoning.backend import OwlProfileBackend, prepare_profile_backend
from bijux_ontology_reasoning.datatypes import (
    DatatypeFieldBranch,
    DatatypeObligation,
    check_datatype_branches,
    check_datatype_fields,
    check_datatype_obligations,
)
from bijux_ontology_reasoning.documents import OwlDocument, prepare_owl_document
from rdflib import Literal
from rdflib.namespace import XSD

from bijux_ontology.declarations.types import FieldDeclaration, ValueDeclaration
from bijux_ontology.declarations.value_constraints import ValueConstraints
from bijux_ontology.limits import Budget, Limits

SMALL, WIDE, FAR = (
    NamedEntity("datatype", "urn:" + name) for name in ("small", "wide", "far")
)
_AXIOMS = """
Declaration(Datatype(<urn:small>))
DatatypeDefinition(<urn:small> DatatypeRestriction(xsd:integer
 xsd:minInclusive "1"^^xsd:integer
 xsd:maxInclusive "5"^^xsd:integer))
Declaration(Datatype(<urn:wide>))
DatatypeDefinition(<urn:wide> DatatypeRestriction(xsd:integer
 xsd:minInclusive "0"^^xsd:integer
 xsd:maxInclusive "10"^^xsd:integer))
Declaration(Datatype(<urn:far>))
DatatypeDefinition(<urn:far> DatatypeRestriction(xsd:integer
 xsd:minInclusive "20"^^xsd:integer))
Declaration(DataProperty(<urn:constrained>))
DataPropertyRange(<urn:constrained> <urn:small>)
Declaration(DataProperty(<urn:parent>)) DataPropertyRange(<urn:parent> <urn:wide>)
Declaration(DataProperty(<urn:child>)) SubDataPropertyOf(<urn:child> <urn:parent>)
Declaration(DataProperty(<urn:alias>)) EquivalentDataProperties(<urn:child> <urn:alias>)
DataPropertyRange(<urn:alias> <urn:small>)
Declaration(DataProperty(<urn:missing-range>))
"""


def _source(extra: str = "", *, imports: str = "") -> OwlDocument:
    """Use the actual maintained parser with unchanged supplied Functional Syntax."""
    return prepare_owl_document(
        "Prefix(xsd:=<http://www.w3.org/2001/XMLSchema#>) "
        "Ontology(<urn:datatype-theory> " + imports + _AXIOMS + extra + ")",
        format="functional",
        document_iri="urn:datatype-document",
    )


@pytest.fixture
def backend(tmp_path: Path) -> OwlProfileBackend:
    """Require a caller-installed pinned native distribution, without discovery."""
    java, bundle, pin = (
        os.environ.get(name)
        for name in ("BIJUX_OWL_JAVA", "BIJUX_OWL_BUNDLE", "BIJUX_OWL_MANIFEST_SHA256")
    )
    if not (java and bundle and pin):
        pytest.skip("Explicit pinned local DL backend is not configured")
    return prepare_profile_backend(
        Path(java), Path(bundle), manifest_sha256=pin, artifacts=tmp_path / "jobs"
    )


def test_custom_ranges_use_real_fresh_property_not_existing_constraints(
    backend: OwlProfileBackend,
) -> None:
    collision = (
        "Declaration(DataProperty(<urn:bijux:ontology:datatype:witness:0>)) "
        "DataPropertyRange(<urn:bijux:ontology:datatype:witness:0> <urn:small>)"
    )
    requests = (
        DatatypeObligation(SMALL, WIDE),
        DatatypeObligation(WIDE, SMALL),
        DatatypeObligation(SMALL, FAR, "disjointness"),
        DatatypeObligation(SMALL, WIDE, "disjointness"),
    )
    result = check_datatype_obligations(backend, _source(collision), requests)
    assert result.consistent is True and result.temporary_consistent is True
    assert (
        result.fresh_absent is True
        and result.fresh_property == "urn:bijux:ontology:datatype:witness:1"
    )
    assert result.report.complete and not result.report.verified
    assert tuple(a.request for a in result.answers) == requests
    assert tuple(a.outcome for a in result.answers) == (
        "included",
        "not_included",
        "disjoint",
        "not_disjoint",
    )
    assert result.answers[1].raw_difference_satisfiable is True
    assert result.answers[2].raw_intersection_satisfiable is False
    assert result.answers[0].report.verified and result.answers[2].report.verified


def test_actual_global_inherited_and_equivalent_ranges_cover_every_branch(
    backend: OwlProfileBackend,
) -> None:
    sample = Literal("3", datatype=XSD.integer, normalize=False)
    field = FieldDeclaration(
        "count",
        "urn:child",
        (
            ValueDeclaration("literal", datatype="urn:small"),
            ValueDeclaration("literal", datatype="urn:far"),
            ValueDeclaration("literal", datatype="urn:wide", samples=(sample,)),
        ),
        location="Record.count",
    )
    result = check_datatype_fields(backend, _source(), (field,))
    assert result.report.complete and not result.report.verified
    assert tuple(a.outcome for a in result.answers) == (
        "included",
        "disjoint",
        "overlap",
    )
    assert all(
        isinstance(a.request, DatatypeFieldBranch) and a.request.field is field
        for a in result.answers
    )
    assert {a.property.iri for a in result.answers[0].ranges} == {
        "urn:parent",
        "urn:alias",
    }
    assert result.answers[2].raw_difference_satisfiable is True
    assert result.answers[2].request == DatatypeFieldBranch(field, 2)
    assert field.values[2].samples == (sample,)
    assert {i.location for i in result.report.issues if i.kind == "finding"} == {
        "Record.count"
    }


def test_native_unsupported_facets_and_datatypes_remain_unknown(
    backend: OwlProfileBackend,
) -> None:
    unsupported = DatatypeRestriction(
        NamedEntity("datatype", str(XSD.integer)),
        (
            DatatypeFacet(
                str(XSD.totalDigits), NativeLiteral("2", str(XSD.integer), None)
            ),
        ),
    )
    result = check_datatype_obligations(
        backend,
        _source(),
        (
            DatatypeObligation(unsupported, WIDE),
            DatatypeObligation(NamedEntity("datatype", "urn:undefined"), WIDE),
            DatatypeObligation(SMALL, WIDE),
        ),
    )
    assert not result.report.complete and not result.report.verified
    assert tuple(a.outcome for a in result.answers) == (
        "unknown",
        "unknown",
        "included",
    )
    assert result.answers[0].native_error == "UnsupportedFacetException"
    assert result.answers[1].native_error == "UnsupportedDatatypeException"
    assert result.answers[2].report.verified


def test_missing_ranges_unsupported_branches_and_constraints_are_not_omitted(
    backend: OwlProfileBackend,
) -> None:
    fields = (
        FieldDeclaration(
            "missing",
            "urn:missing-range",
            (ValueDeclaration("literal", datatype="urn:small"),),
        ),
        FieldDeclaration(
            "undeclared",
            "urn:undeclared",
            (ValueDeclaration("literal", datatype="urn:small"),),
        ),
        FieldDeclaration(
            "branch",
            "urn:child",
            (
                ValueDeclaration("literal", datatype="urn:small"),
                ValueDeclaration("iri"),
            ),
        ),
        FieldDeclaration(
            "constrained",
            "urn:child",
            (
                ValueDeclaration(
                    "literal",
                    datatype="urn:wide",
                    constraints=ValueConstraints(min_length=1),
                ),
            ),
        ),
        FieldDeclaration(
            "collection",
            "urn:child",
            (
                ValueDeclaration("literal", datatype="urn:small", container="@set"),
                ValueDeclaration("literal", datatype="urn:small", container="@list"),
            ),
        ),
    )
    result = check_datatype_fields(backend, _source(), fields)
    assert len(result.answers) == 7 and not result.report.complete
    assert tuple(a.outcome for a in result.answers) == (
        "unknown",
        "unknown",
        "included",
        "unknown",
        "unknown",
        "included",
        "unknown",
    )
    assert result.answers[2].report.verified
    assert result.answers[5].report.verified
    listed = result.answers[6]
    assert listed.request == DatatypeFieldBranch(fields[4], 1)
    assert listed.raw_difference_satisfiable is None
    assert listed.raw_intersection_satisfiable is None
    assert listed.report.outcome == "unverified"
    assert (
        result.answers[0].native_error is not None
        and "MissingGlobalDataPropertyRange" in result.answers[0].native_error
    )

    malformed = check_datatype_obligations(
        backend,
        _source(),
        (
            DatatypeObligation(
                SMALL,
                WIDE,
                cast(TypingLiteral["inclusion", "disjointness"], "invalid"),
                location="Record.invalid",
            ),
        ),
    )
    assert malformed.report.outcome == "input_error"
    assert any(
        i.kind == "input_error"
        and i.location == "Record.invalid"
        and "Choose inclusion or disjointness" in i.message
        for i in malformed.report.issues
    )


def test_inconsistent_or_missing_import_source_never_proves_datatype_relationship(
    backend: OwlProfileBackend,
) -> None:
    requests = (DatatypeObligation(SMALL, WIDE),)
    inconsistent = check_datatype_obligations(
        backend,
        _source(
            "Declaration(NamedIndividual(<urn:i>)) ClassAssertion(owl:Nothing <urn:i>)"
        ),
        requests,
    )
    assert inconsistent.raw_consistent is False and inconsistent.consistent is False
    assert (
        inconsistent.answers[0].outcome == "unknown"
        and not inconsistent.report.verified
    )
    missing = check_datatype_obligations(
        backend, _source(imports="Import(<urn:absent>)"), requests
    )
    assert missing.answers[0].outcome == "unknown" and not missing.report.verified
    assert missing.fresh_property is None


def test_owned_cancellation_cumulative_cut_and_fresh_recovery(
    backend: OwlProfileBackend,
) -> None:
    request = (DatatypeObligation(WIDE, SMALL), DatatypeObligation(SMALL, WIDE))
    event = Event()
    event.set()
    cancelled = check_datatype_obligations(backend, _source(), request, cancel=event)
    assert not cancelled.report.verified and all(
        a.outcome == "unknown" for a in cancelled.answers
    )
    budget = Budget(Limits(max_work=2))
    cut = check_datatype_obligations(backend, _source(), request, budget=budget)
    assert cut.report.outcome == "exhausted" and budget.remaining("work") >= 0
    recovered = check_datatype_obligations(backend, _source(), request)
    assert recovered.report.complete and recovered.answers[0].outcome == "not_included"
    assert recovered.answers[1].report.verified


def test_imported_range_witnesses_remain_in_the_owned_required_closure(
    backend: OwlProfileBackend,
) -> None:
    """Actual imported definitions and superproperty ranges remain indispensable."""
    imported = prepare_owl_document(
        "Prefix(xsd:=<http://www.w3.org/2001/XMLSchema#>) "
        "Ontology(<urn:imported> " + _AXIOMS + ")",
        format="functional",
        document_iri="urn:imported-document",
    )
    source = prepare_owl_document(
        "Ontology(<urn:root> Import(<urn:imported>) "
        "Declaration(DataProperty(<urn:leaf>)) "
        "SubDataPropertyOf(<urn:leaf> <urn:child>))",
        format="functional",
        document_iri="urn:root-document",
    )
    field = FieldDeclaration(
        "value", "urn:leaf", (ValueDeclaration("literal", datatype="urn:small"),)
    )
    result = check_datatype_fields(
        backend,
        source,
        (field,),
        imports={"urn:imported": imported},
    )
    assert result.report.verified and len(result.documents) == 2
    assert {a.property.iri for a in result.answers[0].ranges} == {
        "urn:parent",
        "urn:alias",
    }


def test_selected_mixed_field_branches_preserve_original_indices(
    backend: OwlProfileBackend,
) -> None:
    """An explicit literal leaf never rewrites or silently closes a mixed field."""
    field = FieldDeclaration(
        "mixed",
        "urn:child",
        (
            ValueDeclaration("iri"),
            ValueDeclaration("literal", datatype="urn:small"),
        ),
    )
    selected = DatatypeFieldBranch(field, 1)
    result = check_datatype_branches(backend, _source(), (selected,))
    assert result.report.verified and len(result.answers) == 1
    assert result.answers[0].request == selected
    assert isinstance(result.answers[0].request, DatatypeFieldBranch)
    assert result.answers[0].request.field is field
    automatic = check_datatype_fields(backend, _source(), (field,))
    assert tuple(a.outcome for a in automatic.answers) == ("unknown", "included")
    assert not automatic.report.complete
