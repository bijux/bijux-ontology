"""Explicit root publication policies remain separate from legal SKOS integrity."""

from dataclasses import replace
from typing import Any

import pytest
from rdflib import BNode, Graph, Literal, Namespace
from rdflib.namespace import RDF, SKOS

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.skos.checking import check_skos
from bijux_ontology.skos.publication import TopConceptPolicy, check_top_concepts
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary

EX = Namespace("https://example.org/publication/")


def graph() -> Graph:
    return Graph().add((EX.Scheme, RDF.type, SKOS.ConceptScheme))


def test_required_roots_accept_either_inverse_and_missing_is_not_core_integrity() -> (
    None
):
    source = graph()
    source.add((EX.Root, RDF.type, SKOS.Concept))
    policy = TopConceptPolicy(str(EX.Scheme), required=(str(EX.Root),))
    assert check_skos(source).verified
    missing = check_top_concepts(inspect_vocabulary(source), policy)
    assert missing.report.complete and missing.report.outcome == "disagreement"
    assert missing.report.issues[0].code == "skos.publication.required_root_missing"
    for triple in (
        (EX.Root, SKOS.topConceptOf, EX.Scheme),
        (EX.Scheme, SKOS.hasTopConcept, EX.Root),
    ):
        selected = source + Graph().add(triple)
        result = check_top_concepts(inspect_vocabulary(selected), policy)
        assert result.report.verified and result.roots == (EX.Root,)
        assert len(result.assertions) == 1
        assert result.assertions[0].predicate == triple[1]
    source.add((EX.Scheme, SKOS.hasTopConcept, EX.Root))
    source.add((EX.Root, SKOS.topConceptOf, EX.Scheme))
    view = inspect_vocabulary(source)
    source.remove((None, SKOS.hasTopConcept, None))
    result = check_top_concepts(view, policy)
    assert result.report.verified and len(result.assertions) == 2


def test_allowed_inventory_and_authored_broader_policies_are_explicit() -> None:
    source = graph()
    source.add((EX.Scheme, SKOS.hasTopConcept, EX.Root))
    source.add((EX.OtherScheme, SKOS.hasTopConcept, EX.Unselected))
    policy = TopConceptPolicy(str(EX.Scheme), allowed=(str(EX.Root),))
    assert check_top_concepts(inspect_vocabulary(source), policy).report.verified
    forbidden = check_top_concepts(
        inspect_vocabulary(source), replace(policy, allowed=())
    )
    assert [issue.code for issue in forbidden.report.issues] == [
        "skos.publication.root_not_allowed"
    ]
    for triple in (
        (EX.Root, SKOS.broader, EX.Parent),
        (EX.Parent, SKOS.narrower, EX.Root),
    ):
        changed = source + Graph().add(triple)
        changed.add((EX.Parent, SKOS.inScheme, EX.OtherScheme))
        assert check_top_concepts(inspect_vocabulary(changed), policy).report.verified
        outside = replace(policy, broader="forbid-within-scheme")
        assert check_top_concepts(inspect_vocabulary(changed), outside).report.verified
        any_parent = check_top_concepts(
            inspect_vocabulary(changed), replace(policy, broader="forbid")
        )
        assert any_parent.report.outcome == "disagreement"
        assert any_parent.report.issues[0].evidence
        changed.add((EX.Parent, SKOS.inScheme, EX.Scheme))
        within = check_top_concepts(inspect_vocabulary(changed), outside)
        assert within.report.outcome == "disagreement"
        changed.remove((EX.Parent, SKOS.inScheme, None))
        unknown = check_top_concepts(inspect_vocabulary(changed), outside)
        assert unknown.report.outcome == "unverified"
        assert (
            unknown.report.issues[0].code == "skos.publication.broader_scope_unverified"
        )
    mapped = source + Graph().add((EX.Root, SKOS.broadMatch, EX.Parent))
    assert check_top_concepts(
        inspect_vocabulary(mapped), replace(policy, broader="forbid")
    ).report.verified


def test_policy_inventory_ownership_and_runtime_boundaries() -> None:
    supplied = [str(EX.Root)]
    policy = TopConceptPolicy(str(EX.Scheme), required=supplied)  # type: ignore[arg-type]
    supplied.clear()
    assert policy.required == (str(EX.Root),)
    invalid: tuple[Any, ...] = (
        None,
        "values",
        iter(()),
        [{}],
        [BNode("node")],
        [Literal(str(EX.Root))],
    )
    for value in invalid:
        with pytest.raises(InputError):
            TopConceptPolicy(str(EX.Scheme), required=value)
    for scheme in (None, BNode("node"), Literal(str(EX.Scheme)), "relative"):
        with pytest.raises(InputError):
            TopConceptPolicy(scheme, allowed=())  # type: ignore[arg-type]
    with pytest.raises(InputError):
        TopConceptPolicy(str(EX.Scheme))
    with pytest.raises(InputError):
        TopConceptPolicy(str(EX.Scheme), required=(str(EX.Root),), allowed=())
    with pytest.raises(InputError):
        TopConceptPolicy(str(EX.Scheme), required=(str(EX.Root), str(EX.Root)))


def test_incomplete_inventory_retains_truth_before_exhausted_parent_lookup() -> None:
    source = graph()
    source.add((EX.Scheme, SKOS.hasTopConcept, EX.Root))
    source.add((EX.Root, SKOS.broader, EX.Parent))
    view = inspect_vocabulary(source)
    required = TopConceptPolicy(str(EX.Scheme), required=(str(EX.Missing),))
    result = check_top_concepts(view, required, budget=Budget(Limits(max_work=2)))
    assert result.report.outcome == "exhausted" and not result.report.complete
    assert not any(
        issue.code == "skos.publication.required_root_missing"
        for issue in result.report.issues
    )
    policy = TopConceptPolicy(str(EX.Scheme), allowed=(), broader="forbid")
    result = check_top_concepts(view, policy, budget=Budget(Limits(max_work=200)))
    assert result.report.outcome == "exhausted"
    assert any(
        issue.code == "skos.publication.root_not_allowed"
        for issue in result.report.issues
    )
    source.add((EX.Scheme, SKOS.hasTopConcept, Literal("invalid")))
    result = check_top_concepts(inspect_vocabulary(source), required)
    assert not result.report.verified
    assert any(
        issue.code == "skos.publication.reference_unverified"
        for issue in result.report.issues
    )
    assert any(
        issue.code == "skos.publication.required_root_missing"
        for issue in result.report.issues
    )
