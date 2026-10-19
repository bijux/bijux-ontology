"""Checked portable dataset patches preserve exact scope and transactional truth."""

import json
import threading
from dataclasses import replace
from typing import cast

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace
from rdflib.namespace import XSD

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.patches import (
    BlankNodePair,
    PatchBlankBinding,
    apply_rdf_patch,
    patch_from_data,
    patch_to_data,
    plan_rdf_patch,
)
from bijux_ontology.rdf.patches.planning import capture_source
from bijux_ontology.rdf.records.materialization import materialize
from bijux_ontology.rdf.records.types import (
    PatchBlank,
    PatchLiteral,
    PatchQuad,
    SnapshotWitness,
)
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.sources.types import RdfSource

EX = Namespace("https://patch.example/")


def test_materialized_bindings_are_exact_fresh_owned_nodes_across_graph_roles() -> None:
    """A native replay binds anonymous selectors without guessing parser labels."""
    original = _dataset("shared", "01", "empty")
    operation = Operation(None, None, 10, None)
    captured = capture_source(original, "source:", operation)
    bindings: dict[str, BNode] = {}
    result = materialize(captured.witness, original, operation, bindings=bindings)
    node = _shared(result)
    assert any(value is node for value in bindings.values())
    assert any(name is node for name in result._graph_names())
    assert node is not _shared(original)
    assert result.graph(str(EX.H)).value(EX.other, EX.child) is node
    assert str(result.graph(node).value(node, EX.value)) == "01"
    exported = result.graph(str(EX.G))
    exported.remove((EX.root, EX.child, None))
    assert result.graph(str(EX.G)).value(EX.root, EX.child) is node
    again: dict[str, BNode] = {}
    materialize(captured.witness, original, operation, bindings=again)
    assert not set(bindings.values()).intersection(again.values())
    with pytest.raises(InputError, match="empty plain dictionary"):
        materialize(captured.witness, original, operation, bindings=bindings)
    incomplete: dict[str, BNode] = {}
    with pytest.raises(LimitExceeded):
        materialize(
            captured.witness,
            original,
            Operation(Limits(max_work=1), None, 10, None),
            bindings=incomplete,
        )
    assert not incomplete
    graph_token = next(
        name for name in captured.witness.graphs if isinstance(name, PatchBlank)
    )
    selected = SnapshotWitness(
        "named",
        graph_token,
        tuple(quad for quad in captured.witness.quads if quad.graph == graph_token),
        (graph_token,),
    )
    scoped: dict[str, BNode] = {}
    named = materialize(selected, original, operation, bindings=scoped)
    assert named.graph_name is scoped[graph_token.token]
    assert str(named.graph().value(named.graph_name, EX.value)) == "01"


def _dataset(label: str, lexical: str, empty: str) -> PreparedSource:
    """Parse independent complete TriG datasets with a blank shared across roles."""
    return prepare_rdf(
        f'''@prefix ex: <{EX}> .
        @prefix xsd: <{XSD}> .
        ex:{empty} {{}}
        ex:G {{ ex:root ex:child _:{label} . }}
        ex:H {{ ex:other ex:child _:{label} . }}
        _:{label} {{ _:{label} ex:value "{lexical}"^^xsd:integer . }}''',
        format="trig",
        require_complete_graphs=True,
    )


def _shared(source: PreparedSource) -> BNode:
    """Obtain actual source-owned term evidence, never reuse a parsed label."""
    node = source.graph(str(EX.G)).value(EX.root, EX.child)
    assert isinstance(node, BNode)
    return node


def test_portable_shared_blank_lexical_and_empty_graph_edits() -> None:
    """A JSON round trip reconstructs the target on a separately parsed base."""
    base, target = _dataset("before", "01", "removed"), _dataset("target", "1", "added")
    original = frozenset(base.dataset().quads())
    plan = plan_rdf_patch(
        base, target, blank_pairs=(BlankNodePair(_shared(base), _shared(target)),)
    )
    assert plan.report.verified and plan.patch is not None
    patch = patch_from_data(json.loads(json.dumps(patch_to_data(plan.patch))))
    assert patch == plan.patch
    assert patch.removed_graphs == (EX.removed,) and patch.added_graphs == (EX.added,)
    assert {
        q.value.lexical for q in patch.removals if isinstance(q.value, PatchLiteral)
    } == {"01"}
    assert {
        q.value.lexical for q in patch.additions if isinstance(q.value, PatchLiteral)
    } == {"1"}
    independent = _dataset("independent", "01", "removed")
    applied = apply_rdf_patch(independent, patch)
    assert applied.report.verified and applied.source is not None
    assert compare_rdf(applied.source, target).verified
    result_node = _shared(applied.source)
    assert result_node in {
        graph.identifier for graph in applied.source.dataset().graphs()
    }
    assert frozenset(base.dataset().quads()) == original
    assert not applied.source.graph(str(EX.added))
    aliases = Graph().add((EX.s, EX.p, Literal("same")))
    aliases.add((EX.s, EX.p, Literal("same", datatype=XSD.string)))
    typed = prepare_rdf(Graph().add((EX.s, EX.p, Literal("same", datatype=XSD.string))))
    unchanged = plan_rdf_patch(prepare_rdf(aliases), typed)
    assert unchanged.patch is not None and unchanged.report.verified
    assert len(unchanged.patch.base.quads) == 1 and not unchanged.patch.removals


def test_cosmetic_and_ground_only_changes_do_not_demand_unused_blank_identity() -> None:
    """Symmetric untouched resources remain legal; affected ambiguity is required."""
    base = prepare_rdf(
        f"""@prefix e:<{EX}>. e:root e:p _:a, _:b.
        _:a e:v "same". _:b e:v "same". e:s e:meta "old".""",
        format="turtle",
    )
    renamed = prepare_rdf(
        f"""@prefix e:<{EX}>. _:x e:v "same". e:s e:meta "old".
        e:root e:p _:y, _:x. _:y e:v "same".""",
        format="turtle",
    )
    cosmetic = plan_rdf_patch(base, renamed)
    assert cosmetic.patch is not None and cosmetic.report.verified
    assert not any(
        (
            cosmetic.patch.removals,
            cosmetic.patch.additions,
            cosmetic.patch.removed_graphs,
            cosmetic.patch.added_graphs,
        )
    )
    ground = prepare_rdf(
        f"""@prefix e:<{EX}>. e:root e:p _:y, _:x.
        _:x e:v "same". _:y e:v "same". e:s e:meta "new".""",
        format="turtle",
    )
    changed = plan_rdf_patch(base, ground)
    assert changed.patch is not None and changed.report.verified
    assert not any(
        isinstance(term, PatchBlank)
        for q in (*changed.patch.removals, *changed.patch.additions)
        for term in (q.subject, q.value, q.graph)
    )
    assert apply_rdf_patch(base, changed.patch).report.verified
    altered = prepare_rdf(
        f"""@prefix e:<{EX}>. e:root e:p _:x, _:y.
        _:x e:v "different". _:y e:v "same". e:s e:meta "old".""",
        format="turtle",
    )
    uncertain = plan_rdf_patch(base, altered)
    assert uncertain.patch is None and uncertain.report.outcome == "unverified"
    before = tuple(cast(BNode, n) for n in base.graph().objects(EX.root, EX.p))
    after_graph = altered.graph()
    after = tuple(cast(BNode, n) for n in after_graph.objects(EX.root, EX.p))
    pairs = tuple(BlankNodePair(a, b) for a, b in zip(before, after, strict=True))
    declared = plan_rdf_patch(base, altered, blank_pairs=pairs)
    assert declared.patch is not None and declared.report.verified
    ambiguous = apply_rdf_patch(base, declared.patch)
    assert ambiguous.source is None and ambiguous.report.outcome == "unverified"
    changed_subject = next(
        q.subject for q in declared.patch.removals if isinstance(q.subject, PatchBlank)
    )
    assert isinstance(changed_subject, PatchBlank)
    chosen = before[0]
    explicit = apply_rdf_patch(
        base,
        declared.patch,
        blank_bindings=(PatchBlankBinding(changed_subject.token, chosen),),
    )
    assert explicit.source is not None and explicit.report.verified
    assert compare_rdf(explicit.source, altered).verified


def test_wrong_base_missing_removal_and_failed_postcondition_publish_no_candidate() -> (
    None
):
    """Every failure leaves the accepted source and existing readers stable."""
    base = prepare_rdf(Graph().add((EX.s, EX.p, EX.old)))
    target = prepare_rdf(Graph().add((EX.s, EX.p, EX.new)))
    plan = plan_rdf_patch(base, target)
    assert plan.patch is not None
    original = frozenset(base.graph())
    wrong = apply_rdf_patch(
        prepare_rdf(Graph().add((EX.s, EX.p, EX.other))), plan.patch
    )
    assert wrong.source is None and wrong.report.outcome == "input_error"
    missing = replace(plan.patch, removals=(PatchQuad(EX.s, EX.p, EX.absent),))
    failed = apply_rdf_patch(base, missing)
    assert failed.source is None and any(
        i.code == "rdf.patch_removal_missing" for i in failed.report.issues
    )
    invalid = apply_rdf_patch(base, replace(plan.patch, additions=()))
    assert invalid.source is None and any(
        i.code == "rdf.patch_postcondition_failed" for i in invalid.report.issues
    )
    assert frozenset(base.graph()) == original
    old_a, old_b, new_a, new_b = (BNode() for _ in range(4))
    old_graph = Graph().add((EX.root, EX.left, old_a)).add((EX.root, EX.right, old_b))
    old_graph.add((old_a, EX.color, EX.red)).add((old_b, EX.color, EX.blue))
    new_graph = Graph().add((EX.root, EX.left, new_a)).add((EX.root, EX.right, new_b))
    new_graph.add((new_a, EX.color, EX.red)).add((new_b, EX.color, EX.blue))
    new_graph.add((new_a, EX.extra, EX.added))
    before, after = prepare_rdf(old_graph), prepare_rdf(new_graph)
    accepted_before = frozenset(before.graph())
    planned = plan_rdf_patch(
        before,
        after,
        blank_pairs=tuple(
            BlankNodePair(
                cast(BNode, before.graph().value(EX.root, predicate)),
                cast(BNode, after.graph().value(EX.root, predicate)),
            )
            for predicate in (EX.left, EX.right)
        ),
    )
    assert planned.patch is not None
    left, right = (
        next(
            q.value.token
            for q in planned.patch.base.quads
            if q.predicate == predicate and isinstance(q.value, PatchBlank)
        )
        for predicate in (EX.left, EX.right)
    )

    def swapped(value: object) -> object:
        """Change target references while leaving the portable edit set intact."""
        if isinstance(value, list):
            if len(value) == 2 and value[0] == "blank":
                return ["blank", {left: right, right: left}.get(value[1], value[1])]
            return [swapped(item) for item in value]
        if isinstance(value, dict):
            return {key: swapped(item) for key, item in value.items()}
        return value

    wire = patch_to_data(planned.patch)
    wire["target"] = swapped(wire["target"])
    rebound = apply_rdf_patch(before, patch_from_data(wire))
    assert rebound.source is None and any(
        i.code == "rdf.patch_postcondition_failed" for i in rebound.report.issues
    )
    assert frozenset(before.graph()) == accepted_before


def test_pinned_context_scope_and_unknown_union_inventory_are_preserved() -> None:
    """Hidden graphs cannot affect a selected patch or enter its output candidate."""
    left, right = Dataset(), Dataset()
    for dataset in (left, right):
        dataset.graph(EX.G).add((EX.s, EX.p, EX.value))
    left.graph(EX.hidden).add((EX.s, EX.p, EX.old))
    right.graph(EX.hidden).add((EX.s, EX.p, EX.new))
    base = prepare_rdf(RdfSource(left, graph_name=str(EX.G)))
    target = prepare_rdf(RdfSource(right, graph_name=str(EX.G)))
    planned = plan_rdf_patch(base, target)
    assert planned.patch is not None and planned.report.verified
    assert (
        planned.patch.base.kind == "named"
        and planned.patch.base.graph_selection == EX.G
    )
    assert not planned.patch.removals and not planned.patch.additions
    applied = apply_rdf_patch(base, planned.patch)
    assert applied.source is not None and applied.report.verified
    assert str(applied.source.graph_name) == str(EX.G)
    assert EX.hidden not in {g.identifier for g in applied.source.dataset().graphs()}
    union = prepare_rdf(RdfSource(left, union=True))
    unknown = plan_rdf_patch(union, union)
    assert unknown.patch is None and unknown.report.outcome == "unverified"
    insufficient = PreparedSource(
        left,
        is_dataset=True,
        document_iri=None,
        effective_url=None,
        literal_fidelity="normalisation_allowed",
        graph_fidelity="complete",
    )
    assert plan_rdf_patch(insufficient, insufficient).patch is None


def test_bounds_cancellation_invalid_wire_inputs_and_fresh_recovery() -> None:
    """Incomplete execution and malformed portability never produce accepted state."""
    base, target = _dataset("a", "01", "removed"), _dataset("b", "1", "added")
    pairs = (BlankNodePair(_shared(base), _shared(target)),)
    for limits in (Limits(max_work=1), Limits(max_bytes=100), Limits(max_states=1)):
        stopped = plan_rdf_patch(base, target, blank_pairs=pairs, limits=limits)
        assert stopped.patch is None and stopped.report.outcome == "exhausted"
    cancellation = threading.Event()
    cancellation.set()
    assert (
        plan_rdf_patch(base, target, cancel=cancellation).report.outcome == "exhausted"
    )
    assert plan_rdf_patch(base, target, timeout=0.000001).report.outcome == "exhausted"
    foreign = plan_rdf_patch(
        base,
        target,
        blank_pairs=(BlankNodePair(BNode(str(_shared(base))), _shared(target)),),
    )
    assert foreign.patch is None and foreign.report.outcome == "unverified"
    planned = plan_rdf_patch(base, target, blank_pairs=pairs)
    assert planned.patch is not None and planned.report.verified
    for value in (None, {}, {**patch_to_data(planned.patch), "version": True}):
        with pytest.raises(InputError):
            patch_from_data(value)
    wire = patch_to_data(planned.patch)
    cast(list[object], wire["additions"]).append([None])
    with pytest.raises(InputError):
        patch_from_data(wire)
    assert apply_rdf_patch(
        _dataset("fresh", "01", "removed"), planned.patch
    ).report.verified
    ground_base = prepare_rdf(Graph().add((EX.s, EX.p, EX.old)))
    ground_target = prepare_rdf(Graph().add((EX.s, EX.p, EX.new)))
    shared = Budget(Limits(max_work=5000))
    assert plan_rdf_patch(ground_base, ground_target, budget=shared).report.verified
    assert shared.remaining("work") < shared.limits.max_work
    shared.consume("work", shared.remaining("work"))
    pending = plan_rdf_patch(ground_base, ground_target, budget=shared)
    assert pending.patch is None and pending.report.outcome == "exhausted"
