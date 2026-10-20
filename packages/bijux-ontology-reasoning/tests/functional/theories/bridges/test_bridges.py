"""Actual row approvals, native bridge interactions and honest baseline controls."""

import os
from pathlib import Path
from threading import Event
from typing import cast

import pytest
from bijux_ontology_reasoning.axioms import NamedEntity, SubClassOf
from bijux_ontology_reasoning.backend import OwlProfileBackend, prepare_profile_backend
from bijux_ontology_reasoning.documents import prepare_owl_document
from bijux_ontology_reasoning.theories.bridges import (
    BridgeInterpretation,
    BridgeKind,
    check_mapping_bridges,
)
from bijux_ontology_reasoning.theories.integration import TheoryComponent
from rdflib import URIRef

from bijux_ontology.limits import Budget, Limits
from bijux_ontology.vocabulary.mappings import read_sssom
from bijux_ontology.vocabulary.mappings.composition.types import MappingOccurrence
from bijux_ontology.vocabulary.mappings.types import MappingSet

A = URIRef("urn:A")
PROTECTED = (
    SubClassOf(NamedEntity("class", "urn:A"), NamedEntity("class", "urn:B"), ()),
)
DECLARATIONS = " ".join(
    "Declaration(Class(<urn:" + name + ">))" for name in ("A", "B", "C", "Z")
)


def component(label: str, body: str = "", header: str = "") -> TheoryComponent:
    """Retain original byte and native resolution identities independently."""
    return TheoryComponent(
        label,
        prepare_owl_document(
            "Ontology(<urn:" + label + "> " + header + DECLARATIONS + " " + body + ")",
            format="functional",
            document_iri="urn:" + label,
        ),
    )


def mapping(
    rows: str = (
        "ex:A\tskos:exactMatch\tex:C\tsemapv:ManualMappingCuration"
        "\t0.900\tapproved by curator\n"
    ),
    extra_columns: str = "",
) -> MappingSet:
    """Use the actual maintained SSSOM reader rather than hand-built owned rows."""
    raw = (
        "# mapping_set_id: urn:bridge:mappings\n"
        "# license: https://creativecommons.org/publicdomain/zero/1.0/\n"
        '# curie_map:\n#   ex: "urn:"\n'
        "subject_id\tpredicate_id\tobject_id\tmapping_justification\tconfidence\tcomment"
        + extra_columns
        + "\n"
        + rows
    ).encode()
    result = read_sssom(
        raw,
        mapping_set_id="urn:bridge:mappings",
        license="https://creativecommons.org/publicdomain/zero/1.0/",
    )
    assert result.report.verified, result.report
    assert result.mapping_set is not None
    return result.mapping_set


@pytest.fixture
def backend(tmp_path: Path) -> OwlProfileBackend:
    """Require a caller-pinned real maintained OWLAPI/HermiT implementation."""
    java, bundle, pin = (
        os.environ.get(name)
        for name in ("BIJUX_OWL_JAVA", "BIJUX_OWL_BUNDLE", "BIJUX_OWL_MANIFEST_SHA256")
    )
    if not (java and bundle and pin):
        pytest.skip("An explicitly pinned native backend is required")
    return prepare_profile_backend(
        Path(java), Path(bundle), manifest_sha256=pin, artifacts=tmp_path / "native"
    )


def test_explicit_equivalence_preserves_original_row_and_reproduced_support(
    backend: OwlProfileBackend,
) -> None:
    """Only the approved row becomes OWL, with exact native owner correlation."""
    table = mapping()
    approval = BridgeInterpretation(
        MappingOccurrence(table, 0),
        "equivalent_classes",
        "curator approved class facet",
    )
    result = check_mapping_bridges(
        backend,
        table,
        component("left", "SubClassOf(<urn:A> <urn:B>)"),
        component("right", "DisjointClasses(<urn:B> <urn:C>)"),
        (approval,),
        protected_entailments=PROTECTED,
        named_classes=(A,),
    )
    assert result.scope_complete and result.report.outcome == "disagreement"
    assert (
        result.component_comparison
        and not result.component_comparison.newly_unsatisfiable
    )
    assert (
        result.bridge_comparison
        and result.bridge_comparison.newly_unsatisfiable == (A,)
    )
    assert result.observations[-1].consistency.consistent is True
    assert (
        result.observations[-1].entailments
        and result.observations[-1].entailments.report.verified
    )
    attribution = result.attributions[0]
    assert (
        attribution.explanation.justification
        and attribution.explanation.justification.reproduced
    )
    assert attribution.interpretations == (approval,)
    assert attribution.interpretations[0].occurrence.mapping_set is table
    assert (
        dict(attribution.interpretations[0].occurrence.row.cells)["confidence"]
        == "0.900"
    )
    assert {origin.document.document_iri for origin in attribution.origins} == {
        "urn:left",
        "urn:right",
        "urn:bijux:ontology:mapping-bridge:0",
    }


def test_plain_exact_match_does_not_create_owl_and_joint_consumers_stay_required(
    backend: OwlProfileBackend,
) -> None:
    """Mapping syntax never promotes a relation or erases a missing consumer proof."""
    table = mapping()
    result = check_mapping_bridges(
        backend,
        table,
        component("left", "SubClassOf(<urn:A> <urn:B>)"),
        component("right", "DisjointClasses(<urn:B> <urn:C>)"),
        (),
        protected_entailments=PROTECTED,
        named_classes=(A,),
    )
    assert (
        result.scope_complete
        and result.report.verified
        and not result.bridges
        and not result.attributions
    )
    assert result.bridge_comparison is None
    right = result.observations[1]
    assert right.entailments and right.entailments.answers[0].outcome == "not_entailed"
    absent = check_mapping_bridges(
        backend,
        table,
        component("left"),
        component("right"),
        (),
        protected_entailments=PROTECTED,
        named_classes=(A,),
    )
    assert absent.scope_complete and absent.report.outcome == "disagreement"
    empty = check_mapping_bridges(
        backend,
        table,
        component("left"),
        component("right"),
        (),
        protected_entailments=(),
    )
    assert empty.report.outcome == "input_error" and not empty.scope_complete


def test_equal_logical_rows_retain_multiple_original_bridge_owners(
    backend: OwlProfileBackend,
) -> None:
    """A native support axiom can correlate with several approved row occurrences."""
    table = mapping(
        "ex:A\tskos:exactMatch\tex:C\tsemapv:ManualMappingCuration\t0.900\tfirst\n"
        "ex:A\tskos:exactMatch\tex:C\tsemapv:ManualMappingCuration\t0.200\tsecond\n"
    )
    selected = tuple(
        BridgeInterpretation(
            MappingOccurrence(table, index), "equivalent_classes", "explicit approval"
        )
        for index in range(2)
    )
    result = check_mapping_bridges(
        backend,
        table,
        component("left", "SubClassOf(<urn:A> <urn:B>)"),
        component("right", "DisjointClasses(<urn:B> <urn:C>)"),
        selected,
        protected_entailments=PROTECTED,
        named_classes=(A,),
    )
    assert result.scope_complete and result.report.outcome == "disagreement"
    assert result.attributions[0].interpretations == selected
    assert {
        origin.document.document_iri
        for origin in result.attributions[0].origins
        if origin.document.document_iri.startswith("urn:bijux:ontology:mapping-bridge:")
    } == {"urn:bijux:ontology:mapping-bridge:0", "urn:bijux:ontology:mapping-bridge:1"}


def test_old_union_and_component_problems_are_never_bridge_blame(
    backend: OwlProfileBackend,
) -> None:
    """An unrelated bridge does not acquire an existing failure's ownership."""
    table = mapping(
        "ex:B\tskos:exactMatch\tex:C\tsemapv:ManualMappingCuration\t0.100\tirrelevant\n"
    )
    approved = (
        BridgeInterpretation(
            MappingOccurrence(table, 0), "subclass", "selected class direction"
        ),
    )
    for left, right in (
        (
            component(
                "left", "SubClassOf(<urn:A> <urn:B>) SubClassOf(<urn:A> <urn:C>)"
            ),
            component("right", "DisjointClasses(<urn:B> <urn:C>)"),
        ),
        (
            component(
                "left",
                "SubClassOf(<urn:A> <urn:B>) SubClassOf(<urn:A> <http://www.w3.org/2002/07/owl#Nothing>)",
            ),
            component("right"),
        ),
        (
            component(
                "left",
                "Declaration(NamedIndividual(<urn:i>)) "
                "SubClassOf(<urn:A> <urn:B>) "
                "SubClassOf(<urn:A> <http://www.w3.org/2002/07/owl#Nothing>) "
                "ClassAssertion(<urn:A> <urn:i>)",
            ),
            component("right"),
        ),
    ):
        result = check_mapping_bridges(
            backend,
            table,
            left,
            right,
            approved,
            protected_entailments=PROTECTED,
            named_classes=(A,),
        )
        assert (
            result.bridge_comparison
            and not result.bridge_comparison.newly_unsatisfiable
        )
        assert result.bridge_comparison.newly_inconsistent is False
        assert (
            not result.attributions
            and not result.explanations
            and not result.report.verified
        )


def test_property_direction_and_equivalence_use_actual_declared_facets(
    backend: OwlProfileBackend,
) -> None:
    """Two explicitly approved arrows establish property equivalence without sameAs."""
    for category, mode in (
        ("ObjectProperty", "equivalent_object_properties"),
        ("DataProperty", "equivalent_data_properties"),
    ):
        native_type = (
            "owl object property"
            if category == "ObjectProperty"
            else "owl data property"
        )
        table = mapping(
            "ex:p\tskos:exactMatch\tex:q\tsemapv:ManualMappingCuration"
            "\t0.500\tproperty interpretation\t"
            + native_type
            + "\t"
            + native_type
            + "\n",
            "\tsubject_type\tobject_type",
        )
        declarations = (
            "Declaration("
            + category
            + "(<urn:p>)) Declaration("
            + category
            + "(<urn:q>))"
        )
        result = check_mapping_bridges(
            backend,
            table,
            component("left", declarations + " SubClassOf(<urn:A> <urn:B>)"),
            component("right", declarations),
            (
                BridgeInterpretation(
                    MappingOccurrence(table, 0),
                    cast(BridgeKind, mode),
                    "curator approved property equivalence",
                ),
            ),
            protected_entailments=PROTECTED,
            named_classes=(A,),
        )
        assert result.scope_complete and result.report.verified
        assert len(result.bridges[0].candidate.additions) == 4
        assert all(
            type(axiom).__name__ != "SameIndividuals"
            for axiom in result.bridges[0].candidate.additions
        )


def test_unknown_qualifier_and_absent_original_role_cannot_gain_fresh_declarations(
    backend: OwlProfileBackend,
) -> None:
    """Checked construction cannot manufacture missing original endpoint roles."""
    tables = (
        mapping(
            "ex:A\tskos:exactMatch\tex:C\tsemapv:ManualMappingCuration\t0.900\tnegated\tNot\n",
            "\tpredicate_modifier",
        ),
        mapping(
            "ex:A\tskos:exactMatch\tex:Absent\tsemapv:ManualMappingCuration"
            "\t0.900\tabsent original endpoint\n"
        ),
        mapping(
            "ex:A\tskos:exactMatch\tex:C\tsemapv:ManualMappingCuration"
            "\t0.900\texplicit other facet\towl object property\n",
            "\tsubject_type",
        ),
    )
    for table in tables:
        result = check_mapping_bridges(
            backend,
            table,
            component("left", "SubClassOf(<urn:A> <urn:B>)"),
            component("right"),
            (
                BridgeInterpretation(
                    MappingOccurrence(table, 0),
                    "equivalent_classes",
                    "explicit selection",
                ),
            ),
            protected_entailments=PROTECTED,
            named_classes=(A,),
        )
        assert not result.scope_complete and result.report.outcome == "unverified"
        assert not result.bridges and len(result.observations) == 3
        assert any(
            issue.code == "owl.bridge_semantics_unverified" for issue in result.report
        )


def test_original_occurrence_binding_missing_imports_and_bounds(
    backend: OwlProfileBackend,
) -> None:
    """A lookalike producer, incomplete theory or bounded prefix cannot pass."""
    table, other = mapping(), mapping()
    result = check_mapping_bridges(
        backend,
        table,
        component("left"),
        component("right"),
        (
            BridgeInterpretation(
                MappingOccurrence(other, 0), "subclass", "foreign occurrence"
            ),
        ),
        protected_entailments=PROTECTED,
    )
    assert result.report.outcome == "input_error" and not result.observations
    missing = check_mapping_bridges(
        backend,
        table,
        component("left", header="Import(<urn:missing>) "),
        component("right"),
        (),
        protected_entailments=PROTECTED,
        named_classes=(A,),
    )
    assert (
        not missing.scope_complete
        and not missing.report.verified
        and not missing.attributions
    )
    for budget, cancel in ((Budget(Limits(max_work=1)), None), (None, Event())):
        if cancel is not None:
            cancel.set()
        bounded = check_mapping_bridges(
            backend,
            table,
            component("left"),
            component("right"),
            (),
            protected_entailments=PROTECTED,
            budget=budget,
            cancel=cancel,
        )
        assert not bounded.scope_complete and not bounded.observations
