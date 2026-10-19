"""Exact application relation occurrences require checked meaning and no lost events."""

import threading
from typing import cast

from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, XSD
from rdflib.term import Node

from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.documents.migrations import export_migration
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.migrations.candidates import MigrationCheck
from bijux_ontology.rdf.migrations.relations import (
    BinaryOccurrence,
    RelationOccurrence,
    RelationPattern,
    RelationQualifier,
    RelationRole,
    migrate_relations,
)
from bijux_ontology.rdf.patches.applying import apply_rdf_patch
from bijux_ontology.rdf.snapshots import PreparedSource

E = Namespace("urn:relation:")
Triple = tuple[Node, Node, Node]


def _pattern(*, incoming: bool = False) -> RelationPattern:
    """Meaning follows explicit caller roles, including an owner-to-event link."""
    return RelationPattern(
        E.associated,
        E.Connection,
        RelationRole(E.observer, "incoming" if incoming else "outgoing"),
        RelationRole(E.observed),
        "positive",
    )


def _source(rows: set[Triple]) -> PreparedSource:
    """Own exactly the supplied asserted application graph."""
    graph = Graph()
    for triple in rows:
        graph.add(triple)
    return prepare_rdf(graph)


def _check(rows: set[Triple], graph: URIRef | None = None) -> MigrationCheck:
    """Require actual complete RDF facts, rather than a synthetic approval flag."""

    def check(source: PreparedSource) -> Report:
        """Compare the resulting physical graph with the explicit output contract."""
        actual = set(source.graph(graph))
        return Report(
            "expected-relation-facts",
            1,
            () if actual == rows else (Issue("relation.facts", "Actual facts differ"),),
        )

    return MigrationCheck("exact-facts", check)


def _event(event: URIRef | BNode = E.event) -> set[Triple]:
    """An asserted unqualified relation event with exactly two participants."""
    return {
        (event, RDF.type, E.Connection),
        (event, E.observer, E.a),
        (event, E.observed, E.b),
    }


def _codes(report: Report) -> set[str]:
    """Include the actual retained child evidence of a rejected migration."""
    return {item.code for item in report.issues} | {
        code for child in report.children for code in _codes(child)
    }


def test_explicit_binary_qualifier_and_unqualified_inverse_are_checked() -> None:
    """Exact qualifiers survive expansion; unqualified inverse recovers the source."""
    rows: set[Triple] = {
        (E.a, E.associated, E.b),
        (E.other, E.note, Literal("stay", datatype=XSD.string)),
    }
    source = _source(rows)
    qualifier = Literal("01", datatype=XSD.integer, normalize=False)
    output = _event() | {
        (E.event, E.certainty, qualifier),
        (E.other, E.note, Literal("stay", datatype=XSD.string)),
    }
    expanded = migrate_relations(
        source,
        _pattern(),
        (
            BinaryOccurrence(
                E.a, E.b, E.event, (RelationQualifier(E.certainty, qualifier),)
            ),
        ),
        before_checks=(_check(rows),),
        after_checks=(_check(output),),
    )
    assert (
        expanded.report.verified
        and expanded.source is not None
        and expanded.patch is not None
    )
    assert set(source.graph()) == rows and set(expanded.source.graph()) == output
    assert export_migration(expanded, accept=True, format="turtle").report.verified
    blocked = migrate_relations(
        expanded.source,
        _pattern(),
        (RelationOccurrence(E.event),),
        after_checks=(_check(rows),),
    )
    assert blocked.source is None and "rdf.relation_qualifier_loss" in _codes(
        blocked.report
    )
    plain_output = _event() | {(E.other, E.note, Literal("stay", datatype=XSD.string))}
    plain = migrate_relations(
        source,
        _pattern(),
        (BinaryOccurrence(E.a, E.b, E.event),),
        after_checks=(_check(plain_output),),
    )
    assert plain.source is not None and plain.report.verified
    restored = migrate_relations(
        plain.source,
        _pattern(),
        (RelationOccurrence(E.event),),
        after_checks=(_check(rows),),
    )
    assert restored.report.verified and restored.source is not None
    assert set(restored.source.graph()) == rows
    wrong_base = _source(rows | {(E.changed, E.note, Literal("stale"))})
    stale = apply_rdf_patch(wrong_base, expanded.patch)
    assert not stale.report.verified and stale.source is None


def test_physical_graphs_directed_roles_and_same_participant_events_stay_distinct() -> (
    None
):
    """Two physical edge occurrences allocate different explicit event identities."""
    dataset = Dataset()
    for name in (E.first, E.second):
        dataset.graph(name).add((E.a, E.associated, E.b))
    dataset.graph(E.empty)
    original = prepare_rdf(dataset)
    expected: set[Triple] = {
        (E.event, RDF.type, E.Connection),
        (E.a, E.observer, E.event),
        (E.event, E.observed, E.b),
    }
    result = migrate_relations(
        original,
        _pattern(incoming=True),
        (
            BinaryOccurrence(E.a, E.b, E.event, graph=E.first),
            BinaryOccurrence(E.a, E.b, E.other_event, graph=E.second),
        ),
        after_checks=(_check(expected, E.first),),
    )
    assert result.report.verified and result.source is not None
    assert (E.a, E.observer, E.other_event) in result.source.graph(E.second)
    assert E.empty in {g.identifier for g in result.source.dataset().graphs()}
    assert set(original.graph(E.first)) == {(E.a, E.associated, E.b)}
    duplicate = migrate_relations(
        original,
        _pattern(),
        (
            BinaryOccurrence(E.a, E.b, E.event, graph=E.first),
            BinaryOccurrence(E.a, E.b, E.event, graph=E.second),
        ),
        after_checks=(_check(set()),),
    )
    assert duplicate.source is None and "rdf.relation_identity_collision" in _codes(
        duplicate.report
    )
    wrong_graph = migrate_relations(
        original,
        _pattern(),
        (BinaryOccurrence(E.a, E.b, E.event),),
        after_checks=(_check(set()),),
    )
    assert wrong_graph.source is None and "rdf.relation_occurrence_absent" in _codes(
        wrong_graph.report
    )


def test_distinct_events_and_unhandled_structure_cannot_losslessly_collapse() -> None:
    """Qualifiers, ambiguous roles, sharing and distinct events cannot disappear."""
    cases: tuple[tuple[set[Triple], tuple[RelationOccurrence, ...], str], ...] = (
        (
            _event() | _event(E.other_event),
            (RelationOccurrence(E.event), RelationOccurrence(E.other_event)),
            "rdf.relation_occurrence_collision",
        ),
        (
            _event() | {(E.event, E.observer, E.other)},
            (RelationOccurrence(E.event),),
            "rdf.relation_role_cardinality",
        ),
        (
            _event() | {(E.event, E.certainty, Literal(False))},
            (RelationOccurrence(E.event),),
            "rdf.relation_qualifier_loss",
        ),
        (
            _event() | {(E.event, RDF.type, E.OtherClass)},
            (RelationOccurrence(E.event),),
            "rdf.relation_type_loss",
        ),
        (
            _event() | {(E.other, E.refers, E.event)},
            (RelationOccurrence(E.event),),
            "rdf.relation_occurrence_shared",
        ),
        (
            _event() | {(E.other, E.annotated, Literal("x", datatype=E.event))},
            (RelationOccurrence(E.event),),
            "rdf.relation_occurrence_shared",
        ),
        (
            (_event() - {(E.event, E.observed, E.b)})
            | {(E.event, E.observed, Literal("x", datatype=E.event))},
            (RelationOccurrence(E.event),),
            "rdf.relation_occurrence_shared",
        ),
    )
    for rows, selected, expected in cases:
        source = _source(rows)
        result = migrate_relations(
            source,
            _pattern(),
            selected,
            after_checks=(_check({(E.a, E.associated, E.b)}),),
        )
        assert result.source is None and not result.report.verified
        assert expected in _codes(result.report)
        assert set(source.graph()) == rows


def test_qualified_and_declared_negative_never_produce_positive_edges() -> None:
    """Declared polarity and actual OWL negative types both block positives."""
    source = _source(_event() | {(E.event, RDF.type, OWL.NegativePropertyAssertion)})
    denied = migrate_relations(
        source,
        _pattern(),
        (RelationOccurrence(E.event),),
        after_checks=(_check({(E.a, E.associated, E.b)}),),
    )
    assert denied.source is None and "rdf.relation_negative_unrepresentable" in _codes(
        denied.report
    )
    negative = RelationPattern(
        E.associated,
        E.Connection,
        RelationRole(E.observer),
        RelationRole(E.observed),
        "negative",
    )
    declared = migrate_relations(
        _source(_event()),
        negative,
        (RelationOccurrence(E.event),),
        after_checks=(_check({(E.a, E.associated, E.b)}),),
    )
    assert declared.source is None and declared.report.outcome == "unverified"


def test_owned_blank_roles_and_export_receipt_prevent_identity_substitution() -> None:
    """An equal blank label cannot authorize a different snapshot's occurrence."""
    source = _source(_event(BNode("owned")))
    event = cast(BNode, source.graph().value(predicate=RDF.type, object=E.Connection))
    valid = migrate_relations(
        source,
        _pattern(),
        (RelationOccurrence(event),),
        after_checks=(_check({(E.a, E.associated, E.b)}),),
    )
    assert valid.report.verified and valid.source is not None
    foreign = migrate_relations(
        source,
        _pattern(),
        (RelationOccurrence(BNode(str(event))),),
        after_checks=(_check(set()),),
    )
    assert foreign.source is None and not foreign.report.verified
    valid.source.document_iri = "urn:changed-origin"
    assert export_migration(valid, accept=True, format="turtle").content is None


def test_failed_postcondition_cancel_and_budget_preserve_original() -> None:
    """Partial work and failing checks never approve mutation or accepted output."""
    rows: set[Triple] = {(E.a, E.associated, E.b)}
    source = _source(rows)
    failed = migrate_relations(
        source,
        _pattern(),
        (BinaryOccurrence(E.a, E.b, E.event),),
        after_checks=(_check(rows),),
    )
    assert failed.source is not None and not failed.report.verified
    assert export_migration(failed, accept=True, format="turtle").content is None
    assert set(source.graph()) == rows
    event = threading.Event()
    event.set()
    cancelled = migrate_relations(
        source, _pattern(), (BinaryOccurrence(E.a, E.b, E.event),), cancel=event
    )
    assert cancelled.source is None and cancelled.report.outcome == "exhausted"
    operation = Budget(Limits(max_work=1))
    capped = migrate_relations(
        source, _pattern(), (BinaryOccurrence(E.a, E.b, E.event),), budget=operation
    )
    assert capped.source is None and capped.report.outcome == "exhausted"
    assert operation.remaining("work") >= 0 and set(source.graph()) == rows
