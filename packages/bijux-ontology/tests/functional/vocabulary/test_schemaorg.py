"""Schema.org alternatives remain guidance unless an application selects them."""

import hashlib
import threading
from dataclasses import dataclass, replace
from typing import cast

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, XSD

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.types import RdfSource
from bijux_ontology.rdf.terms import literal_term
from bijux_ontology.vocabulary.schemaorg import (
    SchemaOrgView,
    check_schemaorg,
    prepare_schemaorg,
    schemaorg_property,
)

S = Namespace("https://schema.org/")
E = Namespace("urn:schema-expectations:")
RELEASE = """@prefix s: <https://schema.org/> .
@prefix e: <urn:schema-expectations:> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
s:Thing a rdfs:Class . s:Person a rdfs:Class; rdfs:subClassOf s:Thing .
s:Organization a rdfs:Class; rdfs:subClassOf s:Thing .
s:Event a rdfs:Class; rdfs:subClassOf s:Thing .
s:Place a rdfs:Class; rdfs:subClassOf s:Thing .
s:Student a rdfs:Class; rdfs:subClassOf s:Person .
s:Text a rdfs:Class, s:DataType . s:Number a rdfs:Class, s:DataType .
s:Integer a rdfs:Class; rdfs:subClassOf s:Number .
s:performer a rdf:Property; s:domainIncludes s:Event;
 s:rangeIncludes s:Person, s:Organization .
s:performers a rdf:Property; s:domainIncludes s:Event;
 s:rangeIncludes s:Person, s:Organization; s:supersededBy s:performer .
s:name a rdf:Property; s:domainIncludes s:Thing; s:rangeIncludes s:Text .
s:count a rdf:Property; s:domainIncludes s:Thing; s:rangeIncludes s:Integer .
s:forward a rdf:Property; s:domainIncludes s:Person; s:rangeIncludes s:Organization;
 s:inverseOf s:back . s:back a rdf:Property .
s:sub a rdf:Property; rdfs:subPropertyOf s:performer .
s:pendingProperty a rdf:Property; s:isPartOf <https://pending.schema.org>;
 s:domainIncludes s:Event; s:rangeIncludes s:Person .
"""


def _view(
    *,
    extensions: tuple[URIRef, ...] = (),
    iri_mapping: tuple[tuple[URIRef, URIRef], ...] = (),
) -> SchemaOrgView:
    """Pin explicit local bytes rather than fetching a live global vocabulary."""
    return prepare_schemaorg(
        RdfSource(RELEASE, "turtle", document_iri=str(E.release)),
        release="supplied-test-release",
        namespace=S[""],
        extensions=extensions,
        iri_mapping=iri_mapping,
    )


def _data(
    value: URIRef | Literal,
    kind: URIRef | None = S.Person,
    predicate: URIRef = S.performer,
) -> Graph:
    """Actual emitted values and assertions are independent from the release."""
    graph = Graph().add((E.event, RDF.type, S.Event)).add((E.event, predicate, value))
    if kind is not None and isinstance(value, URIRef):
        graph.add((value, RDF.type, kind))
    return graph


def test_alternatives_named_inheritance_advisory_and_explicit_text_fallback() -> None:
    """Either Person or Organization suffices; guidance is never OWL inconsistency."""
    view = _view()
    assert (
        view.report.verified
        and view.sha256 == hashlib.sha256(RELEASE.encode()).hexdigest()
    )
    for kind in (S.Person, S.Organization, S.Student):
        result = check_schemaorg(
            _data(E.value, kind),
            view,
            subjects=(E.event,),
            predicates=(S.performer,),
            mode="application",
        )
        assert result.report.verified and result.application is not None
        assert result.report.regime == "schemaorg-application-expectations"

    inherited = _data(E.value, S.Student).add((E.value, RDF.type, E.Unknown))
    assert check_schemaorg(
        inherited,
        view,
        subjects=(E.event,),
        predicates=(S.performer,),
        mode="application",
    ).report.verified

    @dataclass
    class Event:
        performer: URIRef
        performer_type: URIRef

    calls = 0

    def serialize(record: Event, *, subject: URIRef) -> Graph:
        """The real application serializer supplies the checked RDF once."""
        nonlocal calls
        calls += 1
        return (
            Graph()
            .add((subject, RDF.type, S.Event))
            .add((subject, S.performer, record.performer))
            .add((record.performer, RDF.type, record.performer_type))
        )

    emitted = serialize(Event(E.value, S.Place), subject=E.event)
    model = ModelDeclaration(
        "event",
        (str(S.Event),),
        (
            FieldDeclaration(
                "performer",
                str(S.performer),
                (ValueDeclaration("iri"),),
                emission="guaranteed",
            ),
        ),
    )
    assert check_rdf_emission(
        prepare_rdf(emitted), (model,), (RdfModelUsage("event", E.event),)
    ).verified
    assert not check_schemaorg(
        emitted,
        view,
        subjects=(E.event,),
        predicates=(S.performer,),
        mode="application",
    ).report.verified
    assert calls == 1
    wrong = _data(E.value, S.Place)
    advisory = check_schemaorg(
        wrong, view, subjects=(E.event,), predicates=(S.performer,)
    )
    assert advisory.application is None and advisory.report.outcome == "disagreement"
    assert advisory.report.regime == "schemaorg-published-guidance"
    assert all(issue.severity == "warning" for issue in advisory.report)
    strict = check_schemaorg(
        wrong, view, subjects=(E.event,), predicates=(S.performer,), mode="application"
    )
    assert not strict.report.verified and strict.report.exit_code() == 1
    text = _data(Literal("a performer"), None)
    refused = check_schemaorg(
        text, view, subjects=(E.event,), predicates=(S.performer,), mode="application"
    )
    assert refused.report.outcome == "disagreement"
    accepted = check_schemaorg(
        text,
        view,
        subjects=(E.event,),
        predicates=(S.performer,),
        mode="application",
        text_fallback="allow",
    )
    assert accepted.report.verified and accepted.uses[0].used_text_fallback
    assert accepted.published.outcome == "disagreement"
    assert (
        check_schemaorg(
            _data(E.value, None),
            view,
            subjects=(E.event,),
            predicates=(S.performer,),
            mode="application",
        ).report.outcome
        == "unverified"
    )


def test_exact_iri_schemes_lifecycle_extensions_and_actual_inverse_witnesses() -> None:
    """No automatic HTTP alias or section widening; lifecycle replacements are exact."""
    view = _view()
    old = check_schemaorg(
        _data(E.value, S.Person, S.performers),
        view,
        subjects=(E.event,),
        predicates=(S.performers,),
        mode="application",
        lifecycle="reject",
    )
    assert old.report.outcome == "disagreement"
    assert any(
        issue.code == "schemaorg.superseded" and issue.expected == str(S.performer)
        for issue in old.report
    )
    assert old.uses[0].expectation.superseded_by == (S.performer,)
    assert check_schemaorg(
        _data(E.value, S.Person, S.performers),
        view,
        subjects=(E.event,),
        predicates=(S.performers,),
        mode="application",
    ).report.verified
    pending = _data(E.value, S.Person, S.pendingProperty)
    assert not check_schemaorg(
        pending,
        view,
        subjects=(E.event,),
        predicates=(S.pendingProperty,),
        mode="application",
    ).report.verified
    selected = _view(extensions=(URIRef("https://pending.schema.org"),))
    assert check_schemaorg(
        pending,
        selected,
        subjects=(E.event,),
        predicates=(S.pendingProperty,),
        mode="application",
    ).report.verified
    inverse = (
        Graph()
        .add((E.organization, RDF.type, S.Organization))
        .add((E.person, RDF.type, S.Person))
        .add((E.organization, S.back, E.person))
    )
    checked = check_schemaorg(
        inverse,
        view,
        subjects=(E.organization,),
        predicates=(S.back,),
        mode="application",
    )
    assert checked.report.verified and any(
        triple[1] == S.inverseOf for triple in checked.uses[0].expectation.assertions
    )
    assert schemaorg_property(view, S.sub).report.outcome == "unverified"
    http = Namespace("http://schema.org/")
    data = _data(E.value, S.Person, http.performer)
    assert not check_schemaorg(
        data,
        view,
        subjects=(E.event,),
        predicates=(http.performer,),
        mode="application",
    ).report.verified
    mapped = _view(iri_mapping=((http.performer, S.performer),))
    assert check_schemaorg(
        data,
        mapped,
        subjects=(E.event,),
        predicates=(http.performer,),
        mode="application",
    ).report.verified
    mapped_hints = prepare_schemaorg(
        RdfSource(
            RELEASE.replace(
                "s:rangeIncludes s:Person, s:Organization .",
                "s:rangeIncludes <http://schema.org/Person> .",
            ),
            "turtle",
            document_iri=str(E.release),
        ),
        release="mapped-hints",
        namespace=S[""],
        iri_mapping=((http.Person, S.Person),),
    )
    assert check_schemaorg(
        _data(Literal("a performer"), None),
        mapped_hints,
        subjects=(E.event,),
        predicates=(S.performer,),
        mode="application",
        text_fallback="allow",
    ).report.verified
    mapped_ancestry = prepare_schemaorg(
        RdfSource(
            RELEASE + "s:MixedStudent a rdfs:Class; "
            "rdfs:subClassOf <http://schema.org/Person> .",
            "turtle",
            document_iri=str(E.release),
        ),
        release="mapped-ancestry",
        namespace=S[""],
        iri_mapping=((http.Person, S.Person),),
    )
    for predicate in (S.performer, S.name):
        value = E.value if predicate == S.performer else Literal("a name")
        graph = _data(value, S.MixedStudent, predicate)
        if predicate == S.name:
            graph.set((E.event, RDF.type, S.MixedStudent))
        assert check_schemaorg(
            graph,
            mapped_ancestry,
            subjects=(E.event,),
            predicates=(predicate,),
            mode="application",
        ).report.verified
    for pairs in (
        ((http.performer, S.performer), (http.performer, S.performers)),
        ((http.performer, S.performer), (S.performer, http.performer)),
    ):
        with pytest.raises(InputError):
            _view(iri_mapping=pairs)


def test_actual_literal_encoding_unsupported_expectations_and_original_lexical() -> (
    None
):
    """The finite RDF bridge validates samples and never retypes original text."""
    view = _view()
    data = (
        Graph()
        .add((E.person, RDF.type, S.Person))
        .add((E.person, S.name, Literal("Person", lang="fr")))
        .add((E.person, S["count"], literal_term("01", datatype=str(XSD.integer))))
    )
    result = check_schemaorg(
        data,
        view,
        subjects=(E.person,),
        predicates=(S.name, S["count"]),
        mode="application",
    )
    assert result.report.verified
    assert any(str(use.statement[2]) == "01" for use in result.uses)
    data.set((E.person, S.name, E.resource))
    assert (
        check_schemaorg(
            data, view, subjects=(E.person,), predicates=(S.name,), mode="application"
        ).report.outcome
        == "disagreement"
    )
    lossy = prepare_rdf(data)
    lossy.literal_fidelity = "normalized"
    assert not check_schemaorg(
        lossy, view, subjects=(E.person,), predicates=(S["count"],), mode="application"
    ).report.verified
    data.set((E.person, S["count"], literal_term("1.0", datatype=str(XSD.decimal))))
    assert (
        check_schemaorg(
            data,
            view,
            subjects=(E.person,),
            predicates=(S["count"],),
            mode="application",
        ).report.outcome
        == "unverified"
    )
    head = BNode()
    graph = _data(E.value)
    graph.remove((E.event, S.performer, None)).add((E.event, S.performer, head)).add(
        (head, RDF.first, E.value)
    ).add((head, RDF.rest, RDF.nil))
    assert (
        check_schemaorg(
            graph,
            view,
            subjects=(E.event,),
            predicates=(S.performer,),
            mode="application",
        ).report.outcome
        == "unverified"
    )


def test_data_scope_owned_blank_bindings_and_input_ownership() -> None:
    """Pinned scopes exclude hidden bad data and keep release ownership."""
    dataset = Dataset()
    for triple in _data(E.value):
        dataset.graph(E.selected).add(triple)
    for triple in _data(E.value, S.Place):
        dataset.graph(E.hidden).add(triple)
    source = prepare_rdf(dataset)
    view = _view()
    with pytest.raises(InputError):
        check_schemaorg(
            source,
            view,
            subjects=(E.event,),
            predicates=(S.performer,),
            mode="application",
        )
    assert check_schemaorg(
        source,
        view,
        subjects=(E.event,),
        predicates=(S.performer,),
        graph_name=E.selected,
        mode="application",
    ).report.verified
    assert not check_schemaorg(
        source,
        view,
        subjects=(E.event,),
        predicates=(S.performer,),
        graph_name=E.hidden,
        mode="application",
    ).report.verified
    blank = BNode()
    native = prepare_rdf(
        Graph()
        .add((blank, RDF.type, S.Event))
        .add((blank, S.performer, E.value))
        .add((E.value, RDF.type, S.Person))
    )
    owned = next(native._read().subjects(RDF.type, S.Event))
    assert check_schemaorg(
        native,
        view,
        subjects=(cast(BNode, owned),),
        predicates=(S.performer,),
        mode="application",
    ).report.verified
    assert not check_schemaorg(
        native,
        view,
        subjects=(BNode(str(owned)),),
        predicates=(S.performer,),
        mode="application",
    ).report.verified
    exposed = view.source.graph() if view.source is not None else Graph()
    exposed.remove((S.performer, None, None))
    assert check_schemaorg(
        _data(E.value),
        view,
        subjects=(E.event,),
        predicates=(S.performer,),
        mode="application",
    ).report.verified


def test_bounds_cancellation_prefix_and_runtime_metadata() -> None:
    """A known mismatch survives later budget exhaustion without oversized anchors."""
    view = _view()
    huge = URIRef("https://schema.org/" + "x" * 10000)
    selected = schemaorg_property(view, huge, budget=Budget(Limits(max_bytes=1000)))
    assert selected.report.outcome == "exhausted" and selected.requested is None
    stopped = threading.Event()
    stopped.set()
    assert (
        check_schemaorg(
            _data(E.value),
            view,
            subjects=(E.event,),
            predicates=(S.performer,),
            cancel=stopped,
        ).report.outcome
        == "exhausted"
    )
    result = check_schemaorg(
        _data(E.value, S.Place),
        view,
        subjects=(E.event,),
        predicates=(S.performer,),
        mode="application",
        limits=Limits(max_issues=1),
    )
    assert result.report.outcome == "exhausted"
    assert any(
        issue.code == "schemaorg.expectation_mismatch" for issue in result.report
    )
    imported = prepare_schemaorg(
        RdfSource(
            RELEASE + f"\n<{E.release}> <{OWL.imports}> <urn:unavailable> .",
            "turtle",
            document_iri=str(E.release),
        ),
        release="imports-unresolved",
        namespace=S[""],
    )
    assert not check_schemaorg(
        _data(E.value),
        imported,
        subjects=(E.event,),
        predicates=(S.performer,),
        mode="application",
    ).report.verified
    for key, bad in (
        ("extensions", "not-a-sequence"),
        ("iri_mapping", ((Literal("https://schema.org/Person"), S.Person),)),
    ):
        with pytest.raises(InputError):
            if key == "extensions":
                _view(extensions=cast(tuple[URIRef, ...], bad))
            else:
                _view(iri_mapping=cast(tuple[tuple[URIRef, URIRef], ...], bad))
    for fields in (
        {"source": prepare_rdf(Graph())},
        {"sha256": "0" * 64},
        {"extensions": (E.other,)},
    ):
        with pytest.raises(InputError):
            replace(view, **fields)
    with pytest.raises(InputError):
        SchemaOrgView()
