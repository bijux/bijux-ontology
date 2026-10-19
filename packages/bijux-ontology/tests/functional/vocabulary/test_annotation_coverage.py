"""Selected publication coverage has explicit kinds, languages and fallback."""

from dataclasses import replace
from typing import Any

import pytest
from rdflib import BNode, Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.absence import ABSENT
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.terms import literal_term
from bijux_ontology.vocabulary.annotations import (
    AnnotationRequirement,
    LanguageRequirement,
    check_annotation_coverage,
)
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.languages import negotiate_annotations
from bijux_ontology.vocabulary.sources.view import Vocabulary

EX = Namespace("https://example.org/coverage/")


def test_selected_class_languages_do_not_borrow_another_entity_or_imports() -> None:
    local = Graph().add((EX.Class, RDF.type, OWL.Class))
    local.add((EX.Class, RDFS.label, Literal("class", lang="en")))
    local.add((EX.Unselected, RDFS.label, Literal("Klasse", lang="de")))
    imported = Graph().add((EX.Class, RDFS.label, Literal("Klasse", lang="de")))
    imported.add((EX.ImportedClass, RDF.type, OWL.Class))
    source = Vocabulary(local, imports=(imported,))
    required = AnnotationRequirement(
        str(RDFS.label),
        languages=(
            LanguageRequirement("en"),
            LanguageRequirement("de"),
        ),
    )
    view = inspect_vocabulary(source, scopes=("local",))
    result = check_annotation_coverage(view, [required], kinds=[str(OWL.Class)])
    assert result.report.outcome == "disagreement" and result.report.complete
    missing = [
        i
        for i in result.report.issues
        if i.code == "annotations.language_minimum_missing"
    ]
    assert len(missing) == 1 and missing[0].location == str(EX.Class)
    assert "language=de" in (missing[0].expected or "")
    assert len(result.items) == 1 and result.items[0].entity == EX.Class
    all_sources = inspect_vocabulary(source)
    local.remove((None, None, None))
    imported.remove((None, None, None))
    complete = check_annotation_coverage(
        all_sources, [required], entities=[str(EX.Class)]
    )
    assert complete.report.verified and len(complete.items[0].values) == 2


def test_filter_coverage_is_not_lookup_and_fallback_is_a_visible_explicit_tier() -> (
    None
):
    graph = Graph().add((EX.Class, RDFS.label, Literal("English", lang="en-GB")))
    view = inspect_vocabulary(graph)
    exact = AnnotationRequirement(
        str(RDFS.label), languages=(LanguageRequirement("en"),)
    )
    assert not check_annotation_coverage(
        view, [exact], entities=[str(EX.Class)]
    ).report.verified
    basic = replace(exact, languages=(LanguageRequirement("en", mode="basic"),))
    assert check_annotation_coverage(
        view, [basic], entities=[str(EX.Class)]
    ).report.verified
    regional = replace(exact, languages=(LanguageRequirement("en-US", mode="basic"),))
    assert not check_annotation_coverage(
        view, [regional], entities=[str(EX.Class)]
    ).report.verified
    fallback = replace(
        exact, languages=(LanguageRequirement("en-US", fallback=("en-GB",)),)
    )
    result = check_annotation_coverage(view, [fallback], entities=[str(EX.Class)])
    language = result.items[0].languages[0]
    assert (
        result.report.verified
        and language.used_fallback
        and language.selected == "en-GB"
    )
    graph.set((EX.Class, RDFS.label, Literal("English", lang="en")))
    plain = inspect_vocabulary(graph)
    assert negotiate_annotations(
        plain, str(EX.Class), predicates=[str(RDFS.label)], ranges=["en-US"]
    ).items
    assert not check_annotation_coverage(
        plain, [regional], entities=[str(EX.Class)]
    ).report.verified
    graph.add((EX.Class, RDFS.label, Literal("eins", lang="de")))
    graph.add((EX.Class, RDFS.label, Literal("zwei", lang="de")))
    deficit = replace(
        exact, languages=(LanguageRequirement("en", minimum=2, fallback=("de",)),)
    )
    result = check_annotation_coverage(
        inspect_vocabulary(graph), [deficit], entities=[str(EX.Class)]
    )
    assert not result.report.verified and not result.items[0].languages[0].used_fallback
    graph.set((EX.Class, RDFS.label, Literal("default")))
    untagged = replace(
        exact, languages=(LanguageRequirement("en", fallback=(ABSENT,)),)
    )
    result = check_annotation_coverage(
        inspect_vocabulary(graph), [untagged], entities=[str(EX.Class)]
    )
    assert result.report.verified and result.items[0].languages[0].selected is ABSENT
    graph.set((EX.Class, RDFS.label, Literal("Deutsch", lang="de-Latn-DE")))
    extended = replace(
        exact, languages=(LanguageRequirement("de-*-DE", mode="extended"),)
    )
    assert check_annotation_coverage(
        inspect_vocabulary(graph), [extended], entities=[str(EX.Class)]
    ).report.verified


def test_original_values_remain_visible_and_exact_distinct_terms_determine_counts() -> (
    None
):
    graph = Graph().add((EX.Class, RDFS.label, EX.NotALiteral))
    rule = AnnotationRequirement(str(RDFS.label))
    result = check_annotation_coverage(
        inspect_vocabulary(graph), [rule], entities=[str(EX.Class)]
    )
    assert result.items[0].values[0].object == EX.NotALiteral
    assert any(i.code == "annotations.value_kind" for i in result.report.issues)
    resources = replace(rule, value_kinds=("iri", "blank"))
    graph.add((EX.Class, RDFS.label, BNode()))
    assert check_annotation_coverage(
        inspect_vocabulary(graph), [resources], entities=[str(EX.Class)]
    ).report.verified
    graph = Graph().add((EX.Class, RDFS.label, Literal("plain")))
    typed = replace(rule, datatypes=(str(XSD.string),))
    assert check_annotation_coverage(
        inspect_vocabulary(graph), [typed], entities=[str(EX.Class)]
    ).report.verified
    graph.add((EX.Class, RDFS.label, literal_term("01", datatype=str(XSD.integer))))
    result = check_annotation_coverage(
        inspect_vocabulary(graph), [typed], entities=[str(EX.Class)]
    )
    assert any(i.code == "annotations.datatype" for i in result.report.issues)
    graph.remove((None, None, None))
    graph.add((EX.Class, RDFS.label, literal_term("01", datatype=str(XSD.integer))))
    graph.add((EX.Class, RDFS.label, literal_term("1", datatype=str(XSD.integer))))
    numeric = replace(rule, datatypes=(str(XSD.integer),), minimum=2, maximum=2)
    assert check_annotation_coverage(
        inspect_vocabulary(graph), [numeric], entities=[str(EX.Class)]
    ).report.verified
    local = Graph().add((EX.Class, RDFS.label, Literal("same")))
    imported = Graph().add((EX.Class, RDFS.label, Literal("same", datatype=XSD.string)))
    source = Vocabulary(local, imports=(imported,))
    result = check_annotation_coverage(
        inspect_vocabulary(source), [replace(rule, minimum=2)], entities=[str(EX.Class)]
    )
    assert not result.report.verified and len(result.items[0].values) == 2


def test_known_shape_and_maximum_failures_survive_later_incomplete_scope() -> None:
    graph = Graph().add((EX.First, RDFS.label, Literal("a", lang="en")))
    graph.add((EX.First, RDFS.label, Literal("b", lang="en")))
    graph.add((EX.First, RDFS.label, EX.NotLiteral))
    graph.add((EX.Last, RDFS.label, Literal("z" * 2000)))
    rule = AnnotationRequirement(
        str(RDFS.label), maximum=1, languages=(LanguageRequirement("en", maximum=1),)
    )
    result = check_annotation_coverage(
        inspect_vocabulary(graph),
        [rule],
        entities=[str(EX.First), str(EX.Last)],
        budget=Budget(Limits(max_work=1000)),
    )
    assert result.report.outcome == "exhausted" and not result.report.complete
    codes = {issue.code for issue in result.report.issues}
    assert {
        "annotations.value_kind",
        "annotations.maximum_exceeded",
        "annotations.language_maximum_exceeded",
    } <= codes
    incomplete = check_annotation_coverage(
        inspect_vocabulary(graph),
        [rule],
        entities=[str(EX.First)],
        budget=Budget(Limits(max_work=1)),
    )
    assert not any(i.code.endswith("minimum_missing") for i in incomplete.report.issues)
    assert incomplete.report.outcome == "exhausted"


def test_finite_owned_contracts_and_explicit_entity_uncertainty() -> None:
    languages = [LanguageRequirement("en")]
    kinds = ["literal"]
    supplied: dict[str, Any] = {"value_kinds": kinds, "languages": languages}
    rule = AnnotationRequirement(str(RDFS.label), **supplied)
    languages.clear()
    kinds.clear()
    assert rule.value_kinds == ("literal",) and len(rule.languages) == 1
    invalid: tuple[Any, ...] = (None, "values", iter(()), [{}])
    for value in invalid:
        with pytest.raises(InputError):
            AnnotationRequirement(str(RDFS.label), value_kinds=value)
        with pytest.raises(InputError):
            LanguageRequirement("en", fallback=value)
    invalid_options: tuple[dict[str, Any], ...] = (
        {"minimum": -1},
        {"minimum": True},
        {"minimum": 2, "maximum": 1},
        {"datatypes": [BNode()]},
        {"languages": [None]},
    )
    for kwargs in invalid_options:
        with pytest.raises(InputError):
            AnnotationRequirement(str(RDFS.label), **kwargs)
    view = inspect_vocabulary(Graph())
    for requirement in (
        LanguageRequirement("en-US"),
        LanguageRequirement("en", mode="basic"),
    ):
        assert requirement.fallback == ()
    for mode in ("lookup", "other"):
        with pytest.raises(InputError):
            LanguageRequirement("en", mode=mode)  # type: ignore[arg-type]
    unknown = check_annotation_coverage(view, [rule], entities=[str(EX.Missing)])
    assert unknown.report.outcome == "unverified" and not unknown.report.complete
    assert not any(i.code.endswith("minimum_missing") for i in unknown.report.issues)
    for kwargs in (
        {"entities": [str(EX.Missing)], "kinds": [str(OWL.Class)]},
        {"entities": [BNode()]},
        {"namespaces": [None]},
    ):
        options: dict[str, Any] = kwargs
        with pytest.raises(InputError):
            check_annotation_coverage(view, [rule], **options)
    bounded_view = inspect_vocabulary(Graph().add((EX.Class, RDFS.label, Literal("x"))))
    large = check_annotation_coverage(
        bounded_view, [replace(rule, minimum=2**20000)], entities=[str(EX.Class)]
    )
    assert large.report.outcome == "disagreement"
    assert "bound=0x" in (large.report.issues[0].expected or "")
