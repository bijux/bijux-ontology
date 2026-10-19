"""Normally installed native RDFa, exact host semantics and bounded ownership."""

import os
import threading
import tracemalloc
from pathlib import Path
from typing import cast

import pytest
from rdflib import Graph, URIRef
from rdflib.namespace import RDF

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    OutputRequirement,
    SubjectIdentity,
    ValueDeclaration,
)
from bijux_ontology.errors import InputError
from bijux_ontology.interfaces.adapters.rdfa import (
    RdfaRuntime,
    extract_rdfa,
    prepare_rdfa_runtime,
)
from bijux_ontology.jsonld.html.extraction import extract_html_jsonld
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.structure import iter_rdf_list
from bijux_ontology.rdf.terms import literal_term

URL = "https://example.org/document"
EX = "https://example.org/vocab#"
NESTED = "https://example.org/nested#"
SUBJECT = URIRef("https://example.org/base/item")
HTML = (
    f'<!DOCTYPE html><html lang="en" prefix="ex: {EX}"><head>'
    '<base href="https://example.org/base/"><title>RDFa</title>'
    '<script id="companion" type="application/ld+json">'
    f'{{"@context":{{"ex":"{EX}"}},"@id":"item","@type":"ex:Thing",'
    '"ex:label":{"@value":"Name","@language":"en"}}</script></head>'
    '<body about="item" typeof="ex:Thing"><span property="ex:label">Name</span>'
    f'<section prefix="ex: {NESTED}" lang="fr">'
    '<span property="ex:label">Nom</span></section>'
    '<a rev="ex:parent" href="parent"></a>'
    '<span property="ex:members" inlist="" content="one"></span>'
    '<span property="ex:members" inlist="" content="two"></span></body></html>'
)


@pytest.fixture
def runtime() -> RdfaRuntime:
    """Use an explicitly normally installed npm tree, never global discovery."""
    directory = os.environ.get("BIJUX_RDFA_RUNTIME")
    if directory is None:
        pytest.skip("RDFa native controls require an explicitly installed npm runtime")
    return RdfaRuntime(os.environ.get("BIJUX_RDFA_NODE", "node"), Path(directory))


def _model() -> ModelDeclaration:
    """Exercise ordinary selected output declarations rather than an RDFa facade."""
    one = OutputRequirement(minimum_values=1, maximum_values=1)
    return ModelDeclaration(
        "record",
        (EX + "Thing",),
        (
            FieldDeclaration(
                "label",
                EX + "label",
                (ValueDeclaration("literal", language="en"),),
                output=one,
            ),
            FieldDeclaration(
                "nested",
                NESTED + "label",
                (ValueDeclaration("literal", language="fr"),),
                output=one,
            ),
            FieldDeclaration(
                "parent",
                EX + "parent",
                (ValueDeclaration("iri"),),
                reverse=True,
                output=one,
            ),
            FieldDeclaration(
                "members",
                EX + "members",
                (ValueDeclaration("literal", language="en", container="@list"),),
                output=OutputRequirement(
                    minimum_values=1,
                    maximum_values=1,
                    minimum_list_members=2,
                    maximum_list_members=2,
                ),
            ),
        ),
        subject=SubjectIdentity(str(SUBJECT), False),
    )


def test_native_html5_record_base_inheritance_reverse_and_list(
    runtime: RdfaRuntime,
) -> None:
    result = extract_rdfa(HTML.encode(), URL, runtime=runtime)
    assert result.report.verified, result.report.to_dict()
    assert result.source is not None and result.producer is not None
    assert result.association_precision == "document" and result.document_url == URL
    assert result.producer.parser == "3.0.2" and result.producer.node.startswith("v")
    graph = result.source.graph()
    assert len(graph) == 9
    assert (SUBJECT, RDF.type, URIRef(EX + "Thing")) in graph
    assert (SUBJECT, URIRef(EX + "label"), literal_term("Name", language="en")) in graph
    assert (
        SUBJECT,
        URIRef(NESTED + "label"),
        literal_term("Nom", language="fr"),
    ) in graph
    assert (
        URIRef("https://example.org/base/parent"),
        URIRef(EX + "parent"),
        SUBJECT,
    ) in graph
    head = next(graph.objects(SUBJECT, URIRef(EX + "members")))
    assert [str(value) for value in iter_rdf_list(graph, head, max_items=10)] == [
        "one",
        "two",
    ]
    checked = check_rdf_emission(
        result.source, (_model(),), (RdfModelUsage("record", SUBJECT, location=URL),)
    )
    assert checked.verified, checked.to_dict()
    graph.remove((None, None, None))
    assert len(result.source.graph()) == 9


def test_prefix_changes_are_actionable_in_ordinary_record_checks(
    runtime: RdfaRuntime,
) -> None:
    changed = HTML.replace(
        'property="ex:label">Name',
        'prefix="ex: https://example.org/changed#" property="ex:label">Name',
    )
    result = extract_rdfa(changed, URL, runtime=runtime)
    assert result.source is not None and result.report.verified
    checked = check_rdf_emission(
        result.source, (_model(),), (RdfModelUsage("record", SUBJECT, location=URL),)
    )
    assert not checked.verified
    assert any(
        issue.code == "emission.value_count_mismatch"
        and issue.location == URL + ".label"
        for issue in checked.issues
    )


def test_companion_jsonld_stays_separate_with_explicit_shared_fields(
    runtime: RdfaRuntime,
) -> None:
    pytest.importorskip("pyld")
    rdfa = extract_rdfa(HTML, URL, runtime=runtime)
    jsonld = extract_html_jsonld(HTML, URL, script_id="companion")
    assert rdfa.source is not None and jsonld.source is not None
    assert jsonld.report.verified
    graph = rdfa.source.graph()
    shared = Graph()
    for triple in graph.triples((SUBJECT, None, None)):
        if triple[1] in (RDF.type, URIRef(EX + "label")):
            shared.add(triple)
    assert len(graph) == 9 and len(jsonld.source.graph()) == 2
    assert compare_rdf(prepare_rdf(shared), jsonld.source).verified
    start, end = HTML.index("<script"), HTML.index("</script>") + len("</script>")
    without = extract_rdfa(HTML[:start] + HTML[end:], URL, runtime=runtime)
    assert without.source is not None
    assert compare_rdf(rdfa.source, without.source).verified


def test_unavailable_host_expansion_and_empty_never_publish(
    runtime: RdfaRuntime,
) -> None:
    for content in (
        "<html>No assertions</html>",
        '<span property="[broken-curie]">bad',
    ):
        result = extract_rdfa(content, URL, runtime=runtime)
        assert result.source is None and not result.report.verified
        assert any(
            issue.code == "rdfa.empty_extraction" for issue in result.report.issues
        )
    copied = extract_rdfa(
        f'<html prefix="ex: {EX}" about="item">'
        '<span property="ex:label">Known</span><link property="rdfa:copy" '
        'href="https://unavailable.example/pattern"></html>',
        URL,
        runtime=runtime,
    )
    assert copied.source is None and any(
        issue.code == "rdfa.expansion_unverified" for issue in copied.report.issues
    )
    unsupported = extract_rdfa(HTML, URL, runtime=runtime, host="xhtml5")
    assert (
        unsupported.source is None and unsupported.report.issues[0].kind == "unverified"
    )
    assert unsupported.producer is None


def test_optional_runtime_absence_and_argument_errors(tmp_path: Path) -> None:
    for runtime in (
        None,
        RdfaRuntime("/unavailable/node", tmp_path),
        RdfaRuntime("node", tmp_path),
    ):
        result = extract_rdfa(HTML, URL, runtime=runtime)
        assert result.source is None and any(
            issue.kind == "unverified" for issue in result.report.issues
        )
    with pytest.raises(InputError):
        extract_rdfa(cast(str, object()), URL)
    with pytest.raises(InputError):
        extract_rdfa(HTML, URL, timeout=float("nan"))
    with pytest.raises(InputError):
        extract_rdfa(HTML, URL, budget=Budget(), limits=Limits(max_work=1))


def test_runtime_asset_copy_preserves_existing_consumer_inputs(tmp_path: Path) -> None:
    package, lock = prepare_rdfa_runtime(tmp_path)
    assert b'"rdfa-streaming-parser"' in package.read_bytes()
    assert b'"3.0.2"' in lock.read_bytes()
    assert prepare_rdfa_runtime(tmp_path) == (package, lock)
    package.write_text("consumer work")
    with pytest.raises(InputError):
        prepare_rdfa_runtime(tmp_path)
    assert package.read_text() == "consumer work"


def test_cumulative_caps_and_owned_deadline_never_publish_partial(
    runtime: RdfaRuntime,
) -> None:
    for limits in (
        Limits(max_work=1000),
        Limits(max_states=5),
        Limits(max_triples=1),
        Limits(max_depth=2),
    ):
        budget = Budget(limits)
        result = extract_rdfa(HTML, URL, runtime=runtime, budget=budget)
        assert result.source is None and any(
            issue.kind == "exhausted" for issue in result.report.issues
        )
        assert budget.remaining("work") >= 0 and budget.remaining("bytes") >= 0
    cancelled = threading.Event()
    cancelled.set()
    untouched = Budget()
    result = extract_rdfa(
        HTML, URL, runtime=runtime, cancel=cancelled, budget=untouched
    )
    assert result.source is None and result.report.issues[0].kind == "exhausted"
    assert untouched.remaining("resources") == untouched.limits.max_resources
    failed_budget = Budget()
    failed = extract_rdfa(
        HTML,
        URL,
        runtime=RdfaRuntime("/unavailable/node", runtime.directory),
        budget=failed_budget,
    )
    assert failed.source is None
    assert failed_budget.remaining("resources") < failed_budget.limits.max_resources
    retained = failed_budget.remaining("resources")
    recovered = extract_rdfa(HTML, URL, runtime=runtime, budget=failed_budget)
    assert recovered.report.verified and recovered.source is not None
    assert failed_budget.remaining("resources") < retained
    timed = extract_rdfa(HTML, URL, runtime=runtime, timeout=0.000001)
    assert timed.source is None and timed.report.issues[0].kind == "exhausted"


def test_input_preflight_does_not_copy_large_content_with_spent_budget(
    runtime: RdfaRuntime,
) -> None:
    budget = Budget()
    budget.consume("bytes", budget.remaining("bytes") - len(URL) - 1)
    content = "😀" * 500_000
    tracemalloc.start()
    try:
        result = extract_rdfa(content, URL, runtime=runtime, budget=budget)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.source is None and any(
        issue.kind == "exhausted" for issue in result.report.issues
    )
    assert peak < 100_000
