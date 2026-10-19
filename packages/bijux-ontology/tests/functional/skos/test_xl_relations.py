"""Explicit XL label relation policies retain RDF identities and partial truth."""

from collections.abc import Sequence
from dataclasses import FrozenInstanceError, replace
from typing import Any, cast

import pytest
from rdflib import BNode, Graph, Literal, Namespace
from rdflib.namespace import RDF, RDFS, SKOS, XSD

from bijux_ontology.absence import ABSENT
from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.skos.label_relations import XLRelationPolicy, check_xl_relations
from bijux_ontology.skos.xl import XL
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary

EX = Namespace("https://example.org/label-relations/")


def labels() -> Graph:
    return (
        Graph()
        .add((EX.Expansion, XL.literalForm, Literal("Food organization", lang="en")))
        .add((EX.Acronym, XL.literalForm, Literal("FAO", lang="en")))
        .add((EX.Expansion, EX.acronym, EX.Acronym))
    )


def test_caller_subrelations_preserve_assertions_and_never_inherit_symmetry() -> None:
    source = labels()
    source.add((EX.acronym, RDFS.subPropertyOf, XL.labelRelation))
    view = inspect_vocabulary(source)
    base = XLRelationPolicy(str(XL.labelRelation))
    assert check_xl_relations(view, [base]).items == ()
    policy = replace(
        base,
        subrelations=(str(EX.acronym),),
        source_language="EN",
        target_language="en",
        language_relation="same",
        form_relation="different",
    )
    selected = check_xl_relations(view, [policy])
    assert selected.report.verified and len(selected.items) == 1
    relation = selected.items[0]
    assert relation.assertion.predicate == EX.acronym
    assert relation.assertion.subject == EX.Expansion
    assert relation.source is not None and relation.source.resource == EX.Expansion
    assert relation.target is not None and relation.target.resource == EX.Acronym
    assert relation.matches and relation.orientation == "forward"
    source.set((EX.Expansion, XL.literalForm, Literal("Organisation", lang="fr")))
    view = inspect_vocabulary(source)
    asymmetric = replace(
        policy,
        source_language="en",
        target_language="fr",
        language_relation="different",
    )
    assert check_xl_relations(view, [asymmetric]).report.outcome == "disagreement"
    either = check_xl_relations(view, [replace(asymmetric, direction="either")])
    assert either.report.verified and either.items[0].orientation == "reversed"
    assert either.items[0].assertion.subject == EX.Expansion


def test_equal_forms_and_owned_blank_labels_never_merge_resources() -> None:
    child = BNode("shared-label")
    source = (
        Graph()
        .add((EX.First, XL.literalForm, Literal("same", lang="en")))
        .add((child, XL.literalForm, Literal("same", lang="EN")))
        .add((EX.First, EX.related, child))
        .add((EX.Second, XL.literalForm, Literal("same", lang="en")))
        .add((EX.Second, EX.related, child))
    )
    view = inspect_vocabulary(source)
    source.remove((None, None, None))
    policy = XLRelationPolicy(str(EX.related), form_relation="same")
    selected = check_xl_relations(view, [policy])
    assert selected.report.verified and len(selected.items) == 2
    assert all(
        item.source is not None
        and item.target is not None
        and item.source.resource != item.target.resource
        for item in selected.items
    )
    assert selected.items[0].target is selected.items[1].target
    assert not check_xl_relations(
        view, [replace(policy, form_relation="different")]
    ).report.verified
    assert (
        check_xl_relations(
            view, [replace(policy, source_language=ABSENT)]
        ).report.outcome
        == "disagreement"
    )
    ordinary = Graph().add((EX.First, EX.related, EX.Second))
    ordinary.add((EX.First, XL.literalForm, Literal("same")))
    ordinary.add((EX.Second, XL.literalForm, Literal("same", datatype=XSD.string)))
    assert check_xl_relations(
        inspect_vocabulary(ordinary),
        [replace(policy, source_language=ABSENT, target_language=ABSENT)],
    ).report.verified


def test_endpoint_failures_unknowns_and_forbidden_relations_are_independent() -> None:
    policy = XLRelationPolicy(str(EX.acronym))
    for endpoint, expected in (
        (Literal("not a label"), "skos.xl_relation.endpoint_not_resource"),
        (EX.Missing, "skos.xl_form_unresolved"),
    ):
        source = labels()
        source.remove((EX.Expansion, EX.acronym, None))
        source.add((EX.Expansion, EX.acronym, endpoint))
        result = check_xl_relations(inspect_vocabulary(source), [policy])
        assert not result.report.verified
        assert any(issue.code == expected for issue in result.report.issues)
        forbidden = check_xl_relations(
            inspect_vocabulary(source), [replace(policy, forbidden=True)]
        )
        assert any(
            i.code == "skos.xl_relation.forbidden" for i in forbidden.report.issues
        )
        assert any(i.code == expected for i in forbidden.report.issues)
    for triple, code in (
        (
            (EX.Acronym, XL.literalForm, Literal("other", lang="en")),
            "skos.xl_relation.form_conflict",
        ),
        ((EX.Acronym, XL.literalForm, EX.NotLiteral), "skos.xl_relation.form_shape"),
        ((EX.Acronym, RDF.type, SKOS.Concept), "skos.xl_relation.role_conflict"),
    ):
        result = check_xl_relations(inspect_vocabulary(labels().add(triple)), [policy])
        assert any(issue.code == code for issue in result.report.issues)


def test_selected_scopes_inventory_ownership_and_runtime_validation() -> None:
    predicates = [str(EX.acronym)]
    policy = XLRelationPolicy(str(XL.labelRelation), subrelations=predicates)  # type: ignore[arg-type]
    predicates.clear()
    assert policy.subrelations == (str(EX.acronym),)
    with pytest.raises(FrozenInstanceError):
        policy.forbidden = True  # type: ignore[misc]
    invalid: tuple[Any, ...] = (None, "predicates", iter(()), [{}], [BNode("label")])
    for value in invalid:
        with pytest.raises(InputError):
            XLRelationPolicy(str(XL.labelRelation), subrelations=value)
    for field, value in (
        ("predicate", Literal(str(EX.acronym))),
        ("source_language", 1),
        ("forbidden", "yes"),
        ("direction", "symmetric"),
        ("form_relation", "casefold"),
    ):
        with pytest.raises(InputError):
            kwargs: dict[str, Any] = {"predicate": str(EX.acronym), field: value}
            XLRelationPolicy(**kwargs)
    view = inspect_vocabulary(labels())
    inventories: tuple[Any, ...] = (None, "policies", iter(()), [{}], [])
    for values in inventories:
        with pytest.raises(InputError):
            check_xl_relations(view, values)
    with pytest.raises(InputError):
        check_xl_relations(view, [policy, XLRelationPolicy(str(EX.acronym))])
    local = Graph().add((EX.Expansion, EX.acronym, EX.Acronym))
    lookup = labels()
    vocabulary = Vocabulary(local, lookup=(lookup,))
    assert (
        check_xl_relations(inspect_vocabulary(vocabulary), [policy]).report.outcome
        == "unverified"
    )
    assert check_xl_relations(
        inspect_vocabulary(vocabulary, scopes=("local", "lookup")), [policy]
    ).report.verified


def test_limits_retain_known_forbidden_truth_without_fabricating_missing_forms() -> (
    None
):
    source = labels()
    policy = XLRelationPolicy(str(EX.acronym), forbidden=True)
    view = inspect_vocabulary(source)
    retained = False
    for work in (1, 20, 40, 200, 375, 400, 500):
        result = check_xl_relations(
            view, [policy], budget=Budget(Limits(max_work=work))
        )
        assert not any(
            i.code == "skos.xl_form_unresolved" for i in result.report.issues
        )
        if result.report.outcome == "exhausted":
            assert not result.report.complete and not result.report.verified
            retained |= any(
                i.code == "skos.xl_relation.forbidden" for i in result.report.issues
            )
    assert retained
    for limits in (
        Limits(max_issues=1),
        Limits(max_total_bytes=20),
        Limits(max_states=2),
    ):
        result = check_xl_relations(
            view, [replace(policy, source_language="fr")], budget=Budget(limits)
        )
        assert result.report.outcome == "exhausted" and not result.report.complete


@pytest.mark.parametrize("later_count", [0, 3])
def test_changing_inventories_keep_one_bounded_capture(later_count: int) -> None:
    class Changing(Sequence[Any]):
        def __init__(self, value: Any) -> None:
            self.value = value
            self.calls = 0
            self.reads: list[int] = []

        def __len__(self) -> int:
            self.calls += 1
            return 1 if self.calls == 1 else later_count

        def __getitem__(self, index: int | slice) -> Any:
            if isinstance(index, slice):
                raise IndexError(index)
            self.reads.append(index)
            return self.value

    predicates = Changing(str(EX.acronym))
    policy = XLRelationPolicy(
        str(XL.labelRelation), subrelations=cast(Any, predicates), forbidden=True
    )
    assert predicates.calls == 1 and predicates.reads == [0]
    assert policy.subrelations == (str(EX.acronym),)
    policies = Changing(policy)
    result = check_xl_relations(inspect_vocabulary(labels()), policies)
    assert policies.calls == 1 and policies.reads == [0]
    assert any(i.code == "skos.xl_relation.forbidden" for i in result.report.issues)
    limited = Changing(policy)
    bounded = check_xl_relations(
        inspect_vocabulary(labels()), limited, budget=Budget(Limits(max_states=2))
    )
    assert limited.calls == 1 and limited.reads == [0]
    assert not bounded.report.complete
