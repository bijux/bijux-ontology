"""Actual RDF replacements require owned occurrence and whole-source proof."""

from copy import deepcopy
from dataclasses import replace
from typing import TypedDict, Unpack, cast

import pytest
from rdflib import BNode, Literal, URIRef
from rdflib.namespace import XSD

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.documents.forms.types import FormRequirement
from bijux_ontology.documents.patches import RetainedUsage
from bijux_ontology.documents.patches.rdf import (
    RDFSourcePatch,
    SourceEditBinding,
    project_rdf_patch,
)
from bijux_ontology.jsonld.preparation import prepare_jsonld
from bijux_ontology.jsonld.resources import DocumentLoader, OfflineDocumentLoader
from bijux_ontology.jsonld.selectors.types import SourceSelector
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.patches import BlankNodePair, DatasetPatch, plan_rdf_patch

E = "https://example.org/source-edit/"
BASE = E + "document"


def document() -> dict[str, object]:
    """Keep literal coercion, reverse field and unrelated source spelling explicit."""
    return {
        "@context": {
            "xsd": str(XSD),
            "value": {"@id": E + "value", "@type": "xsd:integer"},
            "alias": {"@id": E + "value", "@type": "xsd:integer"},
            "keep": E + "keep",
            "incoming": {"@reverse": E + "edge"},
        },
        "@id": E + "owner",
        "value": "01",
        "keep": "unchanged",
        "incoming": {"@id": E + "old"},
    }


def program(
    source: object,
    *,
    reverse: bool = False,
    loader: DocumentLoader | None = None,
) -> DatasetPatch:
    """Use native conversion and actual patch planning to express chosen RDF intent."""
    pytest.importorskip("pyld")
    pytest.importorskip("jsonpatch")
    before = prepare_jsonld(source, base=BASE, document_loader=loader)
    assert before.source is not None and before.report.verified
    graph = before.source.graph()
    if reverse:
        graph.remove((URIRef(E + "old"), URIRef(E + "edge"), URIRef(E + "owner")))
        graph.add((URIRef(E + "new"), URIRef(E + "edge"), URIRef(E + "owner")))
    else:
        graph.remove((URIRef(E + "owner"), URIRef(E + "value"), None))
        graph.add(
            (
                URIRef(E + "owner"),
                URIRef(E + "value"),
                Literal("02", datatype=XSD.integer, normalize=False),
            )
        )
    target = prepare_rdf(graph)
    planned = plan_rdf_patch(before.source, target)
    assert planned.patch is not None and planned.report.verified
    return planned.patch


class Options(TypedDict, total=False):
    expected_base: object
    bindings: tuple[SourceEditBinding, ...]
    limits: Limits
    budget: Budget
    document_loader: DocumentLoader


def project(
    source: object, patch: DatasetPatch, **options: Unpack[Options]
) -> RDFSourcePatch:
    """Supply existing T132 form and retained-use contracts to the public bridge."""
    forms = (
        FormRequirement(
            SourceSelector(()),
            shape="object",
            required_keys=("@id", "value", "keep", "incoming"),
        ),
    )
    return project_rdf_patch(
        source,
        patch,
        options.get("bindings", (SourceEditBinding(0, 0),)),
        expected_base=options.get("expected_base", deepcopy(source)),
        before_requirements=forms,
        after_requirements=forms,
        retained=(RetainedUsage(("keep",), ("keep",)),),
        base=BASE,
        document_loader=options.get("document_loader"),
        limits=options.get("limits"),
        budget=options.get("budget"),
    )


def test_typed_lexical_and_reverse_endpoints_preserve_compact_structure() -> None:
    for reverse in (False, True):
        source = document()
        original = deepcopy(source)
        result = project(source, program(source, reverse=reverse))
        assert result.report.verified, result.report.to_dict()
        assert result.patch is not None
        candidate = result.patch.document
        assert isinstance(candidate, dict)
        assert candidate["@context"] == source["@context"]
        assert candidate["keep"] == "unchanged" and source == original
        expected = [
            {
                "op": "replace",
                "path": "/incoming/@id" if reverse else "/value",
                "value": E + "new" if reverse else "02",
            }
        ]
        assert result.operations == expected
        assert result.bindings[0].paths == (("incoming",) if reverse else ("value",),)
        independent = result.operations
        assert isinstance(independent, list)
        independent.clear()
        assert result.operations == expected
    source = document()
    source["value"] = {"@value": "01", "@type": str(XSD.integer)}
    result = project(source, program(source))
    assert result.report.verified, result.report.to_dict()
    assert result.operations == [
        {"op": "replace", "path": "/value/@value", "value": "02"}
    ]


def test_alias_occurrence_policy_and_exact_whole_rdf_postcondition() -> None:
    source = document()
    source["alias"] = "01"
    delta = program(source)
    ambiguous = project(source, delta)
    assert not ambiguous.report.verified and ambiguous.patch is None
    assert ambiguous.operations is None
    assert any(
        i.code == "documents.source_edit_ambiguous" for i in ambiguous.report.issues
    )
    incomplete = project(
        source, delta, bindings=(SourceEditBinding(0, 0, (("value",),)),)
    )
    assert incomplete.patch is None and incomplete.operations is None
    assert incomplete.report.outcome == "disagreement"
    assert any(
        child.scope == "documents.patch" and child.verified
        for child in incomplete.report.children
    )
    complete = project(source, delta, bindings=(SourceEditBinding(0, 0, "all"),))
    assert complete.report.verified, complete.report.to_dict()
    assert complete.patch is not None
    candidate = complete.patch.document
    assert (
        isinstance(candidate, dict) and candidate["value"] == candidate["alias"] == "02"
    )
    explicit = project(
        source, delta, bindings=(SourceEditBinding(0, 0, (("alias",), ("value",))),)
    )
    assert explicit.report.verified


def test_stale_raw_and_semantic_bases_withhold_source_capability() -> None:
    source = document()
    delta = program(source)
    stale = deepcopy(source)
    stale["keep"] = "different"
    raw = project(stale, delta, expected_base=source)
    assert raw.patch is None and raw.report.outcome == "disagreement"
    assert any(i.code == "documents.source_edit_base_failed" for i in raw.report.issues)
    semantic = project(stale, delta)
    assert semantic.patch is None and semantic.report.outcome == "input_error"
    assert any(
        i.code == "rdf.patch_precondition_failed" for i in semantic.report.issues
    )
    stale["value"] = "99"
    missing_old_edge = project(stale, delta)
    assert (
        missing_old_edge.patch is None
        and missing_old_edge.report.outcome == "input_error"
    )


def test_scoped_value_alias_and_describing_references_are_not_guessed() -> None:
    source = document()
    context = cast(dict[str, object], source["@context"])
    context["value"] = {
        "@id": E + "value",
        "@context": {"lexical": "@value", "kind": "@type"},
    }
    source["value"] = {"lexical": "01", "kind": str(XSD.integer)}
    result = project(source, program(source))
    assert not result.report.verified and result.patch is None
    assert result.report.outcome == "unverified"
    source = document()
    source["incoming"] = {"@id": E + "old", "keep": "owned description"}
    result = project(source, program(source, reverse=True))
    assert result.patch is None and result.report.outcome == "unverified"


def test_shared_anonymous_edit_cannot_borrow_rdf_processor_labels() -> None:
    pytest.importorskip("pyld")
    source = document()
    context = cast(dict[str, object], source["@context"])
    context["ref"] = {"@id": E + "ref", "@type": "@id"}
    source["@id"] = "_:shared"
    source["@included"] = [
        {"@id": E + "a", "ref": "_:shared"},
        {"@id": E + "b", "ref": "_:shared"},
    ]
    prepared = prepare_jsonld(source, base=BASE)
    assert prepared.source is not None and prepared.report.verified
    before = prepared.source.graph()
    old_blank = next(s for s, _, _ in before if isinstance(s, BNode))
    before.remove((old_blank, URIRef(E + "value"), None))
    before.add(
        (
            old_blank,
            URIRef(E + "value"),
            Literal("02", datatype=XSD.integer, normalize=False),
        )
    )
    target = prepare_rdf(before)
    new_blank = next(s for s, _, _ in target.graph() if isinstance(s, BNode))
    planned = plan_rdf_patch(
        prepared.source, target, blank_pairs=(BlankNodePair(old_blank, new_blank),)
    )
    assert planned.patch is not None and planned.report.verified
    result = project(source, planned.patch)
    assert not result.report.verified and result.patch is None
    assert result.report.outcome == "unverified"


def test_loader_cannot_mutate_captured_source_delta_or_declarations() -> None:
    source = document()
    context = source["@context"]
    url = E + "context"
    source["@context"] = url
    delta = program(source, loader=OfflineDocumentLoader({url: {"@context": context}}))
    expected = deepcopy(source)
    calls: list[str] = []

    def loader(url: str, options: dict[str, object]) -> dict[str, object]:
        """Mutation after capture cannot retarget the actual original source."""
        calls.append(url)
        source["value"] = "999"
        object.__setattr__(delta, "removals", ())
        return {
            "contextUrl": None,
            "documentUrl": url,
            "document": {"@context": context},
        }

    result = project(source, delta, expected_base=expected, document_loader=loader)
    assert result.report.verified, result.report.to_dict()
    assert calls == [url] and result.patch is not None
    candidate = result.patch.document
    assert isinstance(candidate, dict) and candidate["value"] == "02"
    assert source["value"] == "999"


def test_pair_accounting_and_cumulative_bounds_preserve_paid_evidence() -> None:
    source = document()
    delta = program(source)
    invalid = project(source, delta, bindings=())
    assert invalid.patch is None
    invalid = project(
        source, delta, bindings=(SourceEditBinding(0, 0), SourceEditBinding(0, 0))
    )
    assert invalid.patch is None and invalid.report.outcome == "input_error"
    unrelated = project(
        source, delta, bindings=(SourceEditBinding(0, 0, (("keep",),)),)
    )
    assert unrelated.patch is None
    source["alias"] = "01"
    delta = program(source)
    pilot = Budget()
    rejected = project(source, delta, budget=pilot)
    assert any(
        i.code == "documents.source_edit_ambiguous" for i in rejected.report.issues
    )
    for cap in (100, pilot._counts["work"] // 2, pilot._counts["work"] - 1):
        budget = Budget(replace(Limits(), max_work=cap))
        result = project(source, delta, budget=budget)
        assert result.patch is None and result.operations is None
        assert budget._counts.get("work", 0) <= cap
        owned = {id(i) for i in result.report.issues}
        assert set(budget._retained_issues) <= owned
    with pytest.raises(InputError):
        project(source, delta, limits=Limits(), budget=Budget(Limits(max_work=10)))
    with pytest.raises(InputError):
        RDFSourcePatch()


def test_complete_multiple_pairing_and_escaped_source_field() -> None:
    source = document()
    context = cast(dict[str, object], source["@context"])
    context.pop("value")
    context[E + "value/~"] = {"@id": E + "value/~", "@type": "xsd:integer"}
    source[E + "value/~"] = source.pop("value")
    before = prepare_jsonld(source, base=BASE)
    assert before.source is not None and before.report.verified
    graph = before.source.graph()
    graph.remove((URIRef(E + "owner"), URIRef(E + "value/~"), None))
    graph.add(
        (
            URIRef(E + "owner"),
            URIRef(E + "value/~"),
            Literal("02", datatype=XSD.integer, normalize=False),
        )
    )
    graph.remove((URIRef(E + "old"), URIRef(E + "edge"), URIRef(E + "owner")))
    graph.add((URIRef(E + "new"), URIRef(E + "edge"), URIRef(E + "owner")))
    planned = plan_rdf_patch(before.source, prepare_rdf(graph))
    assert planned.patch is not None and planned.report.verified
    delta = planned.patch
    bindings = tuple(
        SourceEditBinding(
            i,
            next(
                j
                for j, new in enumerate(delta.additions)
                if new.predicate == old.predicate
            ),
        )
        for i, old in enumerate(delta.removals)
    )
    forms = (
        FormRequirement(
            SourceSelector(()),
            shape="object",
            required_keys=("@id", E + "value/~", "keep", "incoming"),
        ),
    )
    result = project_rdf_patch(
        source,
        delta,
        bindings,
        expected_base=deepcopy(source),
        before_requirements=forms,
        after_requirements=forms,
        retained=(RetainedUsage(("keep",), ("keep",)),),
        base=BASE,
    )
    assert result.report.verified, result.report.to_dict()
    operations = result.operations
    assert isinstance(operations, list)
    assert {op["path"] for op in operations} == {
        "/https:~1~1example.org~1source-edit~1value~1~0",
        "/incoming/@id",
    }
