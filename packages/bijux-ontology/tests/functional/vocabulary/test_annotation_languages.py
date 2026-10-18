"""RFC language matching stays distinct from exact annotation identity/coverage."""

from typing import Any

import pytest
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import RDFS, SKOS

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.vocabulary.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.languages import matches_language, negotiate_annotations
from bijux_ontology.vocabulary.view import Vocabulary

EX = Namespace("https://example.org/languages/")
PREDICATES = (str(RDFS.label), str(SKOS.definition))


def graph(*labels: tuple[str, str | None]) -> Graph:
    result = Graph()
    for value, language in labels:
        result.add((EX.Record, RDFS.label, Literal(value, lang=language)))
    return result


def test_regional_lookup_preserves_ties_and_exact_requested_tag_priority() -> None:
    source = graph(("français", "fr"), ("autre", "FR"), ("default", None))
    view = inspect_vocabulary(source)
    selected = negotiate_annotations(
        view,
        str(EX.Record),
        predicates=PREDICATES,
        ranges=("fr-CA",),
        fallback=("untagged",),
    )
    assert selected.report.verified
    assert {item.value for item in selected.items} == {"français", "autre"}
    assert all(item.term.language is not None for item in selected.items)
    source.add((EX.Record, SKOS.definition, Literal("Québec", lang="fr-CA")))
    selected = negotiate_annotations(
        inspect_vocabulary(source),
        str(EX.Record),
        predicates=PREDICATES,
        ranges=("fr-CA",),
        fallback=("untagged",),
    )
    assert [item.value for item in selected.items] == ["Québec"]
    assert selected.items[0].predicate == str(SKOS.definition)
    regional = inspect_vocabulary(graph(("France", "fr-FR")))
    assert not negotiate_annotations(
        regional,
        str(EX.Record),
        predicates=PREDICATES,
        ranges=("fr-CA",),
    ).items
    assert not matches_language("fr-FR", "fr-CA")
    assert not matches_language("fr", "fr-CA")


def test_rfc_filtering_examples_and_extension_truncation() -> None:
    for tag in (
        "de-DE",
        "de-Latn-DE",
        "de-Latf-DE",
        "de-DE-x-goethe",
        "de-Latn-DE-1996",
        "de-Deva-DE",
    ):
        assert matches_language(tag, "de-*-DE", extended=True)
        assert matches_language(tag, "de-DE", extended=True)
    for tag in ("de", "de-x-DE", "de-Deva"):
        assert not matches_language(tag, "de-*-DE", extended=True)
    assert matches_language("de-DE-1996", "de-DE")
    assert not matches_language("de-Latn-DE", "de-DE")
    assert matches_language("fr-CA", "*") and not matches_language(None, "*")
    assert matches_language("fr-CA", "*-CA", extended=True)
    for invalid in ("", "en_US", "en--US", "en-*", "*;"):
        with pytest.raises(InputError):
            matches_language("en", invalid)
    with pytest.raises(InputError):
        matches_language("not_a_tag", "*")
    source = graph(("base", "zh-Hant-CN"), ("private", "zh-Hant-CN-x-private1"))
    selected = negotiate_annotations(
        inspect_vocabulary(source),
        str(EX.Record),
        predicates=PREDICATES,
        ranges=("zh-Hant-CN-x-private1-private2",),
    )
    assert [item.value for item in selected.items] == ["private"]
    with pytest.raises(InputError):
        negotiate_annotations(
            inspect_vocabulary(source),
            str(EX.Record),
            predicates=PREDICATES,
            ranges=("zh-*-CN",),
            mode="lookup",
        )


def test_filtering_keeps_all_terms_while_fallback_waits_for_every_request() -> None:
    source = graph(
        ("France", "fr-FR"), ("Canada", "fr-CA"), ("English", "en"), ("default", None)
    )
    view = inspect_vocabulary(source)
    selected = negotiate_annotations(
        view,
        str(EX.Record),
        predicates=PREDICATES,
        ranges=("fr",),
        mode="basic-filter",
        fallback=("untagged",),
    )
    assert {item.language for item in selected.items} == {"fr-CA", "fr-FR"}
    selected = negotiate_annotations(
        view,
        str(EX.Record),
        predicates=PREDICATES,
        ranges=("ja", "*", "en"),
        fallback=("untagged",),
    )
    assert [item.value for item in selected.items] == ["English"]
    assert not negotiate_annotations(
        view,
        str(EX.Record),
        predicates=PREDICATES,
        ranges=("*",),
    ).items
    untagged = negotiate_annotations(
        view,
        str(EX.Record),
        predicates=PREDICATES,
        ranges=("ja",),
        fallback=("untagged", "other"),
    )
    assert [item.value for item in untagged.items] == ["default"]
    other = negotiate_annotations(
        view,
        str(EX.Record),
        predicates=PREDICATES,
        ranges=("*",),
        fallback=("other",),
    )
    assert {item.language for item in other.items} == {"en", "fr-CA", "fr-FR"}
    extended = negotiate_annotations(
        view,
        str(EX.Record),
        predicates=PREDICATES,
        ranges=("*-CA",),
        mode="extended-filter",
    )
    assert [item.value for item in extended.items] == ["Canada"]


def test_source_priorities_never_escape_the_selected_view_and_terms_stay_owned() -> (
    None
):
    local = graph(("local", "fr"))
    imported = graph(("imported", "fr"), ("exact", "fr-CA"))
    lookup = graph(("lookup", "de"))
    vocabulary = Vocabulary(local, imports=(imported,), lookup=(lookup,))
    view = inspect_vocabulary(vocabulary)
    for source in (local, imported, lookup):
        source.remove((None, None, None))
    options: dict[str, Any] = {"predicates": PREDICATES, "ranges": ("fr",)}
    selected = negotiate_annotations(view, str(EX.Record), **options)
    assert [item.value for item in selected.items] == ["local"]
    selected = negotiate_annotations(
        view,
        str(EX.Record),
        **options,
        source_priority=("imports", "local"),
    )
    assert [item.value for item in selected.items] == ["imported"]
    assert selected.items[0].scope == "import[0]"
    exact = negotiate_annotations(
        view, str(EX.Record), predicates=PREDICATES, ranges=("fr-CA",)
    )
    assert [item.value for item in exact.items] == ["exact"]
    assert not negotiate_annotations(
        view,
        str(EX.Record),
        predicates=PREDICATES,
        ranges=("de",),
        source_priority=("lookup",),
        fallback=("other",),
    ).items
    all_sources = inspect_vocabulary(vocabulary, scopes=("local", "imports", "lookup"))
    assert not negotiate_annotations(
        all_sources,
        str(EX.Record),
        predicates=PREDICATES,
        ranges=("de",),
        source_priority=("local",),
    ).items
    assert negotiate_annotations(
        all_sources,
        str(EX.Record),
        predicates=PREDICATES,
        ranges=("de",),
        source_priority=("lookup",),
    ).items[0].term == Literal("lookup", lang="de")


def test_runtime_boundaries_and_partial_selection_are_explicit() -> None:
    view = inspect_vocabulary(graph(("one", "en"), ("two", "en")))
    invalid: tuple[Any, ...] = (None, "ranges", iter(()), [{}])
    for value in invalid:
        with pytest.raises(InputError):
            negotiate_annotations(
                view, str(EX.Record), predicates=PREDICATES, ranges=value
            )
    invalid_options: tuple[dict[str, Any], ...] = (
        {"fallback": [None]},
        {"fallback": ["other", "other"]},
        {"source_priority": [{}]},
        {"source_priority": ["local", "local"]},
        {"predicates": []},
        {"mode": "exact"},
    )
    for kwargs in invalid_options:
        options: dict[str, Any] = {"predicates": PREDICATES, "ranges": ["en"], **kwargs}
        with pytest.raises(InputError):
            negotiate_annotations(view, str(EX.Record), **options)
    for limits in (Limits(max_work=1), Limits(max_states=1), Limits(max_total_bytes=5)):
        result = negotiate_annotations(
            view,
            str(EX.Record),
            predicates=PREDICATES,
            ranges=("en",),
            budget=Budget(limits),
        )
        assert result.report.outcome == "exhausted" and not result.report.complete
    with pytest.raises(LimitExceeded):
        matches_language(
            "de-Latn-DE", "de-*-DE", extended=True, budget=Budget(Limits(max_work=2))
        )
    long_range = "en-" + "-".join(["abcdefgh"] * 100)
    result = negotiate_annotations(
        view,
        str(EX.Record),
        predicates=PREDICATES,
        ranges=(long_range,),
        budget=Budget(Limits(max_work=1500)),
    )
    assert result.report.outcome == "exhausted"
