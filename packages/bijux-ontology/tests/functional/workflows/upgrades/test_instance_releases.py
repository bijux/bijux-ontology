"""Instance release rules operate on actual records independently of schema changes."""

import threading
from dataclasses import replace
from typing import cast

from rdflib import BNode, Namespace, URIRef

from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.patches import BlankNodePair, apply_rdf_patch
from bijux_ontology.rdf.records.types import PatchLiteral
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.workflows.upgrades.instances import (
    InstanceEntity,
    InstanceEntityPair,
    InstanceFieldPolicy,
    InstanceRelease,
    compare_instance_releases,
)

EX = Namespace("urn:instance-release:")


def _release(body: str, *subjects: URIRef) -> InstanceRelease:
    source = prepare_rdf(
        "@prefix e: <urn:instance-release:>. "
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#>. " + body,
        format="turtle",
    )
    return InstanceRelease(
        source, tuple(InstanceEntity(subject) for subject in subjects)
    )


def _blank(source: PreparedSource, subject: URIRef, predicate: URIRef) -> BNode:
    node = source.graph().value(subject, predicate)
    assert isinstance(node, BNode)
    return node


def test_allowed_label_and_forbidden_measurement_retain_actual_delta() -> None:
    before = _release('e:A e:label "old"; e:measurement "10"^^xsd:integer.', EX.A)
    after = _release('e:A e:label "new"; e:measurement "11"^^xsd:integer.', EX.A)
    original = frozenset(before.source.graph())
    result = compare_instance_releases(
        before,
        after,
        fields=(
            InstanceFieldPolicy("label", EX.label, change="allow"),
            InstanceFieldPolicy("measurement", EX.measurement),
        ),
    )
    assert result.report.outcome == "disagreement", result.report.to_dict()
    assert [field.accepted for field in result.fields] == [True, False]
    assert result.patch is not None and len(result.patch.removals) == 2
    assert len(result.patch.additions) == 2
    applied = apply_rdf_patch(before.source, result.patch)
    assert (
        applied.source is not None
        and compare_rdf(applied.source, after.source).verified
    )
    assert frozenset(before.source.graph()) == original


def test_value_qualified_equality_preserves_lexical_delta_and_unknowns() -> None:
    before = _release('e:A e:value "01"^^xsd:integer.', EX.A)
    after = _release('e:A e:value "1"^^xsd:integer.', EX.A)
    exact = compare_instance_releases(
        before, after, fields=(InstanceFieldPolicy("value", EX.value),)
    )
    qualified = compare_instance_releases(
        before,
        after,
        fields=(InstanceFieldPolicy("value", EX.value, equality="sparql-1.1"),),
    )
    assert exact.report.outcome == "disagreement"
    assert qualified.report.verified and qualified.fields[0].exact_equal is False
    assert qualified.fields[0].comparisons[0].relation == "equal"
    assert qualified.patch is not None
    assert {
        q.value.lexical
        for q in qualified.patch.removals
        if isinstance(q.value, PatchLiteral)
    } == {"01"}
    nan = _release('e:A e:value "NaN"^^xsd:double.', EX.A)
    unknown = compare_instance_releases(
        nan,
        nan,
        fields=(InstanceFieldPolicy("value", EX.value, equality="sparql-1.1"),),
    )
    assert unknown.report.outcome == "unverified" and unknown.fields[0].accepted is None
    many_before = _release('e:A e:value "01"^^xsd:integer, "2"^^xsd:integer.', EX.A)
    many_after = _release('e:A e:value "2"^^xsd:integer, "1"^^xsd:integer.', EX.A)
    assert compare_instance_releases(
        many_before,
        many_after,
        fields=(InstanceFieldPolicy("value", EX.value, equality="owl2-datatype-map"),),
    ).report.verified


def test_supported_set_witnesses_do_not_require_cross_family_comparisons() -> None:
    before = _release('e:A e:value "01"^^xsd:integer, "true"^^xsd:boolean.', EX.A)
    after = _release('e:A e:value "1"^^xsd:integer, "1"^^xsd:boolean.', EX.A)
    result = compare_instance_releases(
        before,
        after,
        fields=(InstanceFieldPolicy("values", EX.value, equality="sparql-1.1"),),
    )
    assert result.report.verified and result.fields[0].accepted is True
    assert result.patch is not None and result.patch.removals
    assert result.fields[0].exact_equal is False
    changed = _release('e:A e:value "1"^^xsd:integer, "false"^^xsd:boolean.', EX.A)
    unknown = compare_instance_releases(
        before,
        changed,
        fields=(InstanceFieldPolicy("values", EX.value, equality="sparql-1.1"),),
    )
    assert not unknown.report.verified and unknown.fields[0].accepted is not True


def test_named_identity_is_explicit_and_blank_identity_never_uses_labels() -> None:
    before = _release('e:A e:label "same".', EX.A)
    after = _release('e:B e:label "same".', EX.B)
    renamed = compare_instance_releases(before, after)
    assert renamed.report.verified
    assert (
        renamed.removed_entities == before.entities
        and renamed.added_entities == after.entities
    )
    mapped = compare_instance_releases(
        before,
        after,
        entity_pairs=(InstanceEntityPair(before.entities[0], after.entities[0]),),
        fields=(InstanceFieldPolicy("label", EX.label),),
    )
    assert (
        mapped.report.verified
        and not mapped.added_entities
        and not mapped.removed_entities
    )
    old = _release('e:A e:child _:old; e:mirror _:old. _:old e:label "same".', EX.A)
    new = _release('e:A e:child _:new; e:mirror _:new. _:new e:label "same".', EX.A)
    a, b = _blank(old.source, EX.A, EX.child), _blank(new.source, EX.A, EX.child)
    stable = compare_instance_releases(
        old,
        new,
        blank_pairs=(BlankNodePair(a, b),),
        fields=(InstanceFieldPolicy("child", EX.child),),
        append_only=True,
    )
    assert (
        stable.report.verified
        and stable.patch is not None
        and not stable.patch.additions
    )
    automatic = compare_instance_releases(
        old,
        new,
        fields=(
            InstanceFieldPolicy("child", EX.child),
            InstanceFieldPolicy("mirror", EX.mirror),
        ),
    )
    assert automatic.report.verified
    assert all(field.exact_equal and field.accepted for field in automatic.fields)
    symmetric = _release(
        'e:A e:child _:x, _:y. _:x e:label "same". _:y e:label "same".', EX.A
    )
    altered = _release(
        'e:A e:child _:x, _:y. _:x e:label "changed". _:y e:label "same".', EX.A
    )
    unknown = compare_instance_releases(
        symmetric, altered, fields=(InstanceFieldPolicy("child", EX.child),)
    )
    assert unknown.report.outcome == "unverified" and unknown.patch is None
    anonymous_before = InstanceRelease(old.source, (InstanceEntity(a),))
    anonymous_after = InstanceRelease(new.source, (InstanceEntity(b),))
    assert (
        compare_instance_releases(anonymous_before, anonymous_after).report.outcome
        == "unverified"
    )
    assert compare_instance_releases(
        anonymous_before, anonymous_after, blank_pairs=(BlankNodePair(a, b),)
    ).report.verified
    foreign = InstanceRelease(old.source, (InstanceEntity(BNode(str(a))),))
    assert (
        compare_instance_releases(foreign, anonymous_after).report.outcome
        == "unverified"
    )


def test_list_order_and_shared_child_split_retain_joint_blank_witnesses() -> None:
    before = _release(
        "e:A e:list (e:one e:two); e:child _:shared. "
        'e:B e:child _:shared. _:shared e:value "same".',
        EX.A,
        EX.B,
    )
    after = _release(
        "e:A e:list (e:two e:one); e:child _:left. e:B e:child _:right. "
        '_:left e:value "same". _:right e:value "same".',
        EX.A,
        EX.B,
    )
    a = _blank(before.source, EX.A, EX.list)
    b = _blank(after.source, EX.A, EX.list)
    rest = URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#rest")
    a_tail = cast(BNode, before.source.graph().value(a, rest))
    b_tail = cast(BNode, after.source.graph().value(b, rest))
    shared = _blank(before.source, EX.A, EX.child)
    left = _blank(after.source, EX.A, EX.child)
    right = _blank(after.source, EX.B, EX.child)
    result = compare_instance_releases(
        before,
        after,
        fields=(
            InstanceFieldPolicy("list", EX.list, collection="list"),
            InstanceFieldPolicy("child", EX.child),
        ),
        blank_pairs=(
            BlankNodePair(a, b),
            BlankNodePair(a_tail, b_tail),
            BlankNodePair(shared, left),
        ),
        created_blanks=(right,),
    )
    assert result.patch is not None and result.report.outcome == "disagreement", (
        result.report.to_dict()
    )
    first = result.fields[0]
    assert first.before_values == (EX.one, EX.two) and first.after_values == (
        EX.two,
        EX.one,
    )
    assert first.accepted is False
    assert any(
        len(change.before_incoming) == 2 and len(change.after_incoming) == 1
        for change in result.shared_nodes
    )
    assert result.fields[3].accepted is False
    protected = compare_instance_releases(
        before,
        after,
        blank_pairs=(
            BlankNodePair(a, b),
            BlankNodePair(a_tail, b_tail),
            BlankNodePair(shared, left),
        ),
        created_blanks=(right,),
        append_only=True,
    )
    assert protected.report.outcome == "disagreement"


def test_append_only_protects_records_and_blank_support() -> None:
    before = _release(
        'e:A e:label "old"; e:child e:Named. e:Named e:value "old".', EX.A
    )
    added = _release(
        'e:A e:label "old"; e:child e:Named. e:Named e:value "new". e:B e:label "new".',
        EX.A,
        EX.B,
    )
    result = compare_instance_releases(before, added, append_only=True)
    assert result.report.verified and result.added_entities == (added.entities[1],)
    assert (
        compare_instance_releases(
            before,
            added,
            fields=(InstanceFieldPolicy("child-value", EX.child / EX.value),),
        ).report.outcome
        == "disagreement"
    )
    changed = _release('e:A e:label "changed"; e:child e:Named.', EX.A)
    assert (
        compare_instance_releases(before, changed, append_only=True).report.outcome
        == "disagreement"
    )
    removed = _release('e:B e:label "new".', EX.B)
    assert (
        compare_instance_releases(before, removed, append_only=True).report.outcome
        == "disagreement"
    )
    blank_before = _release('e:A e:child _:x. _:x e:value "old".', EX.A)
    blank_after = _release('e:A e:child _:y. _:y e:value "new".', EX.A)
    pair = BlankNodePair(
        _blank(blank_before.source, EX.A, EX.child),
        _blank(blank_after.source, EX.A, EX.child),
    )
    assert (
        compare_instance_releases(
            blank_before, blank_after, blank_pairs=(pair,), append_only=True
        ).report.outcome
        == "disagreement"
    )


def test_physical_graph_inverse_scope_and_unsupported_path_are_explicit() -> None:
    old = prepare_rdf(
        '@prefix e:<urn:instance-release:>. e:G { e:A e:value "old". '
        'e:Owner e:record e:A. } e:H { e:A e:value "hidden". }',
        format="trig",
    )
    new = prepare_rdf(
        '@prefix e:<urn:instance-release:>. e:G { e:A e:value "new". '
        'e:Owner e:record e:A. } e:H { e:A e:value "hidden". }',
        format="trig",
    )
    before = InstanceRelease(old, (InstanceEntity(EX.A, str(EX.G)),))
    after = InstanceRelease(new, (InstanceEntity(EX.A, str(EX.G)),))
    result = compare_instance_releases(
        before,
        after,
        fields=(
            InstanceFieldPolicy("value", EX.value),
            InstanceFieldPolicy("owner", ~EX.record),
        ),
    )
    assert result.report.outcome == "disagreement" and result.patch is not None
    assert result.fields[0].before_values == (
        PatchLiteral("old", "http://www.w3.org/2001/XMLSchema#string"),
    )
    assert result.fields[1].accepted is True
    assert any(
        q.subject == EX.Owner and q.predicate == EX.record
        for q in result.fields[1].before_support
    )
    unselected = replace(before, entities=(InstanceEntity(EX.A),))
    assert compare_instance_releases(unselected, after).report.outcome == "unverified"
    unsupported = compare_instance_releases(
        before,
        after,
        fields=(
            InstanceFieldPolicy("alternative", EX.value | EX.other, change="allow"),
        ),
    )
    assert (
        unsupported.report.outcome == "unverified"
        and unsupported.fields[0].accepted is None
    )
    absent = replace(before, entities=(InstanceEntity(EX.Missing, str(EX.G)),))
    assert compare_instance_releases(absent, after).report.outcome == "unverified"
    blank_old = prepare_rdf(
        '@prefix e:<urn:instance-release:>. _:g { e:A e:value "same". }', format="trig"
    )
    blank_new = prepare_rdf(
        '@prefix e:<urn:instance-release:>. _:h { e:A e:value "same". }', format="trig"
    )
    graph_old = next(
        name for name in blank_old._graph_names() if isinstance(name, BNode)
    )
    graph_new = next(
        name for name in blank_new._graph_names() if isinstance(name, BNode)
    )
    old_release = InstanceRelease(blank_old, (InstanceEntity(EX.A, graph_old),))
    new_release = InstanceRelease(blank_new, (InstanceEntity(EX.A, graph_new),))
    assert (
        compare_instance_releases(old_release, new_release).report.outcome
        == "unverified"
    )
    assert compare_instance_releases(
        old_release, new_release, blank_pairs=(BlankNodePair(graph_old, graph_new),)
    ).report.verified


def test_budget_and_cancel_never_turn_unchecked_policy_into_acceptance() -> None:
    before = _release('e:A e:value "1"^^xsd:integer.', EX.A)
    after = _release('e:A e:value "2"^^xsd:integer.', EX.A)
    fields = (InstanceFieldPolicy("value", EX.value),)
    for limits in (
        Limits(max_work=1),
        Limits(max_states=1),
        Limits(max_total_bytes=64),
    ):
        budget = Budget(limits)
        result = compare_instance_releases(before, after, fields=fields, budget=budget)
        assert result.report.outcome == "exhausted"
    canceled = threading.Event()
    canceled.set()
    assert (
        compare_instance_releases(
            before, after, fields=fields, cancel=canceled
        ).report.outcome
        == "exhausted"
    )

    # A paid earlier finding remains present when later field work exhausts.
    many = tuple(InstanceFieldPolicy("value-" + str(i), EX.value) for i in range(16))
    baseline = Budget()
    full = compare_instance_releases(before, after, fields=many, budget=baseline)
    assert len(full.fields) == 16
    limits = Limits(max_states=int(baseline._counts["states"] * 0.95))
    paid = Budget(limits)
    late = compare_instance_releases(before, after, fields=many, budget=paid)
    assert late.report.outcome == "exhausted" and late.fields
    assert any(
        issue.code == "instance.field_change_forbidden" for issue in late.report.issues
    )
    assert {id(issue) for issue in paid._retained_issues.values()} <= {
        id(issue) for issue in late.report.issues
    }

    triple_cut = compare_instance_releases(
        before, after, fields=many, limits=Limits(max_triples=9)
    )
    assert triple_cut.report.outcome == "exhausted" and triple_cut.patch is not None
    assert len(triple_cut.fields) == 1
    assert any(
        issue.code == "instance.field_change_forbidden"
        for issue in triple_cut.report.issues
    )
