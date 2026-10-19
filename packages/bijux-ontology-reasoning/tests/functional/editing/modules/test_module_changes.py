"""Real all-or-none module moves preserve annotations and actual consumer scopes."""

import os
from pathlib import Path
from threading import Event
from typing import TypedDict, Unpack

import pytest
from bijux_ontology_reasoning.axioms.changes import axiom_base
from bijux_ontology_reasoning.axioms.inspection import inspect_axioms
from bijux_ontology_reasoning.axioms.types import (
    NamedEntity,
    NativeLiteral,
    OwlAnnotation,
    SubClassOf,
)
from bijux_ontology_reasoning.backend import OwlProfileBackend, prepare_profile_backend
from bijux_ontology_reasoning.documents import OwlDocument, prepare_owl_document
from bijux_ontology_reasoning.editing.modules import (
    AxiomMove,
    ModuleConsumer,
    ModuleImportChange,
    ModuleInline,
    ModuleInterfaceChange,
    ModuleRefactorCandidate,
    NamespaceTransfer,
    prepare_module_refactor,
)
from bijux_ontology_reasoning.integration import TheoryCombination, TheoryComponent

from bijux_ontology.declarations.types import TermDeclaration
from bijux_ontology.limits import Budget
from bijux_ontology.owl.obligations import OntologyObligation
from bijux_ontology.workflows.ontology.modules.types import (
    ModuleBinding,
    ModuleContract,
    ModuleRequirement,
)


def _class(name: str) -> NamedEntity:
    """Keep native class roles and original IRIs without namespace rewriting."""
    return NamedEntity("class", "urn:" + name)


def _definition() -> SubClassOf:
    """Select the exact annotated axiom through the maintained factory."""
    return SubClassOf(
        _class("Definition"),
        _class("Super"),
        (
            OwlAnnotation(
                "urn:note",
                NativeLiteral(
                    "kept",
                    "http://www.w3.org/2001/XMLSchema#string",
                    None,
                ),
                (),
            ),
        ),
    )


def _document(name: str, body: str) -> OwlDocument:
    """Capture ordinary native syntax for real OWLAPI parsing and serialization."""
    return prepare_owl_document(
        "Ontology(<urn:" + name + "> " + body + ")",
        format="functional",
        document_iri="urn:" + name,
    )


def _components(
    *, cycle: bool = False, joint_conflict: bool = False
) -> tuple[TheoryComponent, ...]:
    """Supply separate original modules, including an explicitly read-only original."""
    old = _document(
        "old",
        ("Import(<urn:consumer>) " if cycle else "")
        + """Annotation(<urn:note> "original header")
      Declaration(Class(<urn:Definition>)) Declaration(Class(<urn:Super>))
      Declaration(AnnotationProperty(<urn:note>))
      SubClassOf(Annotation(<urn:note> "kept") <urn:Definition> <urn:Super>)"""
        + (
            """Declaration(Class(<urn:X>)) Declaration(NamedIndividual(<urn:i>))
          ClassAssertion(<urn:X> <urn:i>)"""
            if joint_conflict
            else ""
        ),
    )
    new = _document(
        "new",
        """Declaration(Class(<urn:X>)) Declaration(Class(<urn:Y>))
        Declaration(NamedIndividual(<urn:i>)) DisjointClasses(<urn:X> <urn:Y>)
        ClassAssertion(<urn:Y> <urn:i>)"""
        if joint_conflict
        else "",
    )
    consumer = _document(
        "consumer",
        """Import(<urn:old>)
      Declaration(Class(<urn:Consumer>)) Declaration(Class(<urn:Definition>))
      Declaration(Class(<urn:Super>)) SubClassOf(<urn:Consumer> <urn:Definition>)""",
    )
    readonly = _document("readonly", "Declaration(Class(<urn:External>))")
    return tuple(
        TheoryComponent(name, source)
        for name, source in (
            ("old", old),
            ("new", new),
            ("consumer", consumer),
            ("readonly", readonly),
        )
    )


def _contracts() -> tuple[dict[str, ModuleContract], tuple[ModuleInterfaceChange, ...]]:
    """Keep required consumer meaning fixed while explicitly transferring exports."""
    exported = (
        TermDeclaration("urn:Definition", ("http://www.w3.org/2002/07/owl#Class",)),
        TermDeclaration("urn:Super", ("http://www.w3.org/2002/07/owl#Class",)),
    )
    requirement = ModuleRequirement(
        "definition",
        exported,
        (OntologyObligation("subsumption", ("urn:Definition",), "urn:Super"),),
    )
    old = ModuleContract("old-provider", exported)
    new = ModuleContract("new-provider", ())
    consumer = ModuleContract("consumer", (), (requirement,))
    return {"urn:old": old, "urn:new": new, "urn:consumer": consumer}, (
        ModuleInterfaceChange("urn:old", old, ModuleContract("old-provider", ())),
        ModuleInterfaceChange("urn:new", new, ModuleContract("new-provider", exported)),
    )


class _Options(TypedDict, total=False):
    """Keep overridden failure controls typed without generic keyword dictionaries."""

    keep_old: bool
    omit_import: bool
    protect: bool
    inline: bool
    wrong_base: bool
    max_work: bool
    cancel: Event


def _run(
    backend: OwlProfileBackend,
    components: tuple[TheoryComponent, ...],
    **options: Unpack[_Options],
) -> ModuleRefactorCandidate:
    """Run actual native, interface and deployment checks over exact sources."""
    interfaces, changes = _contracts()
    inline = options.get("inline", False)
    selected_changes: tuple[AxiomMove | ModuleInline, ...]
    transfers: tuple[NamespaceTransfer, ...]
    if inline:
        exported = (
            TermDeclaration("urn:Definition", ("http://www.w3.org/2002/07/owl#Class",)),
            TermDeclaration("urn:Super", ("http://www.w3.org/2002/07/owl#Class",)),
        )
        consumer = interfaces["urn:consumer"]
        interfaces["urn:consumer"] = ModuleContract(
            consumer.name, exported, consumer.requires
        )
        changes = (changes[0],)
        selected_changes = (ModuleInline("urn:old", "urn:consumer"),)
        binding = ModuleBinding("definition", "consumer")
        transfers = (NamespaceTransfer("urn:Definition", "urn:old", "urn:consumer"),)
    else:
        selected_changes = (AxiomMove("urn:old", "urn:new", (_definition(),)),)
        binding = ModuleBinding("definition", "new-provider")
        transfers = ()
    expected = {
        component.source.effective_url: axiom_base(component.source)
        for component in components
    }
    if options.get("wrong_base"):
        expected["urn:old"] = axiom_base(
            _document("old", "Declaration(Class(<urn:Changed>))")
        )
    shared = None
    if options.get("max_work"):
        shared = Budget()
        shared.consume("work", shared.remaining("work"))
    return prepare_module_refactor(
        backend,
        components,
        selected_changes,
        expected=expected,
        owned=("urn:old", "urn:new", "urn:consumer"),
        deployment=TheoryCombination("application", ("consumer", "old")),
        interfaces=interfaces,
        interface_changes=changes,
        consumers=(
            ModuleConsumer(
                "urn:consumer",
                (ModuleBinding("definition", "old-provider"),),
                (binding,),
                (SubClassOf(_class("Consumer"), _class("Super"), ()),),
            ),
        ),
        imports=(
            ModuleImportChange(
                "urn:consumer",
                () if inline or options.get("omit_import") else ("urn:new",),
                () if options.get("keep_old") else ("urn:old",),
            ),
        ),
        protected={"urn:old": (_definition(),)} if options.get("protect") else {},
        namespace_transfers=transfers,
        budget=shared,
        cancel=options.get("cancel"),
    )


@pytest.fixture
def backend(tmp_path: Path) -> OwlProfileBackend:
    """Select the normally installed explicit pinned native implementation."""
    java, bundle, pin = (
        os.environ.get(key)
        for key in (
            "BIJUX_OWL_JAVA",
            "BIJUX_OWL_BUNDLE",
            "BIJUX_OWL_MANIFEST_SHA256",
        )
    )
    if not (java and bundle and pin):
        pytest.skip("An explicit pinned OWL backend is not configured")
    return prepare_profile_backend(
        Path(java),
        Path(bundle),
        manifest_sha256=pin,
        artifacts=tmp_path / "native",
    )


def test_owned_annotated_move_preserves_actual_consumer_and_selected_joint(
    backend: OwlProfileBackend,
) -> None:
    components = _components()
    before = tuple(component.source.content for component in components)
    result = _run(backend, components)
    assert result.report.verified and result.candidate is not None
    assert len(result.modules) == 3 and len(result.joint) == 2
    assert all(report.verified for report in (*result.before, *result.after))
    by_identity = {module.source.effective_url: module for module in result.modules}
    assert any(
        "SubClassOf" in axiom and "kept" in axiom
        for axiom in by_identity["urn:old"].removals
    )
    assert any(
        "SubClassOf" in axiom and "kept" in axiom
        for axiom in by_identity["urn:new"].additions
    )
    assert tuple(component.source.content for component in components) == before
    assert result.candidate.documents[-1].content == components[-1].source.content


def test_missing_provider_import_cannot_pass_through_synthetic_joint_scope(
    backend: OwlProfileBackend,
) -> None:
    components = _components()
    result = _run(backend, components, omit_import=True)
    assert (
        result.candidate is None and not result.modules and not result.report.verified
    )
    assert all(report.verified for report in result.before)
    assert any(
        issue.code == "owl.module_provider_unreachable"
        for issue in result.report.issues
    )
    assert len(result.joint) == 2
    after = result.joint[-1]
    assert after.entailments is not None and not after.entailments.report.verified
    assert result.consumer_checks[-1].wiring is not None
    assert not result.consumer_checks[-1].wiring.report.verified
    assert all(
        module.source.sha256 == axiom_base(module.source).sha256
        for module in components
    )


def test_protected_axiom_and_wrong_source_set_base_reject_all_outputs(
    backend: OwlProfileBackend,
) -> None:
    components = _components()
    original = tuple(component.source.content for component in components)
    protected = _run(backend, components, protect=True)
    assert not protected.report.verified and protected.candidate is None
    assert not protected.modules
    assert any(
        issue.code == "owl.module_change_precondition"
        for issue in protected.report.issues
    )
    stale = _run(backend, components, wrong_base=True)
    assert stale.candidate is None and stale.capability is None
    assert any(
        issue.code == "owl.module_base_mismatch" for issue in stale.report.issues
    )
    assert tuple(component.source.content for component in components) == original


def test_explicit_inline_preserves_real_export_annotations_and_legal_cycles(
    backend: OwlProfileBackend,
) -> None:
    inline = _run(backend, _components(), inline=True)
    assert inline.report.verified and inline.candidate is not None
    assert inline.namespace_transfers == (
        NamespaceTransfer("urn:Definition", "urn:old", "urn:consumer"),
    )
    documents = {source.effective_url: source for source in inline.candidate.documents}
    inspection = inspect_axioms(
        backend, documents["urn:consumer"], axiom_types=("SubClassOf",)
    )
    assert inspection.report.verified
    assert _definition() in {record.axiom for record in inspection.axioms}
    assert not any(
        "SubClassOf" in axiom
        for module in inline.modules
        if module.source.effective_url == "urn:old"
        for axiom in module.additions
    )
    cycle = _run(backend, _components(cycle=True), keep_old=True)
    assert cycle.report.verified and cycle.candidate is not None
    assert all(
        check.entailments.report.verified
        and check.wiring is not None
        and check.wiring.report.verified
        for check in cycle.consumer_checks
    )


def test_new_joint_inconsistency_rejects_despite_each_consumer_check_passing(
    backend: OwlProfileBackend,
) -> None:
    components = _components(joint_conflict=True)
    original = tuple(component.source.content for component in components)
    result = _run(backend, components)
    assert (
        result.candidate is None and not result.modules and not result.report.verified
    )
    assert len(result.consumer_checks) == 2
    assert all(
        check.entailments.report.verified
        and check.wiring is not None
        and check.wiring.report.verified
        for check in result.consumer_checks
    )
    assert result.joint[0].consistency.consistent is True
    assert result.joint[1].consistency.consistent is False
    assert any(issue.code == "owl.inconsistent" for issue in result.report.issues)
    assert tuple(component.source.content for component in components) == original


def test_shared_budget_and_cancel_preserve_sources_and_next_request_recovers(
    backend: OwlProfileBackend,
) -> None:
    components = _components()
    original = tuple(component.source.content for component in components)
    assert _run(backend, components, max_work=True).report.outcome == "exhausted"
    cancelled = Event()
    cancelled.set()
    stopped = _run(backend, components, cancel=cancelled)
    assert stopped.report.outcome == "exhausted" and stopped.candidate is None
    assert _run(backend, components).report.verified
    assert tuple(component.source.content for component in components) == original
