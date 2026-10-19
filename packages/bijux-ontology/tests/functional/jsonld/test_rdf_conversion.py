"""Actual RDF conversion preserves controlled resources and reports omitted edges."""

from typing import cast

import pytest
from rdflib import Dataset, Literal, URIRef
from rdflib.collection import Collection

from bijux_ontology.jsonld.expectations import ContextExpectation
from bijux_ontology.jsonld.processing.conversion import convert_to_rdf
from bijux_ontology.jsonld.processing.expansion import expand_document
from bijux_ontology.jsonld.processing.resources import (
    DocumentLoader,
    context_document_loader,
)
from bijux_ontology.jsonld.usages.checking import check_context

EX = "https://example.org/"


def test_conversion_retains_named_graph_lists_json_and_frozen_resources() -> None:
    calls: list[str] = []

    def changing_loader(url: str, options: dict[str, object]) -> dict[str, object]:
        """Expose accidental second reads by returning a different predicate."""
        calls.append(url)
        return {
            "contextUrl": None,
            "documentUrl": url,
            "document": {"@context": {"p": EX + ("p" if len(calls) == 1 else "q")}},
        }

    document = {
        "@context": EX + "context",
        "@id": EX + "graph",
        "@graph": [
            {
                "@id": EX + "subject",
                "p": [
                    {"@list": ["left", "right"]},
                    {"@value": {"x": 1}, "@type": "@json"},
                ],
            }
        ],
    }
    resources = context_document_loader(changing_loader)
    checked = check_context(
        document,
        [ContextExpectation(("@graph", 0, "p"), EX + "p")],
        document_loader=resources,
    )
    assert checked.verified, checked.to_dict()
    converted = convert_to_rdf(document, document_loader=resources)
    assert converted.report.verified, converted.report.to_dict()
    graph = Dataset().parse(data=converted.nquads, format="nquads")
    named = graph.graph(URIRef(EX + "graph"))
    assert (URIRef(EX + "subject"), URIRef(EX + "p"), None) in named
    assert len(graph.default_graph) == 0
    head = next(
        value
        for value in named.objects(URIRef(EX + "subject"), URIRef(EX + "p"))
        if not isinstance(value, Literal)
    )
    assert list(Collection(named, head)) == [Literal("left"), Literal("right")]
    assert any(
        value.datatype == URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#JSON")
        for value in named.objects()
        if isinstance(value, Literal)
    )
    assert calls == [EX + "context"]


@pytest.mark.parametrize(
    ("document", "code"),
    [
        (
            {"@context": {"p": {"@id": EX + "p", "@type": "@id"}}, "p": "relative"},
            "context.rdf_identifier_loss",
        ),
        (
            {"@context": {"@base": None}, "@id": "graph", "@graph": [{EX + "p": "x"}]},
            "context.rdf_identifier_loss",
        ),
        ({"_:predicate": "x"}, "context.rdf_identifier_loss"),
        (
            {EX + "p": {"@value": "x", "@direction": "rtl"}},
            "context.rdf_direction_loss",
        ),
        (
            {EX + "p": {"@value": "x", "@language": "en_us"}},
            "context.rdf_language_invalid",
        ),
        ({"misspelt": "x"}, "context.property_dropped"),
        ({"@id": EX + "empty", "@graph": []}, "context.rdf_empty_graph_loss"),
    ],
)
def test_preservation_never_accepts_silent_rdf_loss(
    document: dict[str, object], code: str
) -> None:
    converted = convert_to_rdf(document)
    assert not converted.report.verified
    assert code in {issue.code for issue in converted.report.issues}
    assert converted.nquads == ""
    permitted_output = convert_to_rdf(document, preserve=False)
    assert not permitted_output.report.verified
    assert "http://example.org/base/" not in permitted_output.nquads


def test_base_direction_and_index_conversion_have_explicit_semantics() -> None:
    relative = {"@context": {"p": {"@id": EX + "p", "@type": "@id"}}, "p": "relative"}
    assert expand_document(relative)[0][EX + "p"] == [{"@id": "relative"}]
    resolved = convert_to_rdf(relative, base=EX + "data/")
    assert resolved.report.verified
    assert "<" + EX + "data/relative>" in resolved.nquads
    indexed = {
        "@context": {"p": {"@id": EX + "p", "@container": "@index"}},
        "p": {"one": {"@value": "text", "@direction": "rtl"}},
    }
    converted = convert_to_rdf(indexed, direction="i18n-datatype")
    assert converted.report.verified
    assert "https://www.w3.org/ns/i18n#_rtl" in converted.nquads
    assert "one" not in converted.nquads


def test_conversion_preserves_custom_loader_programmer_failures() -> None:
    def failing_loader(url: str, options: dict[str, object]) -> dict[str, object]:
        """The supplied callback remains trusted caller code, not malformed JSON."""
        raise RuntimeError("loader bug")

    with pytest.raises(RuntimeError, match="loader bug"):
        convert_to_rdf(
            {"@context": EX + "ctx"},
            document_loader=cast(DocumentLoader, failing_loader),
        )


def test_explicit_literal_lexical_loss_retains_other_values() -> None:
    double = "http://www.w3.org/2001/XMLSchema#double"
    document = {EX + "p": {"@value": "1.00", "@type": double}, EX + "unrelated": "1.00"}
    converted = convert_to_rdf(document)
    assert converted.nquads == ""
    assert [issue.code for issue in converted.report.issues] == [
        "context.rdf_literal_loss"
    ]
    permitted = convert_to_rdf(document, preserve=False)
    assert '"1.0E0"^^' in permitted.nquads
    assert '"1.00" .' in permitted.nquads
    assert not permitted.report.verified
    lexical = {
        EX + "p": [
            {"@value": "01", "@type": "http://www.w3.org/2001/XMLSchema#integer"},
            {"@value": "1", "@type": "http://www.w3.org/2001/XMLSchema#integer"},
            {"@value": " a\n  b ", "@type": "http://www.w3.org/2001/XMLSchema#token"},
        ]
    }
    preserved = convert_to_rdf(lexical)
    assert preserved.report.verified, preserved.report.to_dict()
    assert '"01"^^' in preserved.nquads
    assert '"1"^^' in preserved.nquads
    assert '" a\\n  b "^^' in preserved.nquads
