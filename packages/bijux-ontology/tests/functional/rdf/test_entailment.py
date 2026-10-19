"""Maintained entailment preparation is explicit, scoped and independently checked."""

import json
from threading import Event
from typing import cast

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS, SH, XSD

from bijux_ontology.declarations import FieldDeclaration as Field
from bijux_ontology.declarations import ModelDeclaration as Model
from bijux_ontology.declarations import ValueDeclaration as Value
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.workers import execute_worker
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.models.emission.checking import check_emission
from bijux_ontology.models.emission.types import ModelUsage
from bijux_ontology.rdf.entailment import preparation, prepare_entailment
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.shacl.checking import validate_shapes

EX = Namespace("https://example.org/")


def domain_graph() -> Graph:
    """Use a real domain consequence, not SHACL's existing subclass traversal."""
    return Graph().add((EX.s, EX.p, EX.o)).add((EX.p, RDFS.domain, EX.Person))


def domain_shape() -> Graph:
    """Require an actual type edge via Core property/value constraints."""
    return (
        Graph()
        .add((EX.Shape, RDF.type, SH.NodeShape))
        .add((EX.Shape, SH.targetNode, EX.s))
        .add((EX.Shape, SH.property, EX.Type))
        .add((EX.Type, SH.path, RDF.type))
        .add((EX.Type, SH.hasValue, EX.Person))
    )


def test_actual_rdfs_domain_and_rl_inverse_change_graph_checks_only_explicitly() -> (
    None
):
    pytest.importorskip("owlrl")
    pytest.importorskip("pyshacl")
    graph = domain_graph()
    before = frozenset(graph)
    raw = validate_shapes(graph, domain_shape())
    assert raw.conforms is False
    rdfs = prepare_entailment(graph, regime="rdfs")
    assert rdfs.report.verified and rdfs.asserted is not None
    assert rdfs.derived is not None and rdfs.effective is not None
    assert frozenset(rdfs.asserted.graph()) == before
    assert (EX.s, RDF.type, EX.Person) in rdfs.derived.graph()
    assert validate_shapes(rdfs.effective, domain_shape()).report.verified
    inverse = Graph().add((EX.s, EX.p, EX.o)).add((EX.p, OWL.inverseOf, EX.q))
    shape = Graph().add((EX.S, RDF.type, SH.NodeShape)).add((EX.S, SH.targetNode, EX.o))
    shape.add((EX.S, SH.property, EX.Prop)).add((EX.Prop, SH.path, EX.q))
    shape.add((EX.Prop, SH.hasValue, EX.s))
    assert validate_shapes(inverse, shape).conforms is False
    shallow = prepare_entailment(inverse, regime="rdfs")
    assert shallow.report.verified and shallow.effective is not None
    assert validate_shapes(shallow.effective, shape).conforms is False
    rl = prepare_entailment(inverse, regime="owl-rl")
    assert rl.report.verified and rl.effective is not None
    assert validate_shapes(rl.effective, shape).report.verified
    assert frozenset(graph) == before


def test_literals_and_final_generalized_facts_retain_exact_joint_blank_identity() -> (
    None
):
    pytest.importorskip("owlrl")
    shared, other = BNode(), BNode()
    lexical = Literal("01", datatype=XSD.integer, normalize=False)
    graph = domain_graph().add((EX.s, EX["count"], lexical))
    graph.add((EX.s, EX.left, shared)).add((EX.s, EX.right, shared))
    graph.add((EX.s, EX.other, other)).add((shared, EX.value, lexical))
    for regime in ("rdfs", "owl-rl"):
        result = prepare_entailment(graph, regime=regime)
        assert result.report.verified and result.effective is not None
        assert result.asserted is not None and result.derived is not None
        effective = result.effective.graph()
        left = effective.value(EX.s, EX.left)
        assert left is effective.value(EX.s, EX.right)
        assert left != effective.value(EX.s, EX.other)
        assert str(effective.value(EX.s, EX["count"])) == "01"
        assert any(
            isinstance(s, Literal) and str(s) == "01" for s, _, _ in result.generalized
        )
        assert all(not isinstance(s, Literal) for s, _, _ in effective)
        if regime == "owl-rl":
            assert any(
                s is left and p == OWL.sameAs and o is left
                for s, p, o in result.derived.graph()
            )
        assert any(s == lexical for s, _, _ in result.generalized)
    # Ordinary predicates do not need an OWL vocabulary declaration.
    assert prepare_entailment(
        Graph().add((EX.s, EX.any, EX.o)), regime="owl-rl"
    ).report.verified
    custom = Graph().add((EX.s, EX.any, Literal("opaque", datatype=EX.Custom)))
    unsupported = prepare_entailment(custom, regime="owl-rl")
    assert not unsupported.report.verified and unsupported.effective is None
    assert unsupported.asserted is not None
    assert "datatype" in unsupported.report.issues[0].message
    assert prepare_entailment(custom, regime="rdfs").report.verified


def test_physical_pins_union_and_owned_blank_graphs_keep_declared_source_scope() -> (
    None
):
    pytest.importorskip("owlrl")
    dataset = Dataset()
    for triple in domain_graph():
        dataset.graph(EX.g).add(triple)
    dataset.graph(EX.hidden).add((EX.s, RDF.type, EX.Hidden))
    source = prepare_rdf(
        RdfSource(
            dataset,
            document_iri=str(EX.document),
            effective_url=str(EX.effective),
            graph_name=str(EX.g),
        )
    )
    result = prepare_entailment(source, regime="rdfs")
    assert result.report.verified and result.effective is not None
    assert result.effective.graph_name == str(EX.g)
    assert result.effective.document_iri == str(EX.document)
    assert result.effective.effective_url == str(EX.effective)
    assert (EX.s, RDF.type, EX.Hidden) not in result.effective.graph()
    assert (EX.s, RDF.type, EX.Hidden) in dataset.graph(EX.hidden)
    conflict = prepare_entailment(source, regime="rdfs", graph_name=str(EX.hidden))
    assert not conflict.report.verified and conflict.effective is None
    invalid_selector = prepare_entailment(
        source, regime="rdfs", graph_name=Literal(str(EX.g))
    )
    assert not invalid_selector.report.verified and invalid_selector.effective is None
    unpinned = prepare_rdf(dataset)
    assert not prepare_entailment(unpinned, regime="rdfs").report.verified
    merged = prepare_entailment(unpinned, regime="rdfs", union=True)
    assert merged.report.verified and merged.union and merged.effective is not None
    assert not merged.effective.is_dataset
    assert (EX.s, RDF.type, EX.Hidden) in merged.effective.graph()
    named = BNode()
    blank_dataset = Dataset()
    blank_dataset.graph(named).add((named, EX.p, EX.o))
    owned = prepare_rdf(blank_dataset)
    pin = next(owned._graph_names())
    resolved = prepare_entailment(owned, regime="rdfs", graph_name=pin)
    assert resolved.report.verified and resolved.effective is not None
    output_pin = resolved.graph_name
    assert isinstance(output_pin, BNode)
    for snapshot in (resolved.asserted, resolved.derived, resolved.effective):
        assert snapshot is not None and next(snapshot._graph_names()) is output_pin
    assert any(
        s is output_pin for s, _, _ in resolved.effective.graph(graph_name=output_pin)
    )
    partial = prepare_entailment(
        owned, regime="rdfs", graph_name=pin, limits=Limits(max_states=300)
    )
    assert partial.report.outcome == "exhausted" and partial.asserted is not None
    assert any(name is partial.graph_name for name in partial.asserted._graph_names())
    foreign = prepare_entailment(owned, regime="rdfs", graph_name=BNode(str(pin)))
    assert not foreign.report.verified and foreign.effective is None


def test_emitted_json_required_fields_are_independent_of_inferred_types() -> None:
    pytest.importorskip("owlrl")
    pytest.importorskip("pyld")
    document = {"@id": str(EX.s), str(EX.p): {"@id": str(EX.o)}}
    model = Model(
        "record",
        (),
        (
            Field(
                "kind",
                str(RDF.type),
                (Value("iri"),),
                key="@type",
                emission="guaranteed",
            ),
        ),
    )
    before = check_emission(document, (model,), (ModelUsage("record"),))
    assert any(i.code == "emission.required_occurrence_missing" for i in before.issues)
    inferred = prepare_entailment(domain_graph(), regime="rdfs")
    assert inferred.report.verified and inferred.effective is not None
    assert (EX.s, RDF.type, EX.Person) in inferred.effective.graph()
    after = check_emission(document, (model,), (ModelUsage("record"),))
    assert after == before and "@type" not in document


def test_closure_limits_deadline_cancel_and_unsupported_work_never_verify_partial() -> (
    None
):
    pytest.importorskip("owlrl")
    graph = domain_graph()
    capped = prepare_entailment(graph, regime="owl-rl", limits=Limits(max_triples=50))
    assert capped.report.outcome == "exhausted" and capped.effective is None
    assert capped.asserted is not None
    short = prepare_entailment(graph, regime="rdfs", timeout=0.001)
    assert short.report.outcome == "exhausted" and short.effective is None
    event = Event()
    event.set()
    cancelled = prepare_entailment(graph, regime="rdfs", cancel=event)
    assert cancelled.report.outcome == "exhausted" and cancelled.effective is None
    assert prepare_entailment(graph, regime="rdfs").report.verified
    imported = graph + Graph().add((EX.schema, OWL.imports, EX.absent))
    missing = prepare_entailment(imported, regime="owl-rl")
    assert not missing.report.verified and missing.effective is None
    invalid = graph + Graph().add(
        (EX.s, EX["count"], Literal("bad", datatype=XSD.integer, normalize=False))
    )
    assert not prepare_entailment(invalid, regime="owl-rl").report.verified
    operation = Budget(Limits(max_issues=1))
    operation.consume("issues")
    failed = prepare_entailment(imported, regime="owl-rl", budget=operation)
    assert failed.report.outcome == "exhausted" and failed.effective is None
    with pytest.raises(InputError):
        prepare_entailment(graph, regime="owl-dl")  # type: ignore[arg-type]


def test_host_qualifies_worker_asserted_integrity_and_disjoint_derived_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("owlrl")
    mode = "lexical"

    def altered(
        module: str,
        payload: bytes,
        *,
        cap: int,
        timeout: float,
        cancel: Event | None = None,
    ) -> bytes:
        """Change a real completed reply while retaining its ordinary typed wire."""
        raw = execute_worker(module, payload, cap=cap, timeout=timeout, cancel=cancel)
        response = cast(dict[str, object], json.loads(raw))
        asserted = cast(list[list[dict[str, object]]], response["asserted"])
        if mode == "lexical":
            asserted[0][2]["value"] = "1"
        elif mode == "overlap":
            cast(list[object], response["derived"]).append(asserted[0])
        else:
            response["selection"] = {"kind": "blank", "value": "detached-graph"}
        return json.dumps(response).encode()

    monkeypatch.setattr(preparation, "execute_worker", altered)
    lexical = Literal("01", datatype=XSD.integer, normalize=False)
    graph = Graph().add((EX.s, EX["count"], lexical))
    altered_source = prepare_entailment(graph, regime="rdfs")
    assert not altered_source.report.verified and altered_source.effective is None
    assert altered_source.asserted is not None
    assert (EX.s, EX["count"], lexical) in altered_source.asserted.graph()
    assert any(i.code == "rdf.structure_changed" for i in altered_source.report.issues)
    assert len(altered_source.report.children) == 1
    mode = "overlap"
    overlapping = prepare_entailment(graph, regime="rdfs")
    assert not overlapping.report.verified and overlapping.effective is None
    assert overlapping.asserted is not None
    assert (EX.s, EX["count"], lexical) in overlapping.asserted.graph()
    assert any("overlap" in i.message for i in overlapping.report.issues)
    mode = "selection"
    name = BNode()
    dataset = Dataset()
    dataset.graph(name).add((name, EX.p, EX.o))
    source = prepare_rdf(dataset)
    selected = next(source._graph_names())
    detached = prepare_entailment(source, regime="rdfs", graph_name=selected)
    assert not detached.report.verified and detached.effective is None
    assert detached.asserted is not None and detached.graph_name is selected
    assert any(i.code == "rdf.structure_changed" for i in detached.report.issues)
