"""Actual maintained HTML extraction, strict selected inputs and offline checks."""

import json

import pytest
from rdflib import Literal, URIRef
from rdflib.namespace import XSD

from bijux_ontology.declarations import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.jsonld.checking import check_context
from bijux_ontology.jsonld.expectations import ContextExpectation
from bijux_ontology.jsonld.html import extract_html_jsonld
from bijux_ontology.jsonld.processing.resources import OfflineDocumentLoader
from bijux_ontology.models.emission.checking import check_emission
from bijux_ontology.models.emission.types import ModelUsage

URL = "https://example.org/pages/document"
P = "https://example.org/name"
CONTEXT = {"name": P}


def _script(identifier: str, name: str, *, media: str = "application/ld+json") -> str:
    """Keep the same actual script payload across selection and semantic controls."""
    document = {"@context": CONTEXT, "@id": identifier, "name": name}
    return f"<script id=\"{identifier}\" type='{media}'>{json.dumps(document)}</script>"


def test_all_scripts_resolve_html_base_and_feed_normal_public_checks() -> None:
    html = '<html><head><base href="../vocabulary/"></head><body>\n'
    html += (
        _script("one", "First")
        + '\n<script>throw new Error("never execute")</script>\n'
    )
    html += _script("two", "Second") + "</body></html>"
    result = extract_html_jsonld(html.encode(), URL)
    assert result.report.verified, result.report.to_dict()
    assert result.source is not None
    assert result.base == "https://example.org/vocabulary/"
    assert [(s.position, s.identifier, s.line) for s in result.scripts] == [
        (1, "one", 2),
        (3, "two", 4),
    ]
    graph = result.source.graph()
    assert set(graph.subjects()) == {
        URIRef(result.base + "one"),
        URIRef(result.base + "two"),
    }
    assert (
        URIRef(result.base + "one"),
        URIRef(P),
        Literal("First", datatype=XSD.string),
    ) in graph
    document = result.document
    assert check_context(
        document, [ContextExpectation((0, "name"), P)], base=result.base
    ).verified
    assert isinstance(document, list)
    model = ModelDeclaration(
        "record",
        (),
        (FieldDeclaration("name", P, (ValueDeclaration("literal", str(XSD.string)),)),),
    )
    assert check_emission(document[0], (model,), (ModelUsage("record"),)).verified
    document.clear()
    assert result.document
    ignored = extract_html_jsonld(html, URL, html_base="ignore", all_scripts=False)
    assert ignored.report.verified and ignored.base == URL
    assert ignored.source is not None
    assert set(ignored.source.graph().subjects()) == {
        URIRef("https://example.org/pages/one")
    }


def test_script_selection_is_exact_and_does_not_interpret_xpath() -> None:
    profiled = 'application/ld+json; profile="urn:profile"'
    html = _script("one", "First") + _script("two", "Second", media=profiled)
    selected = extract_html_jsonld(html, URL, profile="urn:profile")
    assert selected.report.verified and [s.identifier for s in selected.scripts] == [
        "two"
    ]
    fragment = extract_html_jsonld(html, URL + "#two")
    assert fragment.report.verified and len(fragment.scripts) == 1
    for profile, script_id in (
        ("urn:absent", None),
        (None, "absent"),
        (None, '"] | //script | //*[@id="'),
    ):
        absent = extract_html_jsonld(html, URL, profile=profile, script_id=script_id)
        assert absent.source is None and not absent.report.verified
    duplicate = extract_html_jsonld(html + _script("two", "Other"), URL + "#two")
    assert duplicate.source is None and duplicate.report.outcome == "input_error"


def test_selected_bad_json_and_missing_context_never_become_empty_success() -> None:
    valid = _script("one", "First")
    for invalid in (
        '{"name":"first","name":"last"}',
        '{"nested":{"x":1,"x":2}}',
        "{broken",
        "null",
    ):
        html = valid + f'<script type="application/ld+json" id="bad">{invalid}</script>'
        failed = extract_html_jsonld(html, URL)
        assert failed.source is None and failed.report.outcome == "input_error"
        first = extract_html_jsonld(html, URL, all_scripts=False)
        assert first.report.verified and len(first.scripts) == 1
    absent = extract_html_jsonld("<html><p>No semantic scripts</p></html>", URL)
    assert absent.source is None and not absent.report.verified
    remote = (
        '<script type="application/ld+json">'
        '{"@context":"urn:context","name":"A"}</script>'
    )
    missing = extract_html_jsonld(remote, URL)
    assert missing.source is None and not missing.report.verified
    loader = OfflineDocumentLoader({"urn:context": {"@context": CONTEXT}})
    assert extract_html_jsonld(remote, URL, document_loader=loader).report.verified
    for html in ("", "<html>\ud800</html>"):
        assert not extract_html_jsonld(html, URL).report.verified


def test_html_and_conversion_share_bounds_and_preserve_paid_child_findings() -> None:
    html = _script("one", "First")
    for limits in (
        Limits(max_bytes=10),
        Limits(max_states=2),
        Limits(max_work=30),
        Limits(max_depth=1),
    ):
        result = extract_html_jsonld(html, URL, limits=limits)
        assert result.report.outcome == "exhausted" and result.source is None
    nested = "<div>" * 300 + html + "</div>" * 300
    truncated = extract_html_jsonld(nested, URL, limits=Limits(max_depth=1000))
    assert truncated.source is None and truncated.report.outcome == "exhausted"
    budget = Budget()
    result = extract_html_jsonld(html, URL, budget=budget)
    assert result.report.verified
    spent = budget.limits.max_work - budget.remaining("work")
    bounded = extract_html_jsonld(html, URL, limits=Limits(max_work=spent - 1))
    assert bounded.report.outcome == "exhausted"
    dropped = (
        '<script type="application/ld+json">'
        '{"@id":"urn:record","unknown":"lost"}</script>'
    )
    for cap in (300, 600, 1200, 2400, 4800):
        operation = Budget(Limits(max_work=cap))
        checked = extract_html_jsonld(dropped, URL, budget=operation)
        paid = {id(issue) for issue in operation._retained_issues.values()}
        reported = {id(issue) for issue in checked.report.issues}
        for child in checked.report.children:
            reported.update(id(issue) for issue in child.issues)
        assert paid <= reported
        assert not checked.report.verified
    with pytest.raises(InputError):
        extract_html_jsonld(html, URL, limits=Limits(), budget=Budget())
