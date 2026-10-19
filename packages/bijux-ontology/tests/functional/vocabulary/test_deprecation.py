"""Replacement hints retain ambiguity and finite source-role evidence."""

from rdflib import Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF

from bijux_ontology.vocabulary.deprecation import replacement_candidates
from bijux_ontology.vocabulary.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary
from bijux_ontology.workflows.ontology.checking import check_ontology

EX = Namespace("https://example.org/")


def test_candidates_distinguish_multiple_unresolved_and_kind_mismatches() -> None:
    graph = Graph().add((EX.old, RDF.type, OWL.DeprecatedClass))
    graph.add((EX.good, RDF.type, OWL.Class))
    graph.add((EX.otherRole, RDF.type, OWL.ObjectProperty))
    for target in (EX.good, EX.otherRole, EX.missing, Literal("human note")):
        graph.add((EX.old, EX.replacedBy, target))
    view = inspect_vocabulary(graph)
    candidates = replacement_candidates(
        view, str(EX.old), predicates=[str(EX.replacedBy)]
    )
    assert {str(item.assertion.object): item.status for item in candidates.items} == {
        str(EX.good): "declared",
        str(EX.otherRole): "kind-mismatch",
        str(EX.missing): "unresolved",
        "human note": "unverified",
    }
    assert candidates.report.outcome == "disagreement"
    assert not candidates.report.complete
    report = check_ontology(
        graph,
        families=("deprecated",),
        used_terms=[str(EX.old)],
        replacement_predicates=[str(EX.replacedBy)],
    )
    assert "ontology.replacement_ambiguous" in {issue.code for issue in report.issues}
    assert "ontology.deprecated_term" in {issue.code for issue in report.issues}
    assert len(tuple(graph.objects(EX.old, EX.replacedBy))) == 4


def test_absent_hints_and_conflicting_states_never_choose_a_replacement() -> None:
    graph = Graph().add((EX.old, RDF.type, OWL.Class))
    graph.add((EX.old, OWL.deprecated, Literal(True)))
    report = check_ontology(
        graph,
        families=("deprecated",),
        used_terms=[str(EX.old)],
        replacement_predicates=[str(EX.replacedBy)],
    )
    assert [issue.code for issue in report.issues] == [
        "ontology.deprecated_term",
        "ontology.replacement_absent",
    ]
    graph.add((EX.old, OWL.deprecated, Literal(False)))
    conflicting = check_ontology(
        graph, families=("deprecated",), used_terms=[str(EX.old)]
    )
    assert conflicting.outcome == "unverified"
    assert conflicting.issues[0].code == "ontology.deprecation_unverified"


def test_deprecation_and_candidate_resolution_respect_selected_source_roles() -> None:
    original = Graph().add((EX.old, RDF.type, OWL.Class))
    imported = Graph().add((EX.old, OWL.deprecated, Literal(True)))
    imported.add((EX.old, EX.replacedBy, EX.replacement))
    lookup = Graph().add((EX.replacement, RDF.type, OWL.Class))
    vocabulary = Vocabulary(original, imports=(imported,), lookup=(lookup,))
    unresolved = check_ontology(
        vocabulary,
        families=("deprecated",),
        used_terms=[str(EX.old)],
        replacement_predicates=[str(EX.replacedBy)],
    )
    assert "ontology.replacement_unresolved" in {
        issue.code for issue in unresolved.issues
    }
    resolved = check_ontology(
        vocabulary,
        families=("deprecated",),
        include_lookup=True,
        used_terms=[str(EX.old)],
        replacement_predicates=[str(EX.replacedBy)],
    )
    assert [issue.code for issue in resolved.issues] == ["ontology.deprecated_term"]
    unselected = check_ontology(
        vocabulary,
        families=("deprecated",),
        include_imports=False,
        used_terms=[str(EX.old)],
    )
    assert unselected.verified
