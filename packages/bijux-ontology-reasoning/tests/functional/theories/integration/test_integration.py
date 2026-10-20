"""Native joint interactions with original and unbridged scope controls."""

import os
from pathlib import Path
from threading import Event

import pytest
from bijux_ontology_reasoning.axioms import NamedEntity, SubClassOf
from bijux_ontology_reasoning.backend import OwlProfileBackend, prepare_profile_backend
from bijux_ontology_reasoning.documents import prepare_owl_document
from bijux_ontology_reasoning.theories.integration import (
    TheoryCombination,
    TheoryComponent,
    check_integration,
)
from rdflib import URIRef

from bijux_ontology.limits import Budget, Limits

A = URIRef("urn:A")
DECLARATIONS = " ".join(
    "Declaration(Class(<urn:" + name + ">))" for name in ("A", "B", "C", "Z")
)


def component(label: str, body: str = "", header: str = "") -> TheoryComponent:
    """Keep separately supplied original source and resolution identities explicit."""
    return TheoryComponent(
        label,
        prepare_owl_document(
            "Ontology(<urn:" + label + "> " + header + DECLARATIONS + " " + body + ")",
            format="functional",
            document_iri="urn:" + label,
        ),
    )


@pytest.fixture
def backend(tmp_path: Path) -> OwlProfileBackend:
    """Require the actual caller-selected pinned maintained Java backend."""
    java, directory, pin = (
        os.environ.get(name)
        for name in ("BIJUX_OWL_JAVA", "BIJUX_OWL_BUNDLE", "BIJUX_OWL_MANIFEST_SHA256")
    )
    if not (java and directory and pin):
        pytest.skip("An explicitly pinned native backend is required")
    return prepare_profile_backend(
        Path(java), Path(directory), manifest_sha256=pin, artifacts=tmp_path / "native"
    )


def conflicting() -> tuple[TheoryComponent, TheoryComponent]:
    """Each original is coherent while their selected joint makes A unsatisfiable."""
    return (
        component("left", "SubClassOf(<urn:A> <urn:B>) SubClassOf(<urn:A> <urn:C>)"),
        component("right", "DisjointClasses(<urn:B> <urn:C>)"),
    )


def test_new_joint_unsatisfiability_has_reproduced_original_module_support(
    backend: OwlProfileBackend,
) -> None:
    """New interaction proof retains exact original owners without flattening."""
    sources = (
        conflicting()[0],
        TheoryComponent(
            "right",
            prepare_owl_document(
                "Ontology(<urn:right> Declaration(Class(<urn:B>)) "
                "Declaration(Class(<urn:C>)) DisjointClasses(<urn:B> <urn:C>))",
                format="functional",
                document_iri="urn:right",
            ),
        ),
    )
    result = check_integration(
        backend,
        sources,
        (TheoryCombination("deployment", ("left", "right")),),
        named_classes=(A,),
    )
    assert result.scope_complete and not result.all_combinations
    assert result.report.outcome == "disagreement"
    joint = result.combinations[0]
    assert joint.component_comparison.newly_unsatisfiable == (A,)
    assert joint.joint.consistency.consistent is True
    assert len(joint.interactions) == 1
    interaction = joint.interactions[0]
    assert (
        interaction.explanation.justification
        and interaction.explanation.justification.reproduced
    )
    assert {origin.document.document_iri for origin in interaction.origins} == {
        "urn:left",
        "urn:right",
    }
    assert all(
        origin.document.content
        in (sources[0].source.content, sources[1].source.content)
        for origin in interaction.origins
    )
    assert all(
        observation.consistency.consistent is True
        for observation in result.observations
    )
    right = next(value for value in result.observations if value.label == "right")
    assert right.signature_complete
    assert not any(answer.iri == A for answer in right.consistency.classes)


def test_preexisting_and_unselected_unsatisfiable_classes_are_not_new_blame(
    backend: OwlProfileBackend,
) -> None:
    """Retain old problems and keep unselected class obligations outside acceptance."""
    old = component(
        "old", "SubClassOf(<urn:A> <http://www.w3.org/2002/07/owl#Nothing>)"
    )
    right = component("right")
    result = check_integration(
        backend,
        (old, right),
        (TheoryCombination("deployment", ("old", "right")),),
        named_classes=(A,),
    )
    comparison = result.combinations[0].component_comparison
    assert (
        not comparison.newly_unsatisfiable
        and comparison.preexisting_unsatisfiable == (A,)
    )
    assert not result.explanations and result.report.outcome == "disagreement"
    unrelated = component(
        "unrelated", "SubClassOf(<urn:Z> <http://www.w3.org/2002/07/owl#Nothing>)"
    )
    selected = check_integration(
        backend,
        (unrelated, right),
        (TheoryCombination("deployment", ("unrelated", "right")),),
        named_classes=(A,),
    )
    assert selected.scope_complete and selected.report.verified
    assert any(
        issue.code == "owl.unsatisfiable_class"
        for issue in selected.observations[0].consistency.report
    )


def test_unbridged_regression_is_not_blamed_on_an_irrelevant_bridge(
    backend: OwlProfileBackend,
) -> None:
    """Bridge comparison uses the already failing union rather than only components."""
    result = check_integration(
        backend,
        (*conflicting(), component("bridge")),
        (TheoryCombination("deployment", ("left", "right"), ("bridge",)),),
        named_classes=(A,),
    )
    assert result.scope_complete
    joint = result.combinations[0]
    assert joint.component_comparison.newly_unsatisfiable == (A,)
    assert joint.bridge_comparison and not joint.bridge_comparison.newly_unsatisfiable
    assert joint.bridge_comparison.preexisting_unsatisfiable == (A,)
    assert len(joint.interactions) == 1 and joint.interactions[0].stage == "unbridged"
    assert all(
        origin.document.document_iri != "urn:bridge"
        for origin in joint.interactions[0].origins
    )


def test_explicit_bridge_can_introduce_a_real_new_interaction(
    backend: OwlProfileBackend,
) -> None:
    """An authored OWL bridge is evaluated after its coherent unbridged baseline."""
    result = check_integration(
        backend,
        (
            component("left", "SubClassOf(<urn:A> <urn:B>)"),
            component("right", "DisjointClasses(<urn:B> <urn:C>)"),
            component("bridge", "SubClassOf(<urn:A> <urn:C>)"),
        ),
        (TheoryCombination("deployment", ("left", "right"), ("bridge",)),),
        named_classes=(A,),
    )
    assert result.scope_complete
    joint = result.combinations[0]
    assert not joint.component_comparison.newly_unsatisfiable
    assert joint.bridge_comparison and joint.bridge_comparison.newly_unsatisfiable == (
        A,
    )
    assert joint.interactions[0].stage == "bridged"
    assert {
        origin.document.document_iri for origin in joint.interactions[0].origins
    } == {"urn:left", "urn:right", "urn:bridge"}


@pytest.mark.parametrize(
    ("ontology_iri", "document_iri"),
    (
        ("urn:left", "urn:left"),
        ("urn:bijux:ontology:joint", "urn:left"),
        ("urn:left", "urn:bijux:ontology:joint"),
    ),
)
def test_component_nonentailment_does_not_replace_the_joint_consumer_answer(
    backend: OwlProfileBackend,
    ontology_iri: str,
    document_iri: str,
) -> None:
    """A joint consumer proof preserves originals even at synthetic identity names."""
    query = SubClassOf(NamedEntity("class", "urn:A"), NamedEntity("class", "urn:B"), ())
    source = prepare_owl_document(
        "Ontology(<"
        + ontology_iri
        + "> "
        + DECLARATIONS
        + " SubClassOf(<urn:A> <urn:B>))",
        format="functional",
        document_iri=document_iri,
    )
    original = (
        source.content,
        source.sha256,
        source.document_iri,
        source.effective_url,
    )
    result = check_integration(
        backend,
        (TheoryComponent("left", source), component("right")),
        (TheoryCombination("deployment", ("left", "right")),),
        named_classes=(A,),
        consumer_entailments=(query,),
    )
    assert result.scope_complete and result.report.verified
    change = result.combinations[0].component_comparison.entailments[0]
    assert change.before == (True, False) and change.after is True
    right = next(value for value in result.observations if value.label == "right")
    assert right.entailments and right.entailments.answers[0].outcome == "not_entailed"
    assert (
        source.content,
        source.sha256,
        source.document_iri,
        source.effective_url,
    ) == original


def test_joint_inconsistency_is_distinct_from_class_satisfiability(
    backend: OwlProfileBackend,
) -> None:
    """A globally inconsistent union gets an actual inconsistency support."""
    result = check_integration(
        backend,
        (
            component(
                "left",
                "Declaration(NamedIndividual(<urn:i>)) "
                "SubClassOf(<urn:A> <urn:B>) ClassAssertion(<urn:A> <urn:i>)",
            ),
            component(
                "right", "SubClassOf(<urn:A> <urn:C>) DisjointClasses(<urn:B> <urn:C>)"
            ),
        ),
        (TheoryCombination("deployment", ("left", "right")),),
        named_classes=(),
    )
    joint = result.combinations[0]
    assert joint.joint.consistency.consistent is False
    assert joint.component_comparison.newly_inconsistent is True
    assert not joint.component_comparison.newly_unsatisfiable
    assert joint.interactions[0].class_iri is None
    assert joint.interactions[0].explanation.explanation_report.verified


def test_missing_imports_unselected_sources_and_bounded_prefixes_remain_honest(
    backend: OwlProfileBackend,
) -> None:
    """Unselected bad data is untouched; missing required resources prevent success."""
    left, right = component("left"), component("right")
    malformed = TheoryComponent(
        "unused",
        prepare_owl_document(
            "not an ontology", format="functional", document_iri="urn:unused"
        ),
    )
    selections = (
        TheoryCombination("first", ("left", "right")),
        TheoryCombination("second", ("left", "unused")),
    )
    prefix = check_integration(
        backend,
        (left, right, malformed),
        selections,
        max_combinations=1,
        named_classes=(A,),
    )
    assert len(prefix.combinations) == 1 and prefix.combinations[0].report.verified
    assert not prefix.scope_complete and not prefix.all_combinations
    assert all(value.label != "unused" for value in prefix.observations)
    missing = check_integration(
        backend,
        (component("missing", header="Import(<urn:absent>) "), right),
        (TheoryCombination("deployment", ("missing", "right")),),
        named_classes=(A,),
    )
    assert not missing.scope_complete and not missing.report.verified
    assert not missing.explanations
    tiny = check_integration(
        backend, (left, right), selections, budget=Budget(Limits(max_work=1))
    )
    assert not tiny.observations and tiny.report.outcome == "exhausted"
    event = Event()
    event.set()
    cancelled = check_integration(backend, (left, right), selections, cancel=event)
    assert not cancelled.observations and not cancelled.scope_complete
    absent_backend = prepare_profile_backend(
        Path("/bijux-unavailable-java"),
        backend.directory,
        manifest_sha256=backend.manifest_sha256,
        artifacts=backend.artifacts / "unavailable-runtime",
    )
    absent = check_integration(
        absent_backend,
        (left, right),
        (TheoryCombination("deployment", ("left", "right")),),
        named_classes=(A,),
    )
    assert not absent.scope_complete and absent.report.outcome == "unverified"


def test_conflicting_import_versions_do_not_choose_one_component_interpretation(
    backend: OwlProfileBackend,
) -> None:
    """Individually valid import versions cannot silently overwrite one another."""
    sources = []
    for name, body in (("left", ""), ("right", "SubClassOf(<urn:B> <urn:C>)")):
        imported = prepare_owl_document(
            "Ontology(<urn:dependency> " + DECLARATIONS + body + ")",
            format="functional",
            document_iri="urn:dependency",
            effective_url="urn:dependency-" + name,
        )
        root = component(name, header="Import(<urn:dependency>) ")
        sources.append(TheoryComponent(name, root.source, {"urn:dependency": imported}))
    result = check_integration(
        backend,
        sources,
        (TheoryCombination("deployment", ("left", "right")),),
        named_classes=(A,),
    )
    assert len(result.observations) == 2 and not result.combinations
    assert all(value.consistency.consistent is True for value in result.observations)
    assert not result.scope_complete and result.report.outcome == "unverified"
