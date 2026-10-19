"""Selected instance records preserve actual paths, lists and graph boundaries."""

import tracemalloc
from threading import Event

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, SH, XSD
from rdflib.paths import MulPath

from bijux_ontology.data.extraction import (
    RecordField,
    extract_records,
    record_plan_from_shape,
)
from bijux_ontology.data.projection import ReadField
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.snapshots import PreparedSource

EX = Namespace("urn:record-extraction:")


def _source() -> PreparedSource:
    """Share one named instrument and ordered list between two sample roots."""
    return prepare_rdf(
        f"""@prefix e: <{EX}> . @prefix rdfs: <{RDFS}> .
        @prefix xsd: <{XSD}> .
        e:A e:instrument e:instrument; e:measurements _:head; e:extra "ignored" .
        e:B e:instrument e:instrument; e:measurements _:head .
        e:instrument rdfs:label "shared"; e:unselected "ignored" .
        _:head <{RDF.first}> "01"^^xsd:integer; <{RDF.rest}> _:tail .
        _:tail <{RDF.first}> "2"^^xsd:integer; <{RDF.rest}> <{RDF.nil}>;
            e:extra "ignored" .""",
        format="turtle",
    )


def _fields() -> tuple[RecordField, ...]:
    """Describe only selected instrument labels and exact list topology."""
    return (
        RecordField(
            ReadField("instrument", EX.instrument),
            (RecordField(ReadField("label", RDFS.label)),),
        ),
        RecordField(ReadField("measurements", EX.measurements, mode="list")),
    )


def test_named_description_shared_list_and_lexical_terms_are_owned() -> None:
    source = _source()
    before = frozenset(source.graph())
    result = extract_records(source, (EX.A, EX.B), _fields())
    assert result.source is not None and result.report.verified, result.report.to_dict()
    graph = result.source.graph()
    assert (
        graph.value(EX.A, EX.instrument)
        == graph.value(EX.B, EX.instrument)
        == EX.instrument
    )
    assert graph.value(EX.instrument, RDFS.label) == source.graph().value(
        EX.instrument, RDFS.label
    )
    head = graph.value(EX.A, EX.measurements)
    assert isinstance(head, BNode) and graph.value(EX.B, EX.measurements) is head
    assert str(graph.value(head, RDF.first)) == "01"
    tail = graph.value(head, RDF.rest)
    assert (
        graph.value(tail, RDF.first) == Literal(2)
        and graph.value(tail, RDF.rest) == RDF.nil
    )
    assert not tuple(graph.triples((None, EX.extra, None)))
    assert not tuple(graph.triples((None, EX.unselected, None)))
    assert frozenset(source.graph()) == before
    assert all(
        item.source is source and item.description == "included"
        for item in result.references
    )
    graph.remove((EX.A, EX.instrument, None))
    assert result.source.graph().value(EX.A, EX.instrument) == EX.instrument


def test_reference_only_and_required_description_never_cross_an_omitted_graph() -> None:
    source = prepare_rdf(
        f"""@prefix e: <{EX}> . @prefix rdfs: <{RDFS}> .
        e:selected {{ e:A e:instrument e:instrument . }}
        e:omitted {{ e:instrument rdfs:label "hidden" . }}""",
        format="trig",
    )
    reference = extract_records(
        source,
        (EX.A,),
        (RecordField(ReadField("instrument", EX.instrument)),),
        graph_name=EX.selected,
    )
    assert reference.source is not None and reference.report.verified
    assert reference.source.graph_name == str(EX.selected)
    assert reference.references[0].description == "reference_only"
    assert not tuple(reference.source.graph().triples((EX.instrument, None, None)))
    assert EX.omitted not in tuple(reference.source._graph_names())
    missing = extract_records(source, (EX.A,), _fields()[:1], graph_name=EX.selected)
    assert missing.source is None and any(
        issue.code == "projection.cardinality" for issue in missing.report
    )
    assert not extract_records(source, (EX.A,), _fields()[:1]).report.verified


def test_finite_path_witnesses_prune_dead_branches_and_retain_inverse_direction() -> (
    None
):
    graph = Graph()
    for triple in (
        (EX.A, EX.instrument, EX.good),
        (EX.A, EX.instrument, EX.dead),
        (EX.good, RDFS.label, Literal("good")),
        (EX.dead, EX.extra, Literal("irrelevant")),
        (EX.child, EX.parent, EX.A),
        (EX.A, EX.next, EX.B),
        (EX.B, EX.next, EX.A),
    ):
        graph.add(triple)
    result = extract_records(
        graph,
        (EX.A,),
        (
            RecordField(ReadField("label", EX.instrument / RDFS.label, mode="many")),
            RecordField(ReadField("child", ~EX.parent)),
            RecordField(ReadField("roundtrip", EX.next / EX.next)),
        ),
    )
    assert result.source is not None and result.report.verified, result.report.to_dict()
    actual = result.source.graph()
    assert (EX.A, EX.instrument, EX.good) in actual
    assert (EX.A, EX.instrument, EX.dead) not in actual
    assert (EX.child, EX.parent, EX.A) in actual
    assert (EX.A, EX.next, EX.B) in actual and (EX.B, EX.next, EX.A) in actual
    repeated = extract_records(
        graph,
        (EX.A,),
        (RecordField(ReadField("all", MulPath(EX.next, "+"), mode="many")),),
    )
    assert repeated.source is None and repeated.report.outcome == "unverified"


def test_anonymous_graph_and_subject_selectors_keep_actual_ownership() -> None:
    source = prepare_rdf(
        f'@prefix e: <{EX}> . _:graph {{ _:root e:p "value" . }}', format="trig"
    )
    name = next(source._graph_names())
    subject = next(source._read(name).subjects())
    assert isinstance(name, BNode) and isinstance(subject, BNode)
    fields = (RecordField(ReadField("value", EX.p)),)
    selected = extract_records(source, (subject,), fields, graph_name=name)
    assert selected.source is not None and selected.report.verified
    assert isinstance(selected.source.graph_name, BNode)
    assert selected.source.graph_name is next(selected.source._graph_names())
    assert next(selected.source.graph().subjects()) is not subject
    assert (
        extract_records(source, (BNode(str(subject)),), fields, graph_name=name).source
        is None
    )
    assert (
        extract_records(source, (subject,), fields, graph_name=BNode(str(name))).source
        is None
    )


def test_shape_path_plan_supports_nested_fields_and_rejects_recursive_obligations() -> (
    None
):
    shapes = prepare_rdf(
        f"""@prefix e: <{EX}> . @prefix sh: <{SH}> .
      e:Root a sh:NodeShape; sh:property [ sh:path e:instrument;
        sh:minCount 1; sh:node e:Instrument ] .
      e:Instrument sh:property [ sh:path <{RDFS.label}>; sh:minCount 1 ] .""",
        format="turtle",
    )
    plan = record_plan_from_shape(shapes, EX.Root)
    assert plan.report.verified and plan.fields, plan.report.to_dict()
    assert extract_records(_source(), (EX.A, EX.B), plan.fields).report.verified
    graph = shapes.graph()
    property_shape = graph.value(EX.Instrument, SH.property)
    assert isinstance(property_shape, BNode)
    graph.add((property_shape, SH.node, EX.Root))
    recursive_source = prepare_rdf(graph)
    recursive = record_plan_from_shape(recursive_source, EX.Root)
    assert not recursive.fields and recursive.report.outcome == "unverified"
    graph.remove((property_shape, SH.node, EX.Root))
    graph.add((property_shape, SH.sparql, URIRef(EX.constraint)))
    assert not record_plan_from_shape(prepare_rdf(graph), EX.Root).report.verified


def test_bounds_withhold_output_retain_completed_findings_and_recover() -> None:
    source = _source()
    oversized = (RecordField(ReadField(" " + "x" * 500_000 + " ", EX.instrument)),)
    tracemalloc.start()
    try:
        bounded = extract_records(
            source,
            (EX.A,),
            oversized,
            limits=Limits(max_total_bytes=2048, max_work=10000),
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert bounded.source is None and bounded.report.outcome == "exhausted"
    assert peak < 64_000
    for limits in (Limits(max_work=1), Limits(max_states=1), Limits(max_total_bytes=1)):
        result = extract_records(source, (EX.A,), _fields(), limits=limits)
        assert result.source is None and result.report.outcome == "exhausted"
    cancel = Event()
    cancel.set()
    assert (
        extract_records(source, (EX.A,), _fields(), cancel=cancel).report.outcome
        == "exhausted"
    )
    assert extract_records(source, (EX.A,), _fields()).report.verified
    assert not extract_records(source, (), _fields()).report.verified
    missing = (RecordField(ReadField("missing", EX.missing)),)
    pilot = Budget()
    assert not extract_records(source, (EX.A,), missing, budget=pilot).report.verified
    used = pilot.limits.max_work - pilot.remaining("work")
    cut = Budget(Limits(max_work=used - 1))
    result = extract_records(source, (EX.A,), missing, budget=cut)
    assert result.source is None
    assert all(issue in result.report.issues for issue in cut._retained_issues.values())
