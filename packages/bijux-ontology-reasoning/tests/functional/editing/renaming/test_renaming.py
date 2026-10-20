"""Native role-aware transactions over original owned ontology modules."""

import os
from dataclasses import replace
from pathlib import Path

import pytest
from bijux_ontology_reasoning.axioms import ClassAssertion, NamedEntity, SubClassOf
from bijux_ontology_reasoning.axioms.changes import axiom_base
from bijux_ontology_reasoning.backend import OwlProfileBackend, prepare_profile_backend
from bijux_ontology_reasoning.documents import (
    OwlDocument,
    convert_ontology,
    prepare_owl_document,
)
from bijux_ontology_reasoning.editing.renaming import (
    AnnotationRole,
    ModuleCheck,
    ModuleInterfaceChange,
    ModuleSet,
    RoleRename,
    prepare_role_rename,
)
from bijux_ontology_reasoning.inference.entailment import check_entailments

from bijux_ontology.declarations.types import TermDeclaration
from bijux_ontology.limits import Budget
from bijux_ontology.reports import Report
from bijux_ontology.workflows.ontology.modules.types import ModuleContract

_BASE = """Ontology(<urn:ontology>
 Annotation(<urn:note> <urn:C>)
 Declaration(Class(<urn:C>)) Declaration(Class(<urn:D>))
 Declaration(NamedIndividual(<urn:C>)) Declaration(NamedIndividual(<urn:i>))
 Declaration(ObjectProperty(<urn:p>)) Declaration(AnnotationProperty(<urn:note>))
 ClassAssertion(<urn:C> <urn:C>) ClassAssertion(<urn:C> <urn:i>)
 ObjectPropertyAssertion(<urn:p> <urn:C> <urn:i>)
 SubClassOf(Annotation(Annotation(<urn:note> <urn:C>)
 <urn:note> <urn:C>) <urn:C> <urn:D>)
 AnnotationAssertion(<urn:note> <urn:C> <urn:C>)
 AnnotationAssertion(<urn:note> <urn:i> "literal urn:C")
)"""
_POLICY = (
    AnnotationRole("urn:note", "subject", "preserve"),
    AnnotationRole("urn:note", "value", "rename"),
)


@pytest.fixture
def backend(tmp_path: Path) -> OwlProfileBackend:
    """Use only explicitly selected pinned Java11/native artifacts."""
    java, directory, pin = (
        os.environ.get(name)
        for name in (
            "BIJUX_OWL_JAVA",
            "BIJUX_OWL_BUNDLE",
            "BIJUX_OWL_MANIFEST_SHA256",
        )
    )
    if not (java and directory and pin):
        pytest.skip("An explicit pinned OWL backend is not configured")
    return prepare_profile_backend(
        Path(java), Path(directory), manifest_sha256=pin, artifacts=tmp_path / "native"
    )


def document(text: str = _BASE, identity: str = "urn:source") -> OwlDocument:
    """Own ordinary native bytes with explicit parser and resource identities."""
    return prepare_owl_document(text, format="functional", document_iri=identity)


def logical_check(
    backend: OwlProfileBackend, *, all_roles: bool = False
) -> ModuleCheck:
    """Actually establish the mapped class obligations, independently of profiles."""

    def check(witness: ModuleSet, budget: Budget) -> Report:
        """Use maintained entailment on the complete supplied closure."""
        root = witness.documents[0]
        imports = {source.document_iri: source for source in witness.documents[1:]}
        return check_entailments(
            backend,
            root,
            (
                SubClassOf(
                    NamedEntity("class", "urn:NewC"), NamedEntity("class", "urn:D"), ()
                ),
                ClassAssertion(
                    NamedEntity(
                        "named_individual", "urn:NewC" if all_roles else "urn:C"
                    ),
                    NamedEntity("class", "urn:NewC"),
                    (),
                ),
            ),
            imports=imports,
            budget=budget,
        ).report

    return ModuleCheck("mapped-class-obligations", check)


def preservation_check(backend: OwlProfileBackend) -> ModuleCheck:
    """Check an actual maintained whole-axiom serialization roundtrip."""

    def check(witness: ModuleSet, budget: Budget) -> Report:
        """Reread all original candidate imports through the maintained writer."""
        return convert_ontology(
            backend,
            witness.documents[0],
            format="functional",
            imports={source.document_iri: source for source in witness.documents[1:]},
            budget=budget,
        ).report

    return ModuleCheck("native-annotated-structure", check)


def test_class_and_all_roles_preserve_occurrence_positions(
    backend: OwlProfileBackend,
) -> None:
    """Class positions change even in individual assertions; other facets stay."""
    original = document()
    for roles in (("class",), ("all",)):
        all_roles = roles == ("all",)
        candidate = prepare_role_rename(
            backend,
            original,
            RoleRename("urn:C", "urn:NewC", roles),
            expected={original.effective_url: axiom_base(original)},
            owned=(original.effective_url,),
            annotations=_POLICY,
            profiles=("owl2-dl",),
            after_checks=(logical_check(backend, all_roles=all_roles),),
        )
        assert candidate.report.verified and len(candidate.modules) == 1
        module = candidate.modules[0]
        assert module.profiles[0].in_profile and candidate.checks[0].verified
        content = module.source.content
        individual = b"urn:NewC" if all_roles else b"urn:C"
        assert b"ClassAssertion(<urn:NewC> <" + individual + b">)" in content
        assert (
            b"ObjectPropertyAssertion(<urn:p> <" + individual + b"> <urn:i>)" in content
        )
        assert b'"literal urn:C"' in content
        assert b"AnnotationAssertion(<urn:note> <urn:C> <urn:NewC>)" in content
        assert b"Annotation(<urn:note> <urn:NewC>)" in content
        assert module.additions and module.removals
        assert original.content == _BASE.encode()


def test_owned_imports_and_explicit_interfaces(backend: OwlProfileBackend) -> None:
    """Required child edits reject an unowned closure, then succeed together."""
    child = document(
        "Ontology(<urn:child> Declaration(Class(<urn:C>)) "
        "Declaration(Class(<urn:D>)) SubClassOf(<urn:C> <urn:D>))",
        "urn:child",
    )
    root = document(
        "Ontology(<urn:root> Import(<urn:child>) "
        "Declaration(NamedIndividual(<urn:C>)) ClassAssertion(<urn:C> <urn:C>))"
    )
    rename = RoleRename("urn:C", "urn:NewC", ("class",))
    expected = {source.effective_url: axiom_base(source) for source in (root, child)}
    old = ModuleContract(
        "provider",
        (TermDeclaration("urn:C", ("http://www.w3.org/2002/07/owl#Class",)),),
    )
    new = ModuleContract(
        "provider",
        (TermDeclaration("urn:NewC", ("http://www.w3.org/2002/07/owl#Class",)),),
    )
    unowned = prepare_role_rename(
        backend,
        root,
        rename,
        expected=expected,
        owned=(root.effective_url,),
        imports={"urn:child": child},
        after_checks=(logical_check(backend),),
    )
    assert not unowned.report.verified and not unowned.modules
    assert any(
        issue.code == "owl.rename_unowned_module" for issue in unowned.report.issues
    )
    accepted = prepare_role_rename(
        backend,
        root,
        rename,
        expected=expected,
        owned=(root.effective_url, child.effective_url),
        imports={"urn:child": child},
        interfaces={child.effective_url: old},
        interface_changes=(ModuleInterfaceChange(child.effective_url, old, new),),
        profiles=("owl2-dl",),
        after_checks=(logical_check(backend),),
    )
    assert accepted.report.verified and len(accepted.modules) == 2
    assert accepted.original_interfaces == ((child.effective_url, old),)
    assert accepted.interfaces == ((child.effective_url, new),)
    assert b"Class(<urn:C>)" in child.content


def test_collision_annotation_policy_and_exact_base_refuse_transaction(
    backend: OwlProfileBackend,
) -> None:
    """Reject target merges, undeclared annotation roles and stale bases."""
    original = document()
    expected = {original.effective_url: axiom_base(original)}
    for rename, policy in (
        (RoleRename("urn:C", "urn:D", ("class",)), _POLICY),
        (
            RoleRename("urn:C", "http://www.w3.org/2002/07/owl#Thing", ("class",)),
            _POLICY,
        ),
        (RoleRename("urn:C", "urn:NewC", ("class",)), ()),
    ):
        refused = prepare_role_rename(
            backend,
            original,
            rename,
            expected=expected,
            owned=(original.effective_url,),
            annotations=policy,
            after_checks=(logical_check(backend),),
        )
        assert not refused.report.verified and not refused.modules
    stale = prepare_role_rename(
        backend,
        original,
        RoleRename("urn:C", "urn:NewC", ("class",)),
        expected={
            original.effective_url: replace(
                axiom_base(original), effective_url="urn:other-base"
            )
        },
        owned=(original.effective_url,),
        annotations=_POLICY,
        after_checks=(logical_check(backend),),
    )
    assert not stale.report.verified and not stale.modules and stale.capability is None
    assert any(
        issue.code == "owl.rename_base_mismatch" for issue in stale.report.issues
    )


def test_failed_or_missing_postconditions_never_accept_native_candidate(
    backend: OwlProfileBackend,
) -> None:
    """Native syntax success cannot manufacture required application truth."""
    original = document()

    def missing_class(witness: ModuleSet, budget: Budget) -> Report:
        """Run an actual false entailment against the unchanged individual facet."""
        return check_entailments(
            backend,
            witness.documents[0],
            (
                ClassAssertion(
                    NamedEntity("named_individual", "urn:absent"),
                    NamedEntity("class", "urn:D"),
                    (),
                ),
            ),
            budget=budget,
        ).report

    for checks in ((), (ModuleCheck("missing-required-instance", missing_class),)):
        candidate = prepare_role_rename(
            backend,
            original,
            RoleRename("urn:C", "urn:NewC", ("class",)),
            expected={original.effective_url: axiom_base(original)},
            owned=(original.effective_url,),
            annotations=_POLICY,
            after_checks=checks,
        )
        assert candidate.modules and not candidate.report.verified
        assert original.content == _BASE.encode()


def test_datatype_role_preserves_literal_lexical_form_and_annotation_domain(
    backend: OwlProfileBackend,
) -> None:
    """Datatype identities change structurally, while literal characters stay exact."""
    original = document(
        "Ontology(<urn:datatype> Declaration(Datatype(<urn:C>)) "
        "Declaration(DataProperty(<urn:d>)) Declaration(NamedIndividual(<urn:i>)) "
        "Declaration(AnnotationProperty(<urn:note>)) "
        "AnnotationPropertyDomain(<urn:note> <urn:C>) "
        "AnnotationPropertyRange(<urn:note> <urn:C>) "
        'DataPropertyAssertion(<urn:d> <urn:i> "01"^^<urn:C>))'
    )
    policies = (
        AnnotationRole("urn:note", "domain", "rename"),
        AnnotationRole("urn:note", "range", "preserve"),
    )
    candidate = prepare_role_rename(
        backend,
        original,
        RoleRename("urn:C", "urn:NewC", ("datatype",)),
        expected={original.effective_url: axiom_base(original)},
        owned=(original.effective_url,),
        annotations=policies,
        after_checks=(preservation_check(backend),),
    )
    assert candidate.report.verified
    content = candidate.modules[0].source.content
    assert b'"01"^^<urn:NewC>' in content
    assert b"AnnotationPropertyDomain(<urn:note> <urn:NewC>)" in content
    assert b"AnnotationPropertyRange(<urn:note> <urn:C>)" in content
