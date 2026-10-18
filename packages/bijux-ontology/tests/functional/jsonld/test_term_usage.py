"""Explicit usage policies distinguish legal implicit mappings at actual scopes."""

import importlib.util
from typing import Any

import pytest
from rdflib import Graph, Namespace
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.errors import InputError, ResourceError
from bijux_ontology.jsonld.checking import PreparedUsages
from bijux_ontology.jsonld.selectors.types import ArrayItems, Property, SourceSelector
from bijux_ontology.jsonld.term_usage import TermUsageRegion, check_term_usages
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.vocabulary.inspection import inspect_vocabulary

requires_processor = pytest.mark.skipif(
    importlib.util.find_spec("pyld") is None, reason="maintained context processing"
)
E = Namespace("urn:terms:")
IMPLICIT_KEY = "lab" + "le"


def region(path: tuple[str | int, ...] = (), **options: Any) -> TermUsageRegion:
    """Select one actual object without requiring an RDF property contribution."""
    return TermUsageRegion(SourceSelector((), start=path), **options)


@requires_processor
def test_closed_region_reports_actual_implicit_key_and_allows_selected_extensions() -> (
    None
):
    """An implicit mapping remains legal JSON-LD while violating an opt-in rule."""
    prepared = PreparedUsages(
        {
            "@context": {
                "@vocab": str(E),
                "label": str(RDFS.label),
                "extension": "urn:extension",
            },
            "@id": "urn:r",
            "label": "right",
            IMPLICIT_KEY: "typo",
            "extension": {"custom": "allowed"},
        }
    )
    result = check_term_usages(
        prepared, (region(), region(("extension",), implicit="allow"))
    )
    issue = next(
        i for i in result.report.issues if i.code == "context.term_usage_disallowed"
    )
    assert issue.location == "/" + IMPLICIT_KEY and issue.actual == str(E[IMPLICIT_KEY])
    assert next(u for u in result.usages if u.path == ("label",)).role == "explicit"
    custom = next(u for u in result.usages if u.path == ("extension", "custom"))
    assert custom.accepted and custom.role == "implicit-vocabulary"
    assert prepared.observe((IMPLICIT_KEY,)).occurrences[0].predicate == str(
        E[IMPLICIT_KEY]
    )
    extension_only = check_term_usages(
        prepared, (region(("extension",), implicit="allow"),)
    )
    assert extension_only.report.verified


@requires_processor
def test_actual_scopes_aliases_nullification_and_opaque_json_keep_their_roles() -> None:
    """Equal spellings in different active contexts cannot borrow definitions."""
    prepared = PreparedUsages(
        {
            "@context": {
                "@vocab": str(E),
                "id": "@id",
                "p": {
                    "@id": "urn:p",
                    "@context": {"label": str(RDFS.label), "gone": None},
                },
                "q": "urn:q",
                "blob": {"@id": "urn:blob", "@type": "@json"},
            },
            "id": "urn:r",
            "p": {"label": "defined", "gone": "ignored"},
            "q": {"label": "implicit"},
            "blob": {IMPLICIT_KEY: 1},
        }
    )
    result = check_term_usages(
        prepared,
        (
            region(),
            region(("p",), ignored_terms=("gone",)),
            region(("q",)),
            region(("blob",)),
        ),
    )
    by_path = {u.path: u for u in result.usages}
    assert by_path[("p", "label")].role == "explicit"
    assert (
        by_path[("q", "label")].role == "implicit-vocabulary"
        and not by_path[("q", "label")].accepted
    )
    assert (
        by_path[("p", "gone")].role == "nullified" and by_path[("p", "gone")].accepted
    )
    assert by_path[("id",)].role == "keyword"
    assert by_path[("blob",)].role == "opaque"
    assert not any(u.path == ("blob", IMPLICIT_KEY) for u in result.usages)
    assert "/p/gone" in result.report.exclusions and "/blob" in result.report.exclusions
    typed = PreparedUsages(
        {
            "@context": {
                "@vocab": str(E),
                "T": {"@id": "urn:T", "@context": {"label": str(RDFS.label)}},
            },
            "@type": "T",
            "label": "defined",
            "child": {"label": "implicit"},
        }
    )
    types = check_term_usages(typed, (region(), region(("child",))))
    assert next(u for u in types.usages if u.path == ("label",)).role == "explicit"
    assert (
        next(u for u in types.usages if u.path == ("child", "label")).role
        == "implicit-vocabulary"
    )


@requires_processor
def test_iri_permissions_and_owned_vocabulary_membership_are_independent() -> None:
    """Finite owned vocabulary scope qualifies actual expanded IRIs."""
    vocabulary = inspect_vocabulary(
        Graph().add((E.label, RDF.type, OWL.AnnotationProperty))
    )
    prepared = PreparedUsages(
        {
            "@context": {"ex": {"@id": str(E), "@prefix": True}},
            "@id": "urn:r",
            "ex:label": "compact",
            str(E.other): "full",
        }
    )
    result = check_term_usages(
        prepared,
        (region(allow_compact_iris=False, membership="declared"),),
        vocabulary=vocabulary,
    )
    compact = next(u for u in result.usages if u.term == "ex:label")
    full = next(u for u in result.usages if u.term == str(E.other))
    assert compact.role == "compact-iri" and not compact.accepted
    assert full.role == "full-iri" and not full.accepted
    assert any(i.code == "context.term_usage_membership" for i in result.report.issues)
    valid = check_term_usages(
        PreparedUsages(
            {"@context": {"ex": {"@id": str(E), "@prefix": True}}, "ex:label": "yes"}
        ),
        (region(membership="declared"),),
        vocabulary=vocabulary,
    )
    assert valid.report.verified
    for key in ("_:edge", ":edge"):
        unresolved = check_term_usages(
            PreparedUsages({"@id": "urn:r", key: "value"}), (region(),)
        )
        assert unresolved.report.outcome == "unverified"
        usage = next(u for u in unresolved.usages if u.term == key)
        assert usage.role == "unresolved" and not usage.accepted

    class InvalidLookup:
        """An incompatible status cannot certify owned membership."""

        def lookup(self, iri: str) -> Any:
            return object()

    with pytest.raises(InputError):
        check_term_usages(
            prepared, (region(membership="present"),), vocabulary=InvalidLookup()
        )


@requires_processor
def test_required_object_coverage_does_not_borrow_other_records() -> None:
    """Existing per-parent selectors retain missing children and optional no-match."""
    prepared = PreparedUsages(
        {
            "@context": {"@vocab": str(E), "label": str(RDFS.label)},
            "records": [{"label": "one"}, {}],
        }
    )
    selected = SourceSelector(
        (Property("records", minimum=1), ArrayItems(), Property("child", minimum=1))
    )
    result = check_term_usages(prepared, (TermUsageRegion(selected),))
    assert not result.report.verified and any(
        i.code == "selector.parent_count_mismatch" for i in result.report.issues
    )
    optional = check_term_usages(
        prepared, (TermUsageRegion(SourceSelector((Property("missing"),), minimum=0)),)
    )
    assert optional.report.outcome == "not_applicable"
    empty = check_term_usages(prepared, ())
    assert not empty.report.verified
    with pytest.raises(InputError):
        check_term_usages(prepared, (region(), region(implicit="allow")))
    with pytest.raises(InputError):
        check_term_usages(prepared, (region(),), budget=Budget())


@requires_processor
def test_owned_context_resources_and_late_limits_preserve_known_policy_findings() -> (
    None
):
    """A shared operation never turns an exhausted required region into a pass."""
    budget = Budget(Limits(max_issues=1))
    prepared = PreparedUsages(
        {"@context": {"@vocab": str(E)}, "first": "one", "second": "two"}, budget=budget
    )
    result = check_term_usages(prepared, (region(),))
    assert result.report.outcome == "exhausted"
    assert any(i.code == "context.term_usage_disallowed" for i in result.report.issues)
    assert result.usages and result.usages[0].path == ("@context",)
    calls = []

    def loader(url: str, options: dict[str, object]) -> dict[str, object]:
        calls.append(url)
        return {
            "documentUrl": url,
            "contextUrl": None,
            "document": {"@context": {"label": str(RDFS.label)}},
        }

    prepared = PreparedUsages(
        {"@context": "urn:context", "label": "value"}, document_loader=loader
    )
    assert check_term_usages(prepared, (region(),)).report.verified
    assert calls == ["urn:context"]
    huge = "x" * 10000
    prepared = PreparedUsages(
        {"@context": {"@vocab": str(E)}, "n": "x"}, limits=Limits(max_bytes=1000)
    )
    bounded = check_term_usages(prepared, (region(ignored_terms=(huge,)),))
    assert bounded.report.outcome == "exhausted" and not bounded.usages
    finite = PreparedUsages({"urn:n": "value"}, budget=Budget(Limits(max_states=100)))
    inventory = tuple("ignored" + str(index) for index in range(200))
    capped = check_term_usages(finite, (region(ignored_terms=inventory),))
    assert capped.report.outcome == "exhausted" and not capped.usages


def test_core_import_keeps_context_processing_explicitly_optional() -> None:
    """Core consumers can import policy records without manufacturing coverage."""
    if importlib.util.find_spec("pyld") is None:
        with pytest.raises(ResourceError):
            PreparedUsages({"urn:n": 1})
    assert region().implicit == "forbid"
