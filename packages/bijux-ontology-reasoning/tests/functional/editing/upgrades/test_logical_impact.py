"""Native finite upgrade witnesses preserve prior truth and consumer identity."""

import os
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from bijux_ontology_reasoning.axioms.types import (
    DataProperty,
    NamedEntity,
    ObjectProperty,
    SubClassOf,
)
from bijux_ontology_reasoning.backend import OwlProfileBackend, prepare_profile_backend
from bijux_ontology_reasoning.documents import OwlDocument, prepare_owl_document
from bijux_ontology_reasoning.editing.upgrades import (
    EntailmentContract,
    LogicalSnapshot,
    LogicalUpgrade,
    ModelContract,
    SatisfiabilityContract,
    SeparationContract,
    assess_logical_upgrade,
)

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.execution.limits import Budget, Limits

A, B, C, D = (NamedEntity("class", "urn:" + name) for name in ("A", "B", "C", "D"))
DECLARATIONS = """
Declaration(Class(<urn:A>)) Declaration(Class(<urn:B>))
Declaration(Class(<urn:C>)) Declaration(Class(<urn:D>))
Declaration(ObjectProperty(<urn:p>)) Declaration(ObjectProperty(<urn:q>))
Declaration(DataProperty(<urn:d>)) Declaration(DataProperty(<urn:e>))
"""


def source(body: str = "", identity: str = "urn:upgrade") -> OwlDocument:
    """Keep native original syntax, ontology identity and resolution base explicit."""
    return prepare_owl_document(
        "Ontology(<" + identity + "> " + DECLARATIONS + body + ")",
        format="functional",
        document_iri=identity,
    )


@pytest.fixture
def backend(tmp_path: Path) -> OwlProfileBackend:
    """Use the explicit pinned maintained backend rather than synthetic results."""
    java, bundle, pin = (
        os.environ.get(name)
        for name in ("BIJUX_OWL_JAVA", "BIJUX_OWL_BUNDLE", "BIJUX_OWL_MANIFEST_SHA256")
    )
    if not (java and bundle and pin):
        pytest.skip("An explicit pinned native backend is not configured")
    return prepare_profile_backend(
        Path(java), Path(bundle), manifest_sha256=pin, artifacts=tmp_path / "native"
    )


def transitions(result: LogicalUpgrade) -> dict[str, str]:
    """Inspect explicit stable consumer identities, never diagnostic prose."""
    return {item.identifier: item.kind for item in result.transitions}


def test_indirect_loss_prior_failure_and_actual_before_support(
    backend: OwlProfileBackend,
) -> None:
    """A retained declared term can lose an indirect consequence independently."""
    before = source("SubClassOf(<urn:A> <urn:B>) SubClassOf(<urn:B> <urn:C>)")
    after = source("SubClassOf(<urn:A> <urn:B>)")
    consumer = LogicalSnapshot(
        entailments=(
            EntailmentContract("uses-indirect", SubClassOf(A, C, ()), "payload.type"),
            EntailmentContract("prior-failure", SubClassOf(D, C, ())),
        )
    )
    result = assess_logical_upgrade(
        backend,
        before,
        after,
        before_consumer=consumer,
        after_consumer=consumer,
        explain=("uses-indirect",),
        timeout=120,
    )
    assert transitions(result) == {
        "ontology-consistency": "preserved",
        "uses-indirect": "regression",
        "prior-failure": "persistent_prior_failure",
    }
    assert result.compatible is False and result.report.complete
    assert result.explanations[0].side == "before"
    assert result.explanations[0].check.justification is not None
    assert result.explanations[0].check.justification.reproduced
    assert b"Declaration(Class(<urn:C>))" in after.content
    assert result.before is not None and result.after is not None
    assert result.before.source.content == before.content
    assert result.after.source.content == after.content
    assert any(issue.code == "owl.not_entailed" for issue in result.report)
    persistent = assess_logical_upgrade(
        backend,
        after,
        after,
        before_consumer=consumer,
        after_consumer=consumer,
        timeout=120,
    )
    assert persistent.compatible is True and persistent.report.outcome == "disagreement"
    assert all(item.kind != "regression" for item in persistent.transitions)


def test_separation_uses_role_groups_aliases_and_actual_equivalence(
    backend: OwlProfileBackend,
) -> None:
    """Equivalent names inside one alias group and unrelated nodes are permitted."""
    old = source("EquivalentClasses(<urn:A> <urn:B>)")
    current = source(
        "EquivalentClasses(<urn:A> <urn:B> <urn:C> <urn:D>) "
        "EquivalentObjectProperties(<urn:p> <urn:q>) "
        "EquivalentDataProperties(<urn:d> <urn:e>)"
    )
    snapshot = LogicalSnapshot(
        separate=(
            SeparationContract("classes", ((A, B), (C,))),
            SeparationContract(
                "objects", ((ObjectProperty("urn:p"),), (ObjectProperty("urn:q"),))
            ),
            SeparationContract(
                "data", ((DataProperty("urn:d"),), (DataProperty("urn:e"),))
            ),
        )
    )
    result = assess_logical_upgrade(
        backend,
        old,
        current,
        before_consumer=snapshot,
        after_consumer=snapshot,
        timeout=120,
    )
    assert result.compatible is False and result.report.complete
    assert all(
        transitions(result)[name] == "regression"
        for name in ("classes", "objects", "data")
    )
    assert transitions(result)["ontology-consistency"] == "preserved"
    allowed = LogicalSnapshot(
        separate=(SeparationContract("aliases", ((A, B, C), (D,))),)
    )
    safe = source("EquivalentClasses(<urn:A> <urn:B> <urn:C>)")
    replay = assess_logical_upgrade(
        backend, old, safe, before_consumer=allowed, after_consumer=allowed, timeout=120
    )
    assert replay.compatible is True and replay.report.verified


def test_model_snapshots_and_satisfiability_are_distinct_obligations(
    backend: OwlProfileBackend,
) -> None:
    """Possibility alone cannot establish the automatic anonymous domain contract."""
    original = source(
        "ObjectPropertyDomain(<urn:p> <urn:B>) SubClassOf(<urn:A> <urn:B>)"
    )
    changed = source(
        "ObjectPropertyDomain(<urn:p> <urn:C>) SubClassOf(<urn:A> <urn:B>)"
    )
    field = FieldDeclaration(
        "p", "urn:p", (ValueDeclaration("iri", types=("urn:D",)),), location="payload.p"
    )
    model = ModelDeclaration("sample", ("urn:A",), (field,))
    snapshot = LogicalSnapshot(
        models=(ModelContract("sample-consumer", (model,)),),
        satisfiability=(SatisfiabilityContract("possible", "urn:A"),),
    )
    result = assess_logical_upgrade(
        backend,
        original,
        changed,
        before_consumer=snapshot,
        after_consumer=snapshot,
        timeout=120,
    )
    assert transitions(result)["sample-consumer"] == "regression"
    assert transitions(result)["possible"] == "preserved" and result.compatible is False
    new_snapshot = replace(
        snapshot,
        models=(ModelContract("sample-consumer", (replace(model, types=("urn:C",)),)),),
    )
    revised = assess_logical_upgrade(
        backend,
        original,
        changed,
        before_consumer=snapshot,
        after_consumer=new_snapshot,
        timeout=120,
    )
    assert revised.compatible is True
    assert (
        next(
            item for item in revised.transitions if item.identifier == "sample-consumer"
        ).contract_changed
        is True
    )


def test_consistency_unsatisfiability_and_missing_baseline_stay_separate(
    backend: OwlProfileBackend,
) -> None:
    """A new unsatisfiable selected class is not global inconsistency."""
    old = source()
    impossible = source(
        "SubClassOf(<urn:A> <urn:B>) SubClassOf(<urn:A> <urn:C>) "
        "DisjointClasses(<urn:B> <urn:C>)"
    )
    consumer = LogicalSnapshot(
        satisfiability=(SatisfiabilityContract("possible", "urn:A"),)
    )
    result = assess_logical_upgrade(
        backend,
        old,
        impossible,
        before_consumer=consumer,
        after_consumer=consumer,
        explain=("possible",),
        timeout=120,
    )
    assert transitions(result) == {
        "ontology-consistency": "preserved",
        "possible": "regression",
    }
    assert result.compatible is False and result.explanations[0].side == "after"
    inconsistent = source(
        "ClassAssertion(<http://www.w3.org/2002/07/owl#Nothing> <urn:i>)"
    )
    failed = assess_logical_upgrade(
        backend,
        old,
        inconsistent,
        before_consumer=consumer,
        after_consumer=consumer,
        timeout=120,
    )
    assert transitions(failed)["ontology-consistency"] == "regression"
    assert transitions(failed)["possible"] == "unknown" and failed.compatible is False
    open_result = assess_logical_upgrade(
        backend, None, old, before_consumer=None, after_consumer=consumer
    )
    assert open_result.compatible is None and all(
        item.kind == "unknown" for item in open_result.transitions
    )


def test_unknown_explanation_cumulative_stop_and_plain_input_guards(
    backend: OwlProfileBackend,
) -> None:
    """Unresolved support and stopped work cannot erase an established regression."""
    old, new = source("SubClassOf(<urn:A> <urn:B>)"), source()
    snapshot = LogicalSnapshot(
        entailments=(EntailmentContract("uses", SubClassOf(A, B, ())),)
    )
    result = assess_logical_upgrade(
        backend,
        old,
        new,
        before_consumer=snapshot,
        after_consumer=snapshot,
        explain=("absent",),
        timeout=120,
    )
    assert result.compatible is False and not result.report.complete
    assert transitions(result)["uses"] == "regression"
    tiny = Budget(Limits(max_total_bytes=128))
    stopped = assess_logical_upgrade(
        backend,
        old,
        new,
        before_consumer=snapshot,
        after_consumer=snapshot,
        budget=tiny,
    )
    assert stopped.compatible is None and stopped.report.outcome == "exhausted"
    cancelled = Event()
    cancelled.set()
    assert (
        assess_logical_upgrade(
            backend,
            old,
            new,
            before_consumer=snapshot,
            after_consumer=snapshot,
            cancel=cancelled,
        ).compatible
        is None
    )
    duplicates = replace(
        snapshot, satisfiability=(SatisfiabilityContract("uses", "urn:A"),)
    )
    invalid = assess_logical_upgrade(
        backend, old, new, before_consumer=duplicates, after_consumer=snapshot
    )
    assert invalid.report.outcome == "input_error" and invalid.compatible is None
