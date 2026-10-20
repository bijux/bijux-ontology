"""Upgrade verdicts require actual application checks and preserve uncertainty."""

import threading

from rdflib import BNode, Graph, Literal, Namespace
from rdflib.collection import Collection
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    TermDeclaration,
    ValueDeclaration,
)
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.workflows.upgrades.assessment.checking import assess_upgrade

EX = Namespace("https://example.org/")


def test_removed_used_predicate_is_a_demonstrated_contract_break() -> None:
    before = Graph().add((EX.Record, RDF.type, OWL.Class))
    before.add((EX.value, RDF.type, OWL.DatatypeProperty))
    after = Graph().add((EX.Record, RDF.type, OWL.Class))
    model = ModelDeclaration(
        "Record",
        (str(EX.Record),),
        (
            FieldDeclaration(
                "value",
                str(EX.value),
                (ValueDeclaration("literal", datatype=str(XSD.string)),),
            ),
        ),
    )
    result = assess_upgrade(before, after, models=[model])
    assert result.verdict == "demonstrated-break"
    assert result.report.exit_code() == 1
    assert any(delta.status == "introduced" for delta in result.contracts)
    assert any(
        impact.use.field == "value" and impact.relation == "direct"
        for impact in result.affected
    )
    assert len(before) == 2 and len(after) == 1


def test_metadata_edits_and_possible_hierarchy_effects() -> None:
    before = Graph().add((EX.Parent, RDF.type, OWL.Class))
    before.add((EX.Child, RDF.type, OWL.Class))
    before.add((EX.Child, RDFS.subClassOf, EX.Parent))
    before.add((EX.Parent, RDFS.label, Literal("Name", lang="en")))
    after = Graph() + before
    after.set((EX.Parent, RDFS.label, Literal("New name", lang="en")))
    terms = [TermDeclaration(str(EX.Child))]
    labels = assess_upgrade(before, after, terms=terms)
    assert labels.verdict == "informational"
    assert not labels.affected
    after.add((EX.Parent, OWL.disjointWith, EX.Other))
    structure = assess_upgrade(before, after, terms=terms)
    assert structure.verdict == "informational"
    assert any(
        impact.use.iri == str(EX.Child)
        and impact.relation == "hierarchy"
        and impact.via == (str(EX.Parent),)
        for impact in structure.affected
    )


def test_stable_finding_identity_and_complete_resolution() -> None:
    graph = Graph().add((EX.A, RDF.type, OWL.Class))
    old = Issue("sample.constraint", "Old prose", "Model.field", expected="A")
    new = Issue("sample.constraint", "New prose", "Model.field", expected="B")
    unchanged = assess_upgrade(
        graph,
        graph,
        contract_reports=[
            (Report("contract", 1, (old,)), Report("contract", 1, (new,)))
        ],
    )
    assert unchanged.verdict == "informational"
    assert [delta.status for delta in unchanged.contracts] == ["persistent"]
    assert not unchanged.report.verified
    resolved = assess_upgrade(
        graph,
        graph,
        contract_reports=[(Report("contract", 1, (old,)), Report("contract", 1))],
    )
    assert [delta.status for delta in resolved.contracts] == ["resolved"]
    unknown = assess_upgrade(
        graph,
        graph,
        contract_reports=[
            (
                Report("contract", 1, (old,)),
                Report(
                    "contract",
                    0,
                    (Issue("sample.unavailable", "No input", kind="input_error"),),
                ),
            )
        ],
    )
    assert unknown.verdict == "unverified" and unknown.report.exit_code() == 2
    assert (
        next(delta for delta in unknown.contracts if delta.before == old).status
        == "uncertain"
    )


def test_incomplete_prior_check_cannot_prove_a_new_failure() -> None:
    graph = Graph().add((EX.A, RDF.type, OWL.Class))
    result = assess_upgrade(
        graph,
        graph,
        contract_reports=[
            (
                Report(
                    "contract",
                    0,
                    (Issue("sample.unknown", "Unavailable", kind="unverified"),),
                ),
                Report("contract", 1, (Issue("sample.failure", "Mismatch", "field"),)),
            )
        ],
    )
    assert result.verdict == "unverified"
    assert not any(delta.status == "introduced" for delta in result.contracts)


def test_new_deprecation_retains_ambiguous_replacement_hints() -> None:
    before = Graph().add((EX.A, RDF.type, OWL.Class))
    after = Graph() + before
    after.add((EX.A, OWL.deprecated, Literal(True)))
    after.add((EX.A, EX.replacedBy, EX.B))
    after.add((EX.A, EX.replacedBy, EX.C))
    result = assess_upgrade(
        before,
        after,
        terms=[TermDeclaration(str(EX.A))],
        replacement_predicates=[str(EX.replacedBy)],
    )
    assert result.verdict == "informational" and len(result.deprecated) == 1
    assert {edge.object for edge in result.deprecated[0].after.replacements} == {
        EX.B,
        EX.C,
    }
    assert result.deprecated[0].before.state == "absent"
    assert result.deprecated[0].after.state == "deprecated"


def test_changed_anonymous_expression_and_exhaustion_cannot_certify_compatibility() -> (
    None
):
    before = Graph().add((EX.A, RDF.type, OWL.Class))
    after = Graph() + before
    head = BNode()
    Collection(after, head, [EX.B, EX.C])
    after.add((EX.A, OWL.unionOf, head))
    result = assess_upgrade(before, after, terms=[TermDeclaration(str(EX.A))])
    assert result.verdict == "unverified" and not result.report.complete
    assert any(
        issue.code == "upgrade.anonymous_impact_unverified"
        for issue in result.report.issues
    )
    limited = assess_upgrade(
        before, after, terms=[TermDeclaration(str(EX.A))], limits=Limits(max_work=1)
    )
    assert limited.verdict == "unverified"
    assert limited.report.outcome == "exhausted"
    assert assess_upgrade(before, before).verdict == "unverified"


def test_optional_empty_prior_contract_is_not_a_verified_upgrade_baseline() -> None:
    graph = Graph().add((EX.A, RDF.type, OWL.Class))
    result = assess_upgrade(
        graph,
        graph,
        contract_reports=[
            (
                Report("contract", 0, allow_empty=True),
                Report("contract", 1, (Issue("sample.failure", "Mismatch", "field"),)),
            )
        ],
    )
    assert result.verdict == "unverified"
    assert not any(delta.status == "introduced" for delta in result.contracts)
    assert not result.report.verified


def test_completed_membership_does_not_hide_an_unassessed_additional_contract() -> None:
    graph = Graph().add((EX.A, RDF.type, OWL.Class))
    result = assess_upgrade(
        graph,
        graph,
        terms=[TermDeclaration(str(EX.A))],
        contract_reports=[
            (
                Report("optional", 0, allow_empty=True),
                Report("optional", 1, (Issue("sample.failure", "Mismatch", "field"),)),
            )
        ],
    )
    assert result.verdict == "unverified"
    assert any(delta.status == "uncertain" for delta in result.contracts)
    assert not result.report.verified


def test_shared_assessments_preserve_cumulative_capacity_and_parent_cancellation() -> (
    None
):
    graph = Graph().add((EX.A, RDF.type, OWL.Class))
    terms = (TermDeclaration(str(EX.A)),)
    shared = Budget()
    initial = shared.remaining("work")
    assert assess_upgrade(graph, graph, terms=terms, budget=shared).report.verified
    first = shared.remaining("work")
    assert first < initial
    assert assess_upgrade(graph, graph, terms=terms, budget=shared).report.verified
    assert shared.remaining("work") < first
    shared.consume("work", shared.remaining("work"))
    assert assess_upgrade(graph, graph, terms=terms, budget=shared).report.outcome == (
        "exhausted"
    )
    cancel = threading.Event()
    cancel.set()
    operation = Operation(None, None, 30, cancel)
    stopped = assess_upgrade(graph, graph, terms=terms, budget=operation)
    assert stopped.report.outcome == "exhausted"
    assert operation.remaining("work") == operation.limits.max_work


def test_completed_before_finding_survives_unadmitted_after_source() -> None:
    before = Graph().add((EX.B, RDF.type, OWL.Class))
    after = Graph().add((EX.A, RDF.type, OWL.Class))
    shared = Budget(Limits(max_resources=3))
    result = assess_upgrade(
        before, after, terms=(TermDeclaration(str(EX.A)),), budget=shared
    )
    assert result.verdict == "unverified" and result.report.outcome == "exhausted"
    assert len(result.before) == 1 and not result.after
    assert any(issue.kind == "finding" for issue in result.before[0].issues)
    assert any(child is result.before[0] for child in result.report.children)
    assert not result.contracts
    assert len(before) == len(after) == 1
