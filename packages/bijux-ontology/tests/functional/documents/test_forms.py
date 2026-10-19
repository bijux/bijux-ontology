"""Consumer representation rules remain separate from JSON-LD/RDF meaning."""

from copy import deepcopy
from dataclasses import replace
from typing import Any

import pytest

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.documents.forms import FormRequirement, check_document_form
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.jsonld.expectations import ContextExpectation
from bijux_ontology.jsonld.processing.preparation import prepare_jsonld
from bijux_ontology.jsonld.selectors.types import (
    ArrayItems,
    EntitySelector,
    KeyedItems,
    Property,
    SourceSelector,
)
from bijux_ontology.jsonld.usages.checking import prepare_usages
from bijux_ontology.rdf.comparison import compare_rdf

EX = "https://example.org/form/"
CONTEXT = {"name": EX + "name", "related": {"@id": EX + "related", "@type": "@id"}}


def documents() -> tuple[dict[str, Any], dict[str, Any]]:
    """Supply different source forms with exactly the same RDF meaning."""
    flat = {
        "@context": CONTEXT,
        "@graph": [
            {"@id": EX + "parent", "related": {"@id": EX + "child"}},
            {"@id": EX + "child", "name": "Child"},
        ],
    }
    nested = {
        "@context": CONTEXT,
        "@graph": [
            {"@id": EX + "parent", "related": {"@id": EX + "child", "name": "Child"}},
        ],
    }
    return flat, nested


def reference_contract() -> tuple[FormRequirement, ...]:
    """Select graph records and their actual relationship occurrences explicitly."""
    return (
        FormRequirement(SourceSelector(()), shape="object", required_keys=("@graph",)),
        FormRequirement(SourceSelector((Property("@graph"),)), shape="array"),
        FormRequirement(
            SourceSelector((Property("@graph"), ArrayItems())),
            shape="object",
            required_keys=("@id",),
        ),
        FormRequirement(
            SourceSelector(
                (Property("@graph"), ArrayItems(), Property("related", minimum=0))
            ),
            embedding="reference",
        ),
    )


def test_rdf_equal_flat_reference_and_nested_forms_obey_distinct_contracts() -> None:
    flat, nested = documents()
    left, right = prepare_jsonld(flat), prepare_jsonld(nested)
    assert left.source is not None and right.source is not None
    assert compare_rdf(left.source, right.source).verified
    reference = reference_contract()
    valid = check_document_form(flat, reference)
    assert valid.form_report.verified and valid.semantic_report.verified
    assert valid.report.verified
    failed = check_document_form(nested, reference)
    assert failed.semantic_report.verified and not failed.form_report.verified
    assert failed.report.outcome == "disagreement"
    assert any(
        i.code == "documents.form_embedding" and i.location == "/@graph/0/related"
        for i in failed.form_report.issues
    )
    inline = (replace(reference[-1], embedding="inline"),)
    assert check_document_form(nested, inline).report.verified
    assert not check_document_form(flat, inline).report.verified


def test_keyword_spelling_scalar_array_and_extra_keys_are_opt_in() -> None:
    document = {
        "@context": {"graph": "@graph", "identifier": "@id", "name": EX + "name"},
        "graph": [{"identifier": EX + "child", "name": ["Child"]}],
    }
    only_object = (FormRequirement(SourceSelector(()), shape="object"),)
    assert check_document_form(document, only_object).report.verified
    canonical = (replace(only_object[0], required_keys=("@graph",)),)
    result = check_document_form(document, canonical)
    assert result.semantic_report.verified and not result.form_report.verified
    name = SourceSelector((Property("graph"), ArrayItems(), Property("name")))
    assert check_document_form(
        document, [FormRequirement(name, shape="array")]
    ).report.verified
    assert not check_document_form(
        document, [FormRequirement(name, shape="scalar")]
    ).form_report.verified
    record = SourceSelector((Property("graph"), ArrayItems()))
    assert check_document_form(
        document, [FormRequirement(record, required_keys=("identifier",))]
    ).report.verified
    closed = FormRequirement(record, allowed_keys=("identifier",))
    assert not check_document_form(document, [closed]).form_report.verified
    # Entity selection still checks each describing source object, not joined facts.
    entity = EntitySelector(
        identifiers=(EX + "child",), graph=None, coverage_unit="source_occurrence"
    )
    assert check_document_form(
        document, [FormRequirement(entity, required_keys=("identifier",))]
    ).report.verified


def test_wrong_context_and_conversion_loss_cannot_hide_behind_form_success() -> None:
    flat, _ = documents()
    expected = ContextExpectation(
        ("@graph", 0, "related"),
        EX + "related",
        value_kind="iri",
        identifier=EX + "child",
    )
    assert check_document_form(
        flat, reference_contract(), expectations=[expected]
    ).report.verified
    wrong = deepcopy(flat)
    wrong["@context"]["related"]["@id"] = EX + "wrong"
    result = check_document_form(wrong, reference_contract(), expectations=[expected])
    assert result.form_report.verified and not result.semantic_report.verified
    assert not result.report.verified
    dropped = {"@context": {"name": None}, "name": "Child"}
    syntactic = (FormRequirement(SourceSelector(()), required_keys=("name",)),)
    null_term = check_document_form(dropped, syntactic)
    assert null_term.form_report.verified and not null_term.report.verified
    directional = {
        "@context": {"name": EX + "name"},
        "@id": EX + "child",
        "name": {"@value": "Child", "@language": "en", "@direction": "rtl"},
    }
    no_policy = check_document_form(directional, syntactic)
    assert no_policy.form_report.verified and not no_policy.semantic_report.verified
    assert check_document_form(
        directional, syntactic, rdf_direction="i18n-datatype"
    ).report.verified
    invalid = {"@context": {"bad": {"@id": EX + "name", "@reverse": EX + "other"}}}
    assert check_document_form(invalid, syntactic).report.outcome == "input_error"


def test_prepared_snapshot_reuse_pins_resources_and_preserves_occurrence_scope() -> (
    None
):
    flat, _ = documents()
    prepared = prepare_usages(flat)
    flat.clear()
    assert check_document_form(prepared, reference_contract()).report.verified
    assert check_document_form(prepared, reference_contract()).report.verified
    with pytest.raises(InputError):
        check_document_form(prepared, reference_contract(), base=EX)
    with pytest.raises(InputError):
        check_document_form(prepared, reference_contract(), budget=Budget())
    omitted = prepare_usages(
        {"@context": CONTEXT, "@id": EX + "child", "name": "Child"},
        allowed_omissions=(("name",),),
    )
    with pytest.raises(InputError):
        check_document_form(omitted, reference_contract())
    with pytest.raises(InputError):
        FormRequirement(EntitySelector(identifiers=(EX + "child",), graph=None))
    keys: Any = ["@graph"]
    contract = FormRequirement(SourceSelector(()), required_keys=keys)
    keys.clear()
    assert contract.required_keys == ("@graph",)
    invalid_keys: tuple[Any, ...] = (None, "@graph", {}, iter(["@graph"]), [1])
    for invalid in invalid_keys:
        with pytest.raises(InputError):
            FormRequirement(SourceSelector(()), required_keys=invalid)


def test_partial_budget_keeps_known_form_failures_and_pending_scope_incomplete() -> (
    None
):
    document = {"@context": {"name": EX + "name"}, "@id": EX + "child", "name": "Child"}
    first = FormRequirement(SourceSelector(()), shape="array")
    following = FormRequirement(SourceSelector(()), shape="object")
    observed = False
    for work in (300, 500, 700, 1000):
        result = check_document_form(
            document, [first, *([following] * 100)], limits=Limits(max_work=work)
        )
        if any(i.code == "documents.form_shape" for i in result.form_report.issues):
            assert result.report.outcome == "exhausted" and not result.report.complete
            assert any(i.code == "documents.form_shape" for i in result.report.issues)
            observed = True
            break
    assert observed
    inventories: tuple[Any, ...] = (None, "requirements", {}, iter([first]), [1])
    for inventory in inventories:
        result = check_document_form(document, inventory)
        assert result.report.outcome == "input_error" and not result.report.verified


def test_selector_and_expectation_metadata_is_bounded_before_diagnostics() -> None:
    document = {"@id": EX + "child"}
    text = "x" * 100000
    iri = EX + text
    selectors = (
        SourceSelector((), start=(text,)),
        SourceSelector((Property(text),)),
        SourceSelector((KeyedItems(keys=(text,)),)),
        SourceSelector((), start=("é" * 600,)),
        SourceSelector((), start=(2**20000,)),
        SourceSelector((), minimum=2**20000),
        EntitySelector(
            identifiers=(iri,), graph=None, coverage_unit="source_occurrence"
        ),
        EntitySelector(types=(iri,), graph=None, coverage_unit="source_occurrence"),
        EntitySelector(graph=iri, coverage_unit="source_occurrence"),
    )
    limits = Limits(max_total_bytes=10000, max_bytes=1000)
    for selector in selectors:
        result = check_document_form(
            document, [FormRequirement(selector)], limits=limits
        )
        assert result.report.outcome == "exhausted" and not result.report.complete
        assert all(
            len(i.location.encode("utf-8")) <= 1000 for i in result.report.issues
        )
    for contract in (
        FormRequirement(SourceSelector(()), required_keys=(text,)),
        FormRequirement(SourceSelector(()), allowed_keys=(text,)),
    ):
        assert (
            check_document_form(document, [contract], limits=limits).report.outcome
            == "exhausted"
        )
    expectation = ContextExpectation((text,), EX + "p")
    result = check_document_form(
        document,
        [FormRequirement(SourceSelector(()))],
        expectations=[expectation],
        limits=limits,
    )
    assert result.form_report.verified and result.report.outcome == "exhausted"
    assert not result.report.complete
    assert all(len(i.location) <= 1000 for i in result.report.issues)
    prepared = prepare_usages(document, limits=limits)
    assert prepared.check([expectation]).outcome == "exhausted"
    for batch in (False, True):
        prepared = prepare_usages(document, limits=limits)
        with pytest.raises(LimitExceeded):
            if batch:
                prepared.observe_many(((text,),))
            else:
                prepared.observe((text,))
