"""Actual native postconditions reject repairs that merely move a violation."""

from collections.abc import Callable

import pytest
from rdflib import Graph, Namespace, URIRef
from rdflib.namespace import RDF, SH

from bijux_ontology import prepare_source
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.shacl.findings import ShaclFinding
from bijux_ontology.shacl.remediation import RemediationStrategy, propose_remediations

EX = Namespace("https://example.org/repair/")


def sources() -> tuple[PreparedSource, PreparedSource]:
    """A declared target with a controlled value and an independent type rule."""
    return (
        prepare_source(
            "@prefix e:<https://example.org/repair/>. "
            "e:A a e:Record; e:p e:Old. e:Old a e:Allowed.",
            format="turtle",
        ),
        prepare_source(
            "@prefix e:<https://example.org/repair/>. "
            "@prefix sh:<http://www.w3.org/ns/shacl#>. "
            "e:S a sh:NodeShape; sh:targetClass e:Record; sh:property e:P. "
            "e:P sh:path e:p; sh:in (e:Good); sh:minCount 1; sh:class e:Allowed. "
            "e:Good a e:Allowed.",
            format="turtle",
        ),
    )


def replacement(
    target: URIRef, *, add_type: bool = True
) -> Callable[[PreparedSource, tuple[ShaclFinding, ...]], PreparedSource | None]:
    """Ordinary application content supplies actual graph changes, never mocks."""

    def propose(
        source: PreparedSource, findings: tuple[ShaclFinding, ...]
    ) -> PreparedSource:
        graph = source.graph()
        assert findings and findings[0].focus_node == EX.A
        graph.remove((EX.A, EX.p, None))
        graph.add((EX.A, EX.p, target))
        if add_type:
            graph.add((target, RDF.type, EX.Allowed))
        return prepare_source(graph)

    return propose


def strategy(
    name: str, target: URIRef, *, add_type: bool = True
) -> RemediationStrategy:
    """Bind a caller policy to the actual named property-shape/component identity."""
    return RemediationStrategy(
        name, EX.P, SH.InConstraintComponent, replacement(target, add_type=add_type)
    )


def test_complete_postconditions_and_independent_alternatives() -> None:
    pytest.importorskip("pyshacl")
    data, shapes = sources()
    original = set(data.graph())
    result = propose_remediations(
        data,
        shapes,
        (
            strategy("approved", EX.Good),
            strategy("missing-class", EX.Good, add_type=False),
        ),
    )
    assert result.baseline is not None and result.baseline.conforms is False
    assert len(result.alternatives) == 2
    good, bad = (a.candidate for a in result.alternatives)
    assert good.report.verified and good.patch is not None and good.source is not None
    assert (EX.A, EX.p, EX.Good) in good.source.graph()
    assert good.patch.removals and good.patch.additions
    assert not bad.report.verified
    assert not result.unresolved
    assert any(i.code == "shacl.nonconformance" for i in bad.report.issues)
    assert set(data.graph()) == original and (EX.A, EX.p, EX.Old) in data.graph()


def test_missing_values_without_provider_stay_unresolved() -> None:
    pytest.importorskip("pyshacl")
    _, shapes = sources()
    data = prepare_source(Graph().add((EX.A, RDF.type, EX.Record)))
    absent = propose_remediations(data, shapes, ())
    assert absent.unresolved and not absent.report.verified and not absent.alternatives
    explicit = RemediationStrategy(
        "no-invented-content",
        EX.P,
        SH.MinCountConstraintComponent,
        lambda source, findings: None,
    )
    result = propose_remediations(data, shapes, (explicit,))
    assert result.unresolved and not result.alternatives and not result.report.verified
    provider = RemediationStrategy(
        "explicit-provider",
        EX.P,
        SH.MinCountConstraintComponent,
        replacement(EX.Good),
    )
    supplied = propose_remediations(data, shapes, (provider,))
    assert supplied.alternatives[0].candidate.report.verified
    assert not supplied.unresolved and not list(data.graph().objects(EX.A, EX.p))


def test_deleting_the_original_target_cannot_evade_recheck() -> None:
    pytest.importorskip("pyshacl")
    data, shapes = sources()

    def erase(
        source: PreparedSource, findings: tuple[ShaclFinding, ...]
    ) -> PreparedSource:
        return prepare_source(Graph())

    result = propose_remediations(
        data,
        shapes,
        (RemediationStrategy("erase-target", EX.P, SH.InConstraintComponent, erase),),
    )
    assert len(result.alternatives) == 1
    candidate = result.alternatives[0].candidate
    assert not candidate.report.verified
    assert any("coverage" in i.code for i in candidate.report.issues)
    empty = propose_remediations(
        prepare_source(Graph()), shapes, (strategy("unused", EX.Good),)
    )
    assert not empty.report.verified and not empty.alternatives


def test_cumulative_exhaustion_and_exact_strategy_identity() -> None:
    pytest.importorskip("pyshacl")
    data, shapes = sources()
    selected = strategy("approved", EX.Good)
    invalid = propose_remediations(data, shapes, (selected, selected))
    assert invalid.report.outcome == "input_error"
    bounded = propose_remediations(
        data, shapes, (selected,), budget=Budget(Limits(max_work=1))
    )
    assert bounded.report.outcome == "exhausted" and not bounded.alternatives
    other = RemediationStrategy(
        "unrelated", EX.Other, SH.InConstraintComponent, replacement(EX.Good)
    )
    result = propose_remediations(data, shapes, (other,))
    assert not result.alternatives and result.unresolved and not result.report.verified
