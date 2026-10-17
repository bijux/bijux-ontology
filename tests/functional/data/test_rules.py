"""Actual data positions, lexical defects and trusted rule state isolation."""

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, XSD

from bijux_ontology.data.rules import DataView, run_rules
from bijux_ontology.data.usage import check_data_usage
from bijux_ontology.errors import InputError
from bijux_ontology.reports import Issue

EX = Namespace("https://example.org/")


def test_data_usage_checks_vocab_positions_without_treating_records_as_terms() -> None:
    vocab = Graph().add((EX.Record, RDF.type, OWL.Class))
    vocab.add((EX.related, RDF.type, RDF.Property))
    data = Graph().add((EX.record, RDF.type, EX.Record))
    data.add((EX.record, EX.related, EX.other_record))
    assert check_data_usage(data, vocab).verified
    data.add((EX.record, EX.typo, EX.other_record))
    data.add(
        (EX.record, EX.related, Literal("1e3", datatype=XSD.decimal, normalize=False))
    )
    report = check_data_usage(data, vocab)
    assert any(
        issue.code == "vocabulary.term_missing" and issue.actual == str(EX.typo)
        for issue in report.issues
    )
    assert any(issue.code == "literal.lexical_invalid" for issue in report.issues)
    assert not any(issue.actual == str(EX.other_record) for issue in report.issues)
    excluded = check_data_usage(data, vocab, exclude=(str(EX.typo),))
    assert excluded.exclusions == (str(EX.typo),)
    assert not any(issue.actual == str(EX.typo) for issue in excluded.issues)
    anonymous = Graph().add((EX.record, RDF.type, BNode()))
    unresolved = check_data_usage(anonymous, vocab)
    assert [issue.code for issue in unresolved.issues] == ["data.type_unverified"]
    assert not unresolved.verified


def test_dataset_validation_requires_selection_and_literal_results_keep_scope() -> None:
    vocab = Graph().add((EX.related, RDF.type, RDF.Property))
    data = Dataset()
    data.graph(EX.graph).add((EX.record, EX.related, Literal("value", lang="fr")))
    with pytest.raises(InputError, match="Dataset requires"):
        check_data_usage(data, vocab)
    assert check_data_usage(data, vocab, graph_name=EX.graph).verified
    assert not check_data_usage(data, vocab, graph_name="@default").verified


def test_custom_rules_receive_owned_graph_exports_and_preserve_exceptions() -> None:
    data = Graph().add((EX.record, EX.related, EX.value))

    def clear_export(view: DataView) -> tuple[Issue, ...]:
        graph = view.graph()
        graph.remove((None, None, None))
        assert len(tuple(view.triples())) == 1
        return ()

    def require_description(view: DataView) -> tuple[Issue, ...]:
        assert len(tuple(view.triples())) == 1
        return (
            Issue(
                "catalogue.description_missing",
                "A description is required",
                str(EX.record),
            ),
        )

    report = run_rules(
        data, {"owned-export": clear_export, "description": require_description}
    )
    assert report.checked == 2 and len(data) == 1
    assert report.issues[0].code == "catalogue.description_missing"
    assert not run_rules(data, {}).verified

    def programming_error(view: DataView) -> tuple[Issue, ...]:
        raise RuntimeError("application failure")

    with pytest.raises(RuntimeError, match="application failure"):
        run_rules(data, {"error": programming_error})


def test_rule_result_budgets_preserve_prior_scopes_and_callback_exceptions() -> None:
    from collections.abc import Iterator

    from bijux_ontology.errors import LimitExceeded
    from bijux_ontology.limits import Limits

    data = Graph().add((EX.record, EX.related, EX.value))

    def first(view: DataView) -> tuple[Issue, ...]:
        return (Issue("catalogue.first", "Retained earlier result"),)

    def many(view: DataView) -> Iterator[Issue]:
        for _ in range(1000):
            yield Issue("catalogue.repeated", "A generated finding")

    report = run_rules(
        data, {"first": first, "many": many}, limits=Limits(max_issues=2)
    )
    assert report.exit_code() == 2 and not report.complete
    assert report.children[0].scope == "first" and report.children[0].complete
    assert [issue.code for issue in report.issues[:2]] == [
        "catalogue.first",
        "catalogue.repeated",
    ]
    assert report.issues[-1].kind == "exhausted"

    def raises(view: DataView) -> tuple[Issue, ...]:
        raise LimitExceeded("consumer failure")

    with pytest.raises(LimitExceeded, match="consumer failure"):
        run_rules(data, {"raises": raises})
