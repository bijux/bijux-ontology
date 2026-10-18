"""Directional identities survive attribution and never silently pass RDF loss."""

import pytest
from rdflib import Namespace

from bijux_ontology.errors import LimitExceeded
from bijux_ontology.jsonld.checking import prepare_usages
from bijux_ontology.jsonld.conversion import DirectionPolicy
from bijux_ontology.jsonld.observations import occurrences
from bijux_ontology.jsonld.preparation import prepare_jsonld
from bijux_ontology.limits import Budget, Limits

EX = Namespace("https://language.example/")


def _document(value: object) -> dict[str, object]:
    """Keep one explicit named owner and predicate for identity comparisons."""
    return {"@id": str(EX.root), "@context": {"label": str(EX.label)}, "label": value}


@pytest.mark.parametrize("policy", ["i18n-datatype", "compound-literal"])
def test_direction_distinct_conversion_never_claims_preservation_after_dedup(
    policy: DirectionPolicy,
) -> None:
    for directions in (("ltr", "rtl"), (None, "rtl")):
        values: list[dict[str, object]] = []
        for direction in directions:
            value: dict[str, object] = {"@value": "same", "@language": "en"}
            if direction is not None:
                value["@direction"] = direction
            values.append(value)
        document = _document(values)
        converted = prepare_jsonld(document, direction=policy)
        assert converted.source is None and not converted.report.verified
        assert any(
            issue.code == "context.rdf_direction_unverified"
            for issue in converted.report.issues
        )


@pytest.mark.parametrize("policy", ["i18n-datatype", "compound-literal"])
def test_direction_guard_keeps_distinct_owners_graphs_properties_and_list_positions(
    policy: DirectionPolicy,
) -> None:
    left = {"@value": "same", "@language": "en", "@direction": "ltr"}
    right = {"@value": "same", "@language": "en", "@direction": "rtl"}
    context = {"label": str(EX.label), "other": str(EX.other)}
    documents = (
        {
            "@context": context,
            "@graph": [
                {"@id": str(EX.left), "label": left},
                {"@id": str(EX.right), "label": right},
            ],
        },
        {
            "@context": context,
            "@graph": [
                {"label": left},
                {"label": right},
            ],
        },
        {
            "@context": context,
            "@graph": [
                {
                    "@id": str(EX.graphLeft),
                    "@graph": [{"@id": str(EX.root), "label": left}],
                },
                {
                    "@id": str(EX.graphRight),
                    "@graph": [{"@id": str(EX.root), "label": right}],
                },
            ],
        },
        {"@context": context, "@id": str(EX.root), "label": left, "other": right},
        _document({"@list": [left, right]}),
    )
    for document in documents:
        prepared = prepare_jsonld(document, direction=policy)
        assert prepared.source is not None and prepared.report.verified, (
            prepared.report.to_dict()
        )


def test_single_value_object_metadata_is_not_a_descendant_usage() -> None:
    document = _document(
        {"@value": "text", "@language": "EN", "@direction": "rtl", "@index": "first"}
    )
    observed = prepare_usages(document).observe(("label",))
    assert observed.limitation is None and len(observed.occurrences) == 1
    traits = observed.occurrences[0].traits
    assert traits.language == "en" and traits.direction == "rtl"


def test_owned_occurrence_capture_bounds_retained_literal_bytes() -> None:
    with pytest.raises(LimitExceeded):
        occurrences(
            [
                {
                    "@id": str(EX.root),
                    str(EX.label): [{"@value": "text", "@direction": "rtl"}],
                }
            ],
            budget=Budget(Limits(max_total_bytes=1)),
        )
