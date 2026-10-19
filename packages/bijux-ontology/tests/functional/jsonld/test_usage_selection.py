"""Complete semantic occurrences, explicit selectors and scoped value contracts."""

from copy import deepcopy

import pytest
from rdflib import Graph, Literal, URIRef

from bijux_ontology.declarations import ABSENT
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.jsonld.checking import check_context, prepare_usages
from bijux_ontology.jsonld.conversion import convert_to_rdf
from bijux_ontology.jsonld.evidence.values import ValueKind
from bijux_ontology.jsonld.expectations import ContextExpectation

EX = "https://example.org/"


@pytest.mark.parametrize(
    ("definition", "value", "kind", "language"),
    [
        (
            {"@id": EX + "p", "@container": "@language"},
            {"en": ["one", "two"], "fr": "trois", "@none": "four"},
            "literal",
            "en",
        ),
        (
            {"@id": EX + "p", "@container": "@index"},
            {"a": {"@id": EX + "a"}, "b": "literal"},
            "iri",
            None,
        ),
    ],
)
def test_map_comparison_observes_every_member(
    definition: dict[str, object], value: object, kind: ValueKind, language: str | None
) -> None:
    document = {"@context": {"p": definition}, "p": value}
    before = deepcopy(document)
    prepared = prepare_usages(document)
    observed = prepared.observe(("p",))
    assert len(observed.occurrences) == (4 if language else 2)
    assert prepared.check([ContextExpectation(("p",), EX + "p")]).verified
    report = prepared.check(
        [ContextExpectation(("p",), EX + "p", value_kind=kind, language=language)]
    )
    assert report.exit_code() == 1
    assert not report.verified
    assert document == before
    if language:
        assert {issue.location for issue in report.issues} == {"/p/fr", "/p/@none"}


def test_type_and_identifier_maps_and_equivalent_list_forms() -> None:
    document = {
        "@context": {
            "@vocab": EX,
            "byId": {"@id": EX + "byId", "@container": "@id"},
            "byType": {"@id": EX + "byType", "@container": "@type"},
            "implicit": {"@id": EX + "parts", "@container": "@list"},
            "explicit": EX + "parts",
        },
        "byId": {EX + "a": {"name": "A"}, "@none": {"name": "B"}},
        "byType": {"Image": {"name": "A"}},
        "implicit": ["left", "right"],
        "explicit": {"@list": ["left", "right"]},
    }
    prepared = prepare_usages(document)
    expectations = [
        ContextExpectation(("byId",), EX + "byId", value_kind="node"),
        ContextExpectation(
            ("byType",), EX + "byType", value_kind="node", types=(EX + "Image",)
        ),
        ContextExpectation(
            ("implicit",), EX + "parts", value_kind="literal", container="list"
        ),
        ContextExpectation(
            ("explicit",), EX + "parts", value_kind="literal", container="list"
        ),
    ]
    assert prepared.check(expectations).verified, prepared.check(expectations).to_dict()
    wrong = prepared.check(
        [
            ContextExpectation(
                ("explicit",),
                EX + "parts",
                ordered_values=({"@value": "right"}, {"@value": "left"}),
            )
        ]
    )
    assert [issue.code for issue in wrong.issues] == ["context.order_mismatch"]
    wrong_type = prepared.check(
        [
            ContextExpectation(("byType",), EX + "byType", types=(EX + "Wrong",)),
        ]
    )
    assert [issue.code for issue in wrong_type.issues] == ["context.types_mismatch"]


def test_nested_keywords_reverse_edges_and_graph_membership() -> None:
    document = {
        "@context": {
            "@vocab": EX,
            "identifier": "@id",
            "kind": "@type",
            "parent": {"@reverse": EX + "child"},
        },
        "parent": {"identifier": EX + "parent", "kind": "Image", "name": "Parent"},
        "@included": [{"identifier": EX + "included", "name": "Included"}],
        "@graph": [{"identifier": EX + "inside", "name": "Inside"}],
        "identifier": EX + "graph",
    }
    prepared = prepare_usages(document)
    report = prepared.check(
        [
            ContextExpectation(("parent",), EX + "child", reverse=True),
            ContextExpectation(("parent", "name"), EX + "name", reverse=False),
            ContextExpectation(
                ("parent", "identifier"), "@id", identifier=EX + "parent"
            ),
            ContextExpectation(("parent", "kind"), "@type", identifier=EX + "Image"),
            ContextExpectation(("@included", 0, "name"), EX + "name"),
            ContextExpectation(("@graph", 0, "name"), EX + "name"),
        ]
    )
    assert report.verified, report.to_dict()
    assert prepared.observe(("@graph", 0, "name")).occurrences[0].graph == EX + "graph"
    wrong = prepared.check(
        [ContextExpectation(("parent",), EX + "child", reverse=False)]
    )
    assert [issue.code for issue in wrong.issues] == ["context.reverse_mismatch"]


def test_identifier_categories_absence_direction_and_opaque_literals() -> None:
    document = {
        "@context": {
            "@language": "de",
            "@direction": "ltr",
            "untagged": {"@id": EX + "untagged", "@language": None, "@direction": None},
            "iri": {"@id": EX + "iri", "@type": "@id"},
            "blank": {"@id": EX + "blank", "@type": "@id"},
            "relative": {"@id": EX + "relative", "@type": "@id"},
            "name": {"@id": EX + "name", "@language": "en", "@direction": "rtl"},
            "payload": {"@id": EX + "payload", "@type": "@json"},
            "@base": None,
        },
        "iri": EX + "target",
        "blank": "_:b",
        "relative": "unresolved",
        "name": "Text",
        "untagged": "Text without tags",
        "payload": {"nested": "opaque"},
    }
    prepared = prepare_usages(document)
    assert prepared.check(
        [
            ContextExpectation(
                ("iri",), EX + "iri", value_kind="iri", identifier=EX + "target"
            ),
            ContextExpectation(("blank",), EX + "blank", value_kind="blank"),
            ContextExpectation(("relative",), EX + "relative", value_kind="relative"),
            ContextExpectation(("name",), EX + "name", language="EN", direction="rtl"),
            ContextExpectation(
                ("untagged",), EX + "untagged", language=ABSENT, direction=ABSENT
            ),
            ContextExpectation(
                ("payload",),
                EX + "payload",
                datatype="http://www.w3.org/1999/02/22-rdf-syntax-ns#JSON",
            ),
        ]
    ).verified
    traits = prepared.observe(("name",)).occurrences[0].traits
    assert (traits.kind, traits.language, traits.direction) == ("literal", "en", "rtl")
    report = prepared.check(
        [
            ContextExpectation(("blank",), EX + "blank", value_kind="iri"),
            ContextExpectation(("relative",), EX + "relative", value_kind="iri"),
            ContextExpectation(
                ("name",), EX + "name", language=ABSENT, direction=ABSENT
            ),
        ]
    )
    assert report.exit_code() == 1
    assert {issue.code for issue in report.issues} == {
        "context.value_kind_mismatch",
        "context.language_mismatch",
        "context.direction_mismatch",
    }
    with pytest.raises(InputError, match="opaque"):
        prepared.observe(("payload", "nested"))
    with pytest.raises(InputError, match="context definitions"):
        prepared.observe(("@context", "name"))
    opaque_array = prepare_usages(
        {
            "@context": {"payload": {"@id": EX + "payload", "@type": "@json"}},
            "payload": [{"nested": "opaque"}],
        }
    )
    with pytest.raises(InputError, match="opaque"):
        opaque_array.observe(("payload", 0, "nested"))


def test_native_numeric_datatype_matches_jsonld_rdf_conversion() -> None:
    values = [1, 10**21, 1.5, True, 1.0, -0.0, 1e21]
    document = {
        "@context": {"@vocab": EX},
        **{f"n{index}": value for index, value in enumerate(values)},
    }
    prepared = prepare_usages(document)
    converted = convert_to_rdf(document)
    assert converted.report.verified
    graph = Graph().parse(data=converted.nquads, format="nt")
    for index, datatype in enumerate(
        ("integer", "double", "double", "boolean", "integer", "integer", "double")
    ):
        predicate = EX + f"n{index}"
        literal = next(graph.objects(predicate=URIRef(predicate)))
        assert isinstance(literal, Literal)
        observed = prepared.observe((f"n{index}",)).occurrences[0].traits
        assert str(literal.datatype) == observed.datatype
        report = prepared.check(
            [
                ContextExpectation(
                    (f"n{index}",),
                    predicate,
                    datatype="http://www.w3.org/2001/XMLSchema#" + datatype,
                )
            ]
        )
        assert report.verified, report.to_dict()


@pytest.mark.parametrize(
    "document",
    [
        {"@context": {"@vocab": EX}, "p": [{"name": "A"}, {"name": "B"}]},
        {
            "@context": {"p": {"@id": EX + "p", "@container": "@list"}},
            "p": ["A", "B", "A"],
        },
    ],
)
def test_indistinguishable_contributions_are_not_positionally_attributed(
    document: dict[str, object],
) -> None:
    prepared = prepare_usages(document)
    assert len(prepared.observe(("p",)).occurrences) >= 2
    usage = prepared.observe(("p", 0))
    assert usage.limitation
    assert not usage.occurrences
    report = prepared.check([ContextExpectation(("p", 0), EX + "p")])
    assert [issue.kind for issue in report.issues] == ["unverified"]


def test_empty_and_unordered_values_have_no_value_or_order_claim() -> None:
    document = {
        "@context": {
            "@vocab": EX,
            "languageMap": {"@id": EX + "p", "@container": "@language"},
        },
        "languageMap": {},
        "emptyList": {"@list": []},
        "unordered": ["A", "B"],
    }
    prepared = prepare_usages(document)
    assert prepared.observe(("languageMap",)).empty
    assert prepared.check(
        [
            ContextExpectation(
                ("emptyList",), EX + "emptyList", container="list", ordered_values=()
            )
        ]
    ).verified
    absent = prepared.check(
        [ContextExpectation(("emptyList",), EX + "emptyList", identifier=ABSENT)]
    )
    assert [issue.kind for issue in absent.issues] == ["unverified"]
    unordered = prepared.check(
        [
            ContextExpectation(
                ("unordered",),
                EX + "unordered",
                ordered_values=({"@value": "A"}, {"@value": "B"}),
            )
        ]
    )
    assert [issue.code for issue in unordered.issues] == ["context.order_unverified"]


@pytest.mark.parametrize(
    "field",
    [
        "predicate",
        "value_kind",
        "container",
        "direction",
        "identifier_kind",
        "ordered_values",
    ],
)
def test_malformed_expectations_are_consistent_input_errors(field: str) -> None:
    supplied: dict[str, object] = {"path": ("p",), "predicate": EX + "p", field: []}
    with pytest.raises(InputError):
        ContextExpectation(**supplied)  # type: ignore[arg-type]


def test_composed_scope_resets_custom_indexes_and_keyword_aliases() -> None:
    document = {
        "@context": {
            "@vocab": EX,
            "nest": "@nest",
            "list": "@list",
            "indexed": {
                "@id": EX + "indexed",
                "@container": "@index",
                "@index": "position",
            },
            "position": EX + "position",
            "typed": {"@id": EX + "typed", "@container": "@type"},
            "A": {"@id": EX + "A", "@context": {"name": EX + "AName"}},
            "B": {"@id": EX + "B", "@context": {"name": EX + "BName"}},
            "child": {
                "@id": EX + "child",
                "@context": {"@propagate": False, "name": EX + "childName"},
            },
            "reset": {
                "@id": EX + "reset",
                "@context": [None, {"@vocab": EX + "reset/"}],
            },
            "ref": {"@id": EX + "ref", "@type": "@id"},
            "voc": {"@id": EX + "voc", "@type": "@vocab"},
        },
        "@type": ["A", "B"],
        "nest": {"name": "Top"},
        "indexed": {"left": {"name": "Indexed"}},
        "typed": {"A": {"name": "Typed"}},
        "parts": {"list": ["a", "b"]},
        "child": {"name": "Child", "next": {"name": "Nested"}},
        "reset": {"name": "Reset"},
        "ref": "relative",
        "voc": "relative",
    }
    before = deepcopy(document)
    prepared = prepare_usages(document, base=EX + "data/")
    assert prepared.root_nodes[0].types == (EX + "A", EX + "B")
    expectations = [
        ContextExpectation(("nest", "name"), EX + "BName"),
        ContextExpectation(("typed", "A", "name"), EX + "AName"),
        ContextExpectation(
            ("parts",),
            EX + "parts",
            container="list",
            ordered_values=({"@value": "a"}, {"@value": "b"}),
        ),
        ContextExpectation(("child", "name"), EX + "childName"),
        ContextExpectation(("child", "next", "name"), EX + "name"),
        ContextExpectation(("reset", "name"), EX + "reset/name"),
        ContextExpectation(("ref",), EX + "ref", identifier=EX + "data/relative"),
        ContextExpectation(("voc",), EX + "voc", identifier=EX + "relative"),
    ]
    report = prepared.check(expectations)
    assert report.verified, report.to_dict()
    indexed = prepared.observe(("indexed", "left")).occurrences[0]
    assert indexed.value == {
        EX + "BName": [{"@value": "Indexed"}],
        EX + "position": [{"@value": "left"}],
    }
    wrong_target = prepared.check(
        [ContextExpectation(("voc",), EX + "voc", identifier=EX + "data/relative")]
    )
    assert [issue.code for issue in wrong_target.issues] == [
        "context.identifier_mismatch"
    ]
    assert document == before


def test_explicit_reversals_graph_containers_and_sets_preserve_scope() -> None:
    document = {
        "@context": {
            "@vocab": EX,
            "graphs": {"@id": EX + "graphs", "@container": ["@graph", "@id"]},
        },
        "@id": EX + "root",
        "@reverse": {
            "sibling": {
                "@id": EX + "sibling",
                "name": "Sibling",
                "@reverse": {"cousin": {"@id": EX + "cousin", "name": "Cousin"}},
            }
        },
        "graphs": {EX + "one": {"name": "Graph"}},
        "set": {"@set": ["A", "B"]},
    }
    prepared = prepare_usages(document)
    report = prepared.check(
        [
            ContextExpectation(("@reverse", "sibling"), EX + "sibling", reverse=True),
            ContextExpectation(("@reverse", "sibling", "name"), EX + "name"),
            ContextExpectation(
                ("@reverse", "sibling", "@reverse", "cousin"),
                EX + "cousin",
                reverse=True,
            ),
            ContextExpectation(
                ("@reverse", "sibling", "@reverse", "cousin", "name"), EX + "name"
            ),
            ContextExpectation(("graphs", EX + "one", "name"), EX + "name"),
            ContextExpectation(
                ("set",), EX + "set", container="unordered", value_kind="literal"
            ),
        ]
    )
    assert report.verified, report.to_dict()
    graph_name = prepared.observe(("graphs", EX + "one", "name")).occurrences[0]
    assert graph_name.graph == EX + "one"
    assert graph_name.location == "/graphs/https:~1~1example.org~1one/name"
    protected = {
        "@context": [{"@protected": True, "name": EX + "name"}, {"name": EX + "other"}],
        "name": "Text",
    }
    invalid = check_context(protected, [ContextExpectation(("name",), EX + "name")])
    assert invalid.outcome == "input_error"


def test_equal_alias_values_keep_independent_language_and_location() -> None:
    prepared = prepare_usages(
        {
            "@context": {
                "english": {"@id": EX + "name", "@language": "en"},
                "french": {"@id": EX + "name", "@language": "fr"},
            },
            "english": "Same",
            "french": "Same",
        }
    )
    assert prepared.check(
        [
            ContextExpectation(("english",), EX + "name", language="en"),
            ContextExpectation(("french",), EX + "name", language="fr"),
        ]
    ).verified
    wrong = prepared.check(
        [ContextExpectation(("french",), EX + "name", language="en")]
    )
    assert [issue.location for issue in wrong.issues] == ["/french"]
    assert [issue.actual for issue in wrong.issues] == ["fr"]


@pytest.mark.parametrize("lists", [[["A"], ["B"]], [[], ["A"]]])
def test_order_expectations_do_not_concatenate_separate_lists(
    lists: list[list[str]],
) -> None:
    document = {
        "@context": {"p": EX + "p"},
        "p": [{"@list": values} for values in lists],
    }
    before = deepcopy(document)
    prepared = prepare_usages(document)
    report = prepared.check(
        [
            ContextExpectation(
                ("p",),
                EX + "p",
                ordered_values=tuple(
                    {"@value": value} for values in lists for value in values
                ),
            )
        ]
    )
    assert report.outcome == "unverified"
    assert [issue.code for issue in report.issues] == ["context.order_unverified"]
    assert prepared.check(
        [
            ContextExpectation(
                ("p", 0),
                EX + "p",
                ordered_values=tuple({"@value": value} for value in lists[0]),
            )
        ]
    ).verified
    assert document == before
