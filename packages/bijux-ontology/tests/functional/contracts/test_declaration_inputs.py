"""Invalid declaration inputs fail before an unconstrained ontology can pass."""

from collections.abc import Callable

import pytest
from rdflib import XSD, Literal

from bijux_ontology import ABSENT as ROOT_ABSENT
from bijux_ontology import EmissionCoverage
from bijux_ontology import FieldDeclaration as Field
from bijux_ontology import ModelDeclaration as Model
from bijux_ontology import ValueDeclaration as Value
from bijux_ontology.absence import ABSENT as SHARED_ABSENT
from bijux_ontology.declarations import ABSENT, TermDeclaration
from bijux_ontology.declarations.codecs import (
    models_from_data,
    terms_from_data,
)
from bijux_ontology.errors import InputError
from bijux_ontology.jsonld.codecs import expectations_from_data

EX = "https://example.org/"


@pytest.mark.parametrize(
    "construct",
    [
        lambda: Value("literal", datatype="not an IRI"),
        lambda: Value("literal", model="resource"),
        lambda: Value("literal", types=(EX + "Record",)),
        lambda: Value("iri", language="en"),
        lambda: Value("literal", language="en", datatype=EX + "Custom"),
        lambda: Model(" ", (EX + "Record",)),
        lambda: Model(
            "record",
            (EX + "Record",),
            (
                Field("name", EX + "name", (Value("literal"),)),
                Field("name", EX + "other", (Value("literal"),)),
            ),
        ),
    ],
)
def test_invalid_variants_fail_at_the_shared_constructor(
    construct: Callable[[], object],
) -> None:
    with pytest.raises(InputError):
        construct()


def test_mapping_ingestion_matches_python_contracts_and_rejects_unknown_fields() -> (
    None
):
    assert terms_from_data([EX + "Record"]) == (TermDeclaration(EX + "Record"),)
    expected = Model(
        "record",
        (EX + "Record",),
        (
            Field(
                "title",
                EX + "title",
                (
                    Value(
                        "literal",
                        datatype=str(XSD.string),
                        samples=(Literal("Example"),),
                    ),
                ),
                emission="guaranteed",
            ),
        ),
    )
    data = [
        {
            "identifier": "record",
            "types": [EX + "Record"],
            "fields": [
                {
                    "name": "title",
                    "predicate": EX + "title",
                    "emission": "guaranteed",
                    "values": [
                        {
                            "kind": "literal",
                            "datatype": str(XSD.string),
                            "samples": [{"lexical": "Example"}],
                        }
                    ],
                }
            ],
        }
    ]
    assert models_from_data(data) == (expected,)
    with pytest.raises(InputError, match="Unknown declaration keys"):
        models_from_data([{"identifier": "record", "types": [], "guessed": True}])
    with pytest.raises(InputError):
        models_from_data(
            [
                {
                    "identifier": "record",
                    "types": [],
                    "fields": [
                        {
                            "name": "title",
                            "predicate": EX + "title",
                            "values": [{"kind": "literal", "datatype": "bad IRI"}],
                        }
                    ],
                }
            ]
        )
    selected = expectations_from_data(
        [{"path": ["title"], "predicate": EX + "title", "language": {"absent": True}}]
    )
    assert selected[0].language is ABSENT is ROOT_ABSENT is SHARED_ABSENT
    with pytest.raises(InputError):
        expectations_from_data(
            [{"path": ["title"], "predicate": EX + "title", "language": {"absent": 1}}]
        )


def test_declaration_errors_and_successful_mappings_keep_their_input_source() -> None:
    broken = [
        {
            "identifier": "record",
            "types": [],
            "fields": [
                {
                    "name": "title",
                    "predicate": EX + "title",
                    "values": [{"kind": "literal", "datatype": "bad IRI"}],
                }
            ],
        }
    ]
    with pytest.raises(InputError) as failure:
        models_from_data(broken, source="models.json")
    assert failure.value.location == "models.json#/0/fields/0/values/0"
    assert failure.value.__cause__ is not None
    term = terms_from_data([EX + "Missing"], source="inventory.json")[0]
    assert term.location == "inventory.json#/0"
    with pytest.raises(InputError) as context_failure:
        expectations_from_data(
            [{"path": ["title"], "predicate": "bad IRI"}], source="context.json"
        )
    assert context_failure.value.location == "context.json#/0"
    assert context_failure.value.__cause__ is not None


def test_closed_coverage_python_and_json_share_alias_and_metadata_invariants() -> None:
    coverage = EmissionCoverage(
        extensions=[EX + "extension"],  # type: ignore[arg-type]
        reverse_extensions=(EX + "incoming",),
        metadata=("@context", "@id"),
        aliases=(("headline", "title"),),
    )
    data = {
        "identifier": "record",
        "types": [],
        "fields": [
            {
                "name": "title",
                "predicate": EX + "title",
                "values": [{"kind": "literal"}],
            }
        ],
        "coverage": {
            "extensions": [EX + "extension"],
            "reverse_extensions": [EX + "incoming"],
            "metadata": ["@context", "@id"],
            "aliases": [["headline", "title"]],
        },
    }
    model = models_from_data([data])[0]
    assert model.coverage == coverage
    assert coverage.extensions == (EX + "extension",)
    for invalid in (
        {"aliases": [["headline", "absent"]]},
        {"aliases": [["title", "title"]]},
        {"aliases": [["alias", "title"], ["alias", "title"]]},
        {"aliases": [["@reverse", "title"]]},
        {"aliases": [["alias"]]},
        {"metadata": ["@graph"]},
        {"extensions": ["relative"]},
        {"invented": True},
    ):
        with pytest.raises(InputError):
            models_from_data([{**data, "coverage": invalid}])


@pytest.mark.parametrize("field", ("index", "list_index"))
def test_index_traits_preserve_exact_absent_and_unchecked_json(field: str) -> None:
    base = {"path": ["items"], "predicate": EX + "items"}
    assert getattr(expectations_from_data([base])[0], field) is None
    for raw, wanted in (
        (None, None),
        ({"absent": True}, ABSENT),
        ("", ""),
        ("chapter", "chapter"),
    ):
        selected = expectations_from_data([{**base, field: raw}])[0]
        assert getattr(selected, field) == wanted
    invalid_values: tuple[object, ...] = (False, [], {"absent": 1})
    for invalid in invalid_values:
        with pytest.raises(InputError):
            expectations_from_data([{**base, field: invalid}], source="indexes.json")
