"""Native inferred contracts, asserted provenance and bounded query coverage."""

import os
from pathlib import Path
from threading import Event

import pytest
from bijux_ontology_reasoning.axioms.types import (
    DataProperty,
    NamedEntity,
    ObjectProperty,
)
from bijux_ontology_reasoning.backend import OwlProfileBackend, prepare_profile_backend
from bijux_ontology_reasoning.backend.types import LoaderQualification, ReasonerIdentity
from bijux_ontology_reasoning.documents import OwlDocument, prepare_owl_document
from bijux_ontology_reasoning.inference.consistency.types import (
    ReasonerIdentity as PublicReasoner,
)
from bijux_ontology_reasoning.inference.relationships import (
    InferredAnswer,
    InferredQuery,
    query_inferred_contracts,
)
from bijux_ontology_reasoning.profiles.types import LoaderQualification as PublicLoader
from rdflib import Dataset, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.sources import RdfSource

_SOURCE = """Ontology(<urn:relationships>
 Declaration(Class(<urn:A>)) Declaration(Class(<urn:B>))
 Declaration(Class(<urn:B2>)) Declaration(Class(<urn:C>))
 Declaration(Class(<urn:D>)) Declaration(Class(<urn:E>))
 Declaration(Class(<urn:Z>)) Declaration(ObjectProperty(<urn:p>))
 Declaration(ObjectProperty(<urn:r>)) Declaration(ObjectProperty(<urn:u>))
 Declaration(ObjectProperty(<urn:s>)) Declaration(ObjectProperty(<urn:t>))
 Declaration(DataProperty(<urn:d>)) Declaration(DataProperty(<urn:e>))
 Declaration(DataProperty(<urn:f>))
 EquivalentClasses(<urn:A> ObjectIntersectionOf(<urn:B>
  ObjectSomeValuesFrom(<urn:p> <urn:C>)))
 EquivalentClasses(<urn:B> <urn:B2>)
 SubClassOf(<urn:B> <urn:D>) SubClassOf(<urn:D> <urn:E>)
 EquivalentClasses(<urn:Z> ObjectIntersectionOf(<urn:B>
  ObjectComplementOf(<urn:B>)))
 SubObjectPropertyOf(<urn:p> <urn:r>) EquivalentObjectProperties(<urn:r> <urn:u>)
 SubObjectPropertyOf(<urn:r> <urn:s>) InverseObjectProperties(<urn:s> <urn:t>)
 ObjectPropertyDomain(<urn:s> <urn:B>) ObjectPropertyRange(<urn:s> <urn:C>)
 SubDataPropertyOf(<urn:d> <urn:e>) SubDataPropertyOf(<urn:e> <urn:f>)
 DataPropertyDomain(<urn:f> <urn:B>)
)"""


@pytest.fixture
def backend(tmp_path: Path) -> OwlProfileBackend:
    """Require the explicit pinned local worker and accepted Java runtime."""
    java, directory, pin = (
        os.environ.get(name)
        for name in ("BIJUX_OWL_JAVA", "BIJUX_OWL_BUNDLE", "BIJUX_OWL_MANIFEST_SHA256")
    )
    if not (java and directory and pin):
        pytest.skip("An explicit pinned OWL backend is not configured")
    return prepare_profile_backend(
        Path(java), Path(directory), manifest_sha256=pin, artifacts=tmp_path / "jobs"
    )


def source(content: str = _SOURCE) -> OwlDocument:
    """Capture the original native syntax without materializing any inference."""
    return prepare_owl_document(content, format="functional", document_iri="urn:source")


def members(answer: InferredAnswer) -> set[NamedEntity | ObjectProperty | DataProperty]:
    """Inspect semantic node membership without imposing native set iteration order."""
    return {member.value for group in answer.groups for member in group.members}


def test_complex_class_taxonomy_depth_equivalence_and_unsatisfiability(
    backend: OwlProfileBackend,
) -> None:
    """Inferred classes retain equivalent nodes and explicit bottom truth."""
    original = source()
    a, b, b2, d, e, z = (
        NamedEntity("class", f"urn:{name}") for name in ("A", "B", "B2", "D", "E", "Z")
    )
    result = query_inferred_contracts(
        backend,
        original,
        (
            InferredQuery(a, "super", True),
            InferredQuery(a, "super", False),
            InferredQuery(b, "equivalent"),
            InferredQuery(z, "equivalent"),
            InferredQuery(d, "sub", True),
        ),
    )
    assert result.report.verified and result.consistent is True
    direct, transitive, equivalent, bottom, subclasses = result.answers
    assert members(direct) == {b, b2}
    assert {b, b2, d, e} <= members(transitive)
    assert len(direct.groups) == 1 and direct.groups[0].complete
    assert members(equivalent) == {b, b2}
    assert bottom.satisfiable is False and any(group.bottom for group in bottom.groups)
    assert z in members(bottom)
    assert b in members(subclasses) and a not in members(subclasses)
    assert original.content == _SOURCE.encode()
    assert b"SubClassOf(<urn:A> <urn:B>)" not in original.content
    assert not next(
        member
        for group in direct.groups
        for member in group.members
        if member.value == b
    ).asserted_in


def test_object_and_data_relationships_preserve_inverse_and_asserted_owners(
    backend: OwlProfileBackend,
) -> None:
    """Inferred inverses and inherited domains remain distinct from explicit axioms."""
    original = source()
    p, r, s = (ObjectProperty(f"urn:{name}") for name in ("p", "r", "s"))
    result = query_inferred_contracts(
        backend,
        original,
        (
            InferredQuery(p, "super", True),
            InferredQuery(p, "super", False),
            InferredQuery(p, "inverse"),
            InferredQuery(p, "domain", True),
            InferredQuery(p, "range", True),
            InferredQuery(DataProperty("urn:d"), "super", False),
            InferredQuery(DataProperty("urn:d"), "domain", True),
            InferredQuery(s, "inverse"),
            InferredQuery(r, "equivalent"),
            InferredQuery(s, "sub", False),
            InferredQuery(DataProperty("urn:e"), "sub", False),
        ),
    )
    assert result.report.verified
    assert members(result.answers[0]) == {r, ObjectProperty("urn:u")}
    assert ObjectProperty("urn:t", True) in members(result.answers[1])
    assert members(result.answers[2]) == {ObjectProperty("urn:p", True)}
    assert members(result.answers[3]) == {NamedEntity("class", "urn:A")}
    assert members(result.answers[4]) == {NamedEntity("class", "urn:C")}
    assert {DataProperty("urn:e"), DataProperty("urn:f")} <= members(result.answers[5])
    direct = {
        member.value: member
        for group in result.answers[0].groups
        for member in group.members
    }
    assert direct[r].asserted_in and not direct[ObjectProperty("urn:u")].asserted_in
    assert direct[r].asserted_in[0] is result.documents[0]
    assert members(result.answers[7]) == {
        ObjectProperty("urn:t"),
        ObjectProperty("urn:s", True),
    }
    assert members(result.answers[8]) == {r, ObjectProperty("urn:u")}
    assert {p, r, ObjectProperty("urn:u")} <= members(result.answers[9])
    assert DataProperty("urn:d") in members(result.answers[10])


def test_import_assertions_and_selected_physical_rdf_scope(
    backend: OwlProfileBackend,
) -> None:
    """Imported owners and selected graphs retain their actual scope."""
    child = prepare_owl_document(
        "Ontology(<urn:child> Declaration(Class(<urn:A>)) "
        "Declaration(Class(<urn:B>)) SubClassOf(<urn:A> <urn:B>))",
        format="functional",
        document_iri="urn:child",
    )
    root = source(
        "Ontology(<urn:root> Import(<urn:child>) Declaration(Class(<urn:A>)))"
    )
    query = InferredQuery(NamedEntity("class", "urn:A"), "super", True)
    imported = query_inferred_contracts(
        backend, root, (query,), imports={"urn:child": child}
    )
    assert imported.report.verified
    member = next(
        member
        for group in imported.answers[0].groups
        for member in group.members
        if member.value == NamedEntity("class", "urn:B")
    )
    assert member.asserted_in == (child,)
    dataset = Dataset()
    graph = dataset.graph(URIRef("urn:selected"))
    for iri in ("urn:A", "urn:B", "urn:C"):
        graph.add((URIRef(iri), RDF.type, OWL.Class))
    graph.add((URIRef("urn:o"), RDF.type, OWL.Ontology))
    graph.add((URIRef("urn:A"), RDFS.subClassOf, URIRef("urn:B")))
    graph.add((URIRef("urn:B"), RDFS.subClassOf, URIRef("urn:C")))
    dataset.graph(URIRef("urn:hidden")).add(
        (URIRef("urn:hiddenClass"), RDF.type, OWL.Class)
    )
    before = set(graph)
    prepared = prepare_rdf(RdfSource(dataset, graph_name="urn:selected"))
    answer = query_inferred_contracts(
        backend,
        prepared,
        (
            InferredQuery(NamedEntity("class", "urn:A"), "super", False),
            InferredQuery(NamedEntity("class", "urn:hiddenClass"), "super", False),
        ),
    )
    assert answer.answers[0].report.verified and not answer.answers[1].report.complete
    assert NamedEntity("class", "urn:C") in members(answer.answers[0])
    assert (
        set(graph) == before
        and len(answer.loaders) == 1
        and answer.loaders[0].qualified
    )
    assert not query_inferred_contracts(
        backend, prepare_rdf(RdfSource(dataset, union=True)), (query,)
    ).report.verified


def test_caps_unknown_queries_and_consistency_cannot_certify_empty_views(
    backend: OwlProfileBackend,
) -> None:
    """A partial node and unsupported requests preserve their exact unknown boundary."""
    a = NamedEntity("class", "urn:A")
    empty = query_inferred_contracts(
        backend,
        source(),
        (
            InferredQuery(
                NamedEntity("class", "http://www.w3.org/2002/07/owl#Thing"),
                "super",
                False,
            ),
        ),
    )
    assert empty.report.verified and not empty.answers[0].groups
    capped = query_inferred_contracts(
        backend,
        source(),
        (InferredQuery(a, "super", False), InferredQuery(a, "equivalent")),
        max_results=1,
    )
    assert capped.report.outcome == "exhausted" and not capped.report.complete
    assert capped.answers[0].groups and not capped.answers[0].report.complete
    assert any(not group.complete for group in capped.answers[0].groups)
    assert not capped.answers[1].report.complete
    mixed = query_inferred_contracts(
        backend,
        source(),
        (
            InferredQuery(DataProperty("urn:d"), "range", True),
            InferredQuery(a, "super", True),
        ),
    )
    assert not mixed.answers[0].report.complete and mixed.answers[1].report.verified
    paid = query_inferred_contracts(
        backend,
        source(),
        (
            InferredQuery(DataProperty("urn:d"), "range", True),
            InferredQuery(DataProperty("urn:d"), "inverse"),
        ),
        limits=Limits(max_issues=1),
    )
    assert paid.report.outcome == "exhausted"
    assert any(
        issue.code == "owl.relationship_query_unverified"
        for issue in paid.report.issues
    )

    inconsistent = source(
        _SOURCE[:-1]
        + " Declaration(NamedIndividual(<urn:i>)) ClassAssertion(<urn:Z> <urn:i>))"
    )
    refused = query_inferred_contracts(
        backend, inconsistent, (InferredQuery(a, "super", False),)
    )
    assert refused.consistent is False and not refused.report.verified
    assert not refused.answers[0].groups and not refused.answers[0].report.complete
    missing = query_inferred_contracts(
        backend,
        source("Ontology(<urn:x> Import(<https://example.invalid/unowned>))"),
        (InferredQuery(a, "super", False),),
    )
    assert missing.consistent is None and not missing.report.complete


def test_cumulative_cancellation_and_same_public_receipt_classes(
    backend: OwlProfileBackend,
) -> None:
    """Cumulative limits and cancellation retain no invented successful query."""
    assert PublicReasoner is ReasonerIdentity and PublicLoader is LoaderQualification
    query = InferredQuery(NamedEntity("class", "urn:A"), "super", False)
    event = Event()
    event.set()
    stopped = query_inferred_contracts(backend, source(), (query,), cancel=event)
    assert stopped.report.outcome == "exhausted"
    for limits in (
        Limits(max_work=1),
        Limits(max_total_bytes=128),
        Limits(max_states=1),
    ):
        limited = query_inferred_contracts(
            backend, source(), (query,), budget=Budget(limits)
        )
        assert limited.report.outcome == "exhausted" and not limited.report.verified

    class QuerySubclass(InferredQuery):
        """Custom request behavior is outside the immutable native request profile."""

    subclass = query_inferred_contracts(
        backend, source(), (QuerySubclass(query.focus, "super", False),)
    )
    assert subclass.report.outcome == "input_error" and not subclass.answers
    giant = InferredQuery(
        NamedEntity("datatype", "urn:" + "x" * 100_000), "super", False
    )
    bounded = query_inferred_contracts(
        backend, source(), (giant,), limits=Limits(max_bytes=4096, max_total_bytes=4096)
    )
    assert bounded.report.outcome == "exhausted" and not bounded.answers
    assert bounded.report.requested == 2
    recovered = query_inferred_contracts(backend, source(), (query,))
    assert recovered.report.verified
