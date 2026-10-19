"""Observable release lifecycle policies retain raw supplied facts and uncertainty."""

from dataclasses import replace

import pytest
from rdflib import BNode, Dataset, Literal, URIRef
from rdflib.namespace import OWL, RDFS

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.sources.types import RdfSource
from bijux_ontology.vocabulary.facts.types import FactPredicate
from bijux_ontology.workflows.upgrades.lifecycle import (
    HistoryCoverage,
    LifecyclePolicy,
    Release,
    Tombstone,
    check_lifecycle,
)

TERM = URIRef("urn:term:A")
ROOT = URIRef("urn:term:Root")
RESERVED = (FactPredicate(str(RDFS.subClassOf)),)


def release(label: str, body: str) -> Release:
    """Use actual native RDF preparation, preserving supplied lexical terms."""
    return Release(
        label,
        prepare_source(
            "@prefix owl: <http://www.w3.org/2002/07/owl#> . "
            "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> . "
            "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> . " + body,
            format="turtle",
            base="https://example.org/ontology/" + label,
        ),
    )


def history(*items: Release) -> HistoryCoverage:
    return HistoryCoverage(tuple(item.label for item in items), True)


def test_retirement_reactivation_and_retained_or_changed_logical_assertions() -> None:
    old = release(
        "old",
        "<urn:term:A> a owl:Class; owl:deprecated true; "
        "rdfs:subClassOf <urn:term:Root> .",
    )
    for parent in ("Root", "Different"):
        new = release(
            "new",
            "<urn:term:A> a owl:Class; owl:deprecated false; "
            f"rdfs:subClassOf <urn:term:{parent}> .",
        )
        result = check_lifecycle(
            [old, new],
            [TERM],
            policy=LifecyclePolicy(reserved=RESERVED),
            coverage=history(old, new),
        )
        assert result.report.outcome == "disagreement"
        assert [(e.kind, e.prohibited) for e in result.events] == [
            ("retired", False),
            ("reactivated", True),
            ("reserved_assertion", True),
        ]
        fact = result.events[-1].fact
        assert fact is not None and fact.statement[2] == URIRef("urn:term:" + parent)
        assert fact.occurrences[0].role == "class"
        assert result.observations[0].release is old
        assert result.observations[1].deprecation.state == "active"
        assert len(result.changes) == 1


def test_editorial_changes_are_not_reactivation_and_permission_is_explicit() -> None:
    old = release(
        "old", '<urn:term:A> a owl:Class; owl:deprecated true; rdfs:label "old" .'
    )
    new = release("new", '<urn:term:A> a owl:Class; rdfs:label "corrected" .')
    result = check_lifecycle(
        [old, new], [TERM], policy=LifecyclePolicy(), coverage=history(old, new)
    )
    assert result.report.verified
    assert [e.kind for e in result.events] == ["retired"]
    assert result.observations[-1].deprecation.state == "absent"
    assert any(c.kind == "annotation" for c in result.changes[0].changes)
    active = release("active", "<urn:term:A> a owl:Class; owl:deprecated false .")
    permitted = check_lifecycle(
        [old, active],
        [TERM],
        policy=LifecyclePolicy(reactivation="allow"),
        coverage=history(old, active),
    )
    assert permitted.report.verified
    assert permitted.events[-1].kind == "reactivated"
    assert not permitted.events[-1].prohibited


def test_tombstones_reappearance_and_open_history_preserve_known_findings() -> None:
    old = release("old", "")
    middle = release("absent", "")
    new = release("new", "<urn:term:A> a owl:Class; rdfs:subClassOf <urn:term:Root> .")
    supplied = [old, middle, new]
    marker = Tombstone(TERM, "old")
    policy = LifecyclePolicy(reserved=RESERVED, declaration_reactivation=True)
    result = check_lifecycle(
        supplied,
        [TERM],
        policy=policy,
        tombstones=[marker],
        coverage=history(*supplied),
    )
    assert {e.kind for e in result.events} == {
        "retired",
        "reactivated",
        "reserved_assertion",
    }
    assert result.report.outcome == "disagreement"
    for coverage in (
        None,
        HistoryCoverage(("old", "missing", "new"), True),
        HistoryCoverage(("old", "absent", "new"), False),
    ):
        partial = check_lifecycle(
            supplied, [TERM], policy=policy, tombstones=[marker], coverage=coverage
        )
        assert not partial.report.verified
        assert any(i.kind == "unverified" for i in partial.report.issues)
        assert any(
            e.kind == "reserved_assertion" and e.prohibited for e in partial.events
        )
        assert not any(e.kind == "reactivated" for e in partial.events)


def test_physical_graph_pins_and_literal_identity_remain_owned() -> None:
    dataset = Dataset()
    dataset.graph(URIRef("urn:selected")).add((TERM, OWL.deprecated, Literal(True)))
    dataset.graph(URIRef("urn:excluded")).add((TERM, OWL.deprecated, Literal(False)))
    old = Release(
        "old",
        PreparedSource(
            dataset,
            is_dataset=True,
            document_iri="urn:old",
            effective_url="urn:old",
            literal_fidelity="supplied_terms",
            graph_name="urn:selected",
        ),
    )
    new = release("new", '<urn:term:A> owl:deprecated "0"^^xsd:boolean .')
    result = check_lifecycle(
        [old, new], [TERM], policy=LifecyclePolicy(), coverage=history(old, new)
    )
    assert result.report.outcome == "disagreement"
    assert result.observations[0].deprecation.state == "deprecated"
    assert result.observations[0].release.source.graph_name == "urn:selected"
    assert str(result.observations[-1].deprecation.assertions[0].object) == "0"
    union = Release(
        "union",
        PreparedSource(
            dataset,
            is_dataset=True,
            document_iri="urn:union",
            effective_url="urn:union",
            literal_fidelity="supplied_terms",
            union=True,
        ),
    )
    rejected = check_lifecycle(
        [union, new], [TERM], policy=LifecyclePolicy(), coverage=history(union, new)
    )
    assert not rejected.report.verified and not rejected.observations


def test_anonymous_logic_and_conflicting_retirement_remain_unknown() -> None:
    old = release("old", "<urn:term:A> a owl:Class; owl:deprecated true .")
    new = release(
        "new",
        "<urn:term:A> a owl:Class; rdfs:subClassOf [ a owl:Restriction; "
        "owl:onProperty <urn:p>; owl:someValuesFrom <urn:C> ] .",
    )
    result = check_lifecycle(
        [old, new],
        [TERM],
        policy=LifecyclePolicy(reserved=RESERVED),
        coverage=history(old, new),
    )
    assert not result.report.verified
    assert any(
        e.fact is not None and isinstance(e.fact.statement[2], BNode)
        for e in result.events
    )
    assert any(i.kind == "unverified" for i in result.report.issues)
    conflicting = release("conflict", "<urn:term:A> owl:deprecated true, false .")
    result = check_lifecycle(
        [conflicting, new],
        [TERM],
        policy=LifecyclePolicy(),
        coverage=history(conflicting, new),
    )
    assert not result.report.verified and not result.events


def test_explicit_occurrence_positions_and_cumulative_exhaustion() -> None:
    old = release("old", "<urn:term:A> owl:deprecated true .")
    new = release("new", "<urn:term:Other> rdfs:subClassOf <urn:term:A> .")
    supplied = [old, new]
    subject = LifecyclePolicy(reserved=RESERVED, positions=("subject",))
    assert check_lifecycle(
        supplied, [TERM], policy=subject, coverage=history(*supplied)
    ).report.verified
    budget = Budget()
    result = check_lifecycle(
        supplied,
        [TERM],
        policy=replace(subject, positions=("object",)),
        coverage=history(*supplied),
        budget=budget,
    )
    assert result.report.outcome == "disagreement"
    assert result.events[-1].fact is not None
    assert result.events[-1].fact.statement == (
        URIRef("urn:term:Other"),
        RDFS.subClassOf,
        TERM,
    )
    assert not check_lifecycle(
        supplied,
        [TERM],
        policy=subject,
        coverage=history(*supplied),
        budget=Budget(Limits(max_work=1)),
    ).report.verified
    with pytest.raises(InputError):
        check_lifecycle(
            supplied, [TERM], policy=subject, limits=Limits(max_work=2), budget=Budget()
        )
    invalid = check_lifecycle([old, old], [TERM], policy=subject)
    assert invalid.report.outcome == "input_error"


def test_mutated_scope_metadata_cannot_become_a_checked_history() -> None:
    old = release("old", "<urn:term:A> owl:deprecated true .")
    new = release("new", "<urn:term:A> a owl:Class .")
    old.source.union = 0  # type: ignore[assignment]
    result = check_lifecycle(
        [old, new], [TERM], policy=LifecyclePolicy(), coverage=history(old, new)
    )
    assert result.report.outcome == "input_error" and not result.observations


def test_each_release_resolves_its_own_supplied_import_closure() -> None:
    sources = [
        release(
            label,
            "<urn:ontology> owl:imports <urn:support> . "
            f"<urn:term:A> owl:deprecated {state} .",
        )
        for label, state in (("old", "true"), ("new", "false"))
    ]
    supplied = []
    for item in sources:
        nested = "urn:support:" + item.label
        resources = {
            "urn:support": RdfSource(
                f"<urn:support> <{OWL.imports}> <{nested}> . "
                f"<urn:term:A> <{OWL.deprecated}> true, false .",
                format="turtle",
                document_iri="urn:support",
            ),
            nested: RdfSource("", format="turtle", document_iri=nested),
        }
        supplied.append(Release(item.label, item.source, resources))
    checked = check_lifecycle(
        supplied, [TERM], policy=LifecyclePolicy(), coverage=history(*supplied)
    )
    assert checked.report.outcome == "disagreement"
    assert [observation.deprecation.state for observation in checked.observations] == [
        "deprecated",
        "active",
    ]
    assert checked.events[-1].kind == "reactivated"
    assert all(list(item.source.graph().objects(None, OWL.imports)) for item in sources)
    missing = check_lifecycle(
        [supplied[0], sources[1]],
        [TERM],
        policy=LifecyclePolicy(),
        coverage=history(*supplied),
    )
    assert missing.report.outcome == "unverified"
    assert [event.kind for event in missing.events] == ["retired"]
    assert missing.observations[0].release is supplied[0]
    assert any(issue.location == "urn:support" for issue in missing.report.issues)
