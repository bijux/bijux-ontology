"""Document index contracts use actual maintained processing, distinct from RDF."""

from copy import deepcopy
from typing import Any, cast

import pytest

from bijux_ontology.absence import ABSENT
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.jsonld.expectations import ContextExpectation
from bijux_ontology.jsonld.index_maps import (
    IndexBinding,
    IndexMapExpectation,
    check_index_maps,
)
from bijux_ontology.jsonld.processing.preparation import prepare_jsonld
from bijux_ontology.jsonld.usages.checking import prepare_usages
from bijux_ontology.rdf.comparison import compare_rdf

X = "https://index.example/"


def _document(*, custom: bool = False) -> dict[str, Any]:
    """Use named owners to establish expansion-independent association witnesses."""
    definition: dict[str, object] = {"@id": X + "items", "@container": "@index"}
    if custom:
        definition["@index"] = X + "code"
    return {
        "@context": {"@vocab": X, "items": definition, "code": X + "code"},
        "@id": X + "Root",
        "items": {
            "alpha": {"@id": X + "A", "name": "A"},
            "beta": {"@id": X + "B", "name": "B"},
        },
    }


def _rule(**kwargs: Any) -> IndexMapExpectation:
    """Select the source map and its exact expanded predicate explicitly."""
    return IndexMapExpectation(ContextExpectation(("items",), X + "items"), **kwargs)


def test_plain_index_key_mutation_is_document_failure_while_rdf_agrees() -> None:
    original = _document()
    prepared = prepare_usages(original)
    rule = _rule(
        keys=("alpha", "beta"),
        bindings=(IndexBinding("alpha", X + "A"), IndexBinding("beta", X + "B")),
    )
    result = check_index_maps(prepared, (rule,))
    assert result.report.verified
    assert all(item.qualified for item in result.observations)
    original["items"]["gamma"] = original["items"].pop("beta")
    changed = check_index_maps(prepare_usages(original), (rule,))
    assert "context.index_keys_mismatch" in {
        issue.code for issue in changed.report.issues
    }
    assert "context.index_binding_missing" in {
        issue.code for issue in changed.report.issues
    }
    assert prepared.check(
        (ContextExpectation(("items", "alpha"), X + "items", index="alpha"),)
    ).verified
    before, after = prepare_jsonld(_document()).source, prepare_jsonld(original).source
    assert before is not None and after is not None
    assert compare_rdf(before, after).verified
    assert check_index_maps(prepared, (rule,)).report.verified


def test_custom_property_index_is_processor_proved_and_authored_coincidence_fails() -> (
    None
):
    document = _document(custom=True)
    result = check_index_maps(
        prepare_usages(document), (_rule(index_property=X + "code"),)
    )
    assert result.report.verified
    plain = check_index_maps(prepare_usages(document), (_rule(),))
    assert "context.index_origin_mismatch" in {
        issue.code for issue in plain.report.issues
    }
    authored = _document()
    authored["@context"]["items"]["@container"] = "@id"
    authored["items"] = {X + "A": {"code": X + "A", "name": "A"}}
    assert not check_index_maps(
        prepare_usages(authored), (_rule(index_property=X + "code"),)
    ).report.verified
    # Existing plain metadata remains independent of the key-derived RDF property.
    document["items"]["alpha"]["@index"] = "authored"
    assert check_index_maps(
        prepare_usages(document), (_rule(index_property=X + "code"),)
    ).report.verified
    assert (
        prepare_usages(document)
        .check((ContextExpectation(("items", "alpha"), X + "items", index="authored"),))
        .verified
    )
    literal = _document()
    literal["items"] = {"alpha": {"@value": "literal", "@language": "en"}}
    assert check_index_maps(prepare_usages(literal), (_rule(),)).report.verified
    assert (
        prepare_usages(literal)
        .check((ContextExpectation(("items", "alpha"), X + "items", index="alpha"),))
        .verified
    )
    coerced = _document(custom=True)
    coerced["@context"]["items"]["@index"] = "code"
    coerced["@context"]["code"] = {"@id": X + "code", "@type": "@id"}
    coerced["items"] = {X + "alpha": {"@id": X + "A"}}
    assert check_index_maps(
        prepare_usages(coerced, base=X), (_rule(index_property=X + "code"),)
    ).report.verified


def test_list_wrapper_index_never_becomes_member_index_and_graph_scope_is_exact() -> (
    None
):
    document = _document()
    document["items"] = {"alpha": {"@list": [{"@id": X + "A"}, {"@id": X + "B"}]}}
    prepared = prepare_usages(document)
    result = check_index_maps(prepared, (_rule(keys=("alpha",)),))
    assert result.report.verified
    members = result.observations[0].usage.occurrences
    assert [item.list_index for item in members] == ["alpha", "alpha"]
    assert all(item.traits.index is None for item in members)
    assert prepared.check(
        (
            ContextExpectation(
                ("items", "alpha"), X + "items", list_index="alpha", index=ABSENT
            ),
        )
    ).verified
    empty_list = deepcopy(document)
    empty_list["items"]["alpha"]["@list"] = []
    assert check_index_maps(prepare_usages(empty_list), (_rule(),)).report.verified
    graph = {
        "@context": document["@context"],
        "@id": X + "Graph",
        "@graph": [{"@id": X + "Root", "items": {"alpha": {"@id": X + "A"}}}],
    }
    rule = IndexMapExpectation(
        ContextExpectation(("@graph", 0, "items"), X + "items"),
        bindings=(IndexBinding("alpha", X + "A", X + "Graph"),),
    )
    assert check_index_maps(prepare_usages(graph), (rule,)).report.verified
    wrong = IndexMapExpectation(
        rule.mapping, bindings=(IndexBinding("alpha", X + "A"),)
    )
    assert not check_index_maps(prepare_usages(graph), (wrong,)).report.verified


def test_merged_records_and_nonindex_keys_keep_completed_key_facts() -> None:
    document = _document()
    document["items"]["beta"] = deepcopy(document["items"]["alpha"])
    result = check_index_maps(
        prepare_usages(document), (_rule(keys=("alpha", "beta")),)
    )
    assert not result.report.verified
    assert any(
        issue.code == "context.index_unverified" for issue in result.report.issues
    )
    assert any(
        child.scope == "index-keys:/items" and child.verified
        for child in result.report.children
    )
    document["@context"]["items"]["@container"] = "@id"
    assert not check_index_maps(prepare_usages(document), (_rule(),)).report.verified
    empty = _document()
    empty["items"] = {}
    assert not check_index_maps(
        prepare_usages(empty), (_rule(keys=()),)
    ).report.verified


def test_limits_retain_prior_key_finding_and_invalid_containers_are_input_errors() -> (
    None
):
    operation = Budget(Limits(max_resources=1))
    prepared = prepare_usages(_document(), budget=operation)
    result = check_index_maps(prepared, (_rule(keys=("wrong",)),))
    assert result.report.outcome == "exhausted"
    assert "context.index_keys_mismatch" in {
        issue.code for issue in result.report.issues
    }
    for values in ("alpha", iter(("alpha",))):
        with pytest.raises(InputError):
            IndexMapExpectation(
                ContextExpectation(("items",), X + "items"), keys=cast(Any, values)
            )


@pytest.mark.parametrize("field", ("index", "list_index"))
def test_expected_index_text_is_bounded_before_diagnostic_retention(field: str) -> None:
    budget = Budget(Limits(max_bytes=1000, max_total_bytes=10000))
    prepared = prepare_usages(
        {"@id": X + "Root", X + "items": {"@value": "text", "@index": "a"}},
        budget=budget,
    )
    expectation = ContextExpectation(
        (X + "items",), X + "items", **{field: cast(Any, "x" * 100000)}
    )
    result = prepared.check((expectation,))
    assert result.outcome == "exhausted" and not result.complete
    assert all(len(issue.expected or "") < 1000 for issue in result.issues)
