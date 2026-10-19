"""Concurrent backend selections preserve shared inputs and later serial results."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from rdflib import Graph, Literal, Namespace
from rdflib.namespace import RDF, SH, XSD

from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.shacl.checking import validate_shapes
from bijux_ontology.shacl.execution.results import ShaclResult

EX = Namespace("https://example.org/")


def test_concurrent_shape_modes_own_graphs_and_keep_options_independent() -> None:
    data = Graph().add((EX.complete, EX["count"], Literal(1)))
    data.add((EX.incomplete, RDF.type, EX.Record))
    shapes = Graph().add((EX.Shape, RDF.type, SH.NodeShape))
    shapes.add((EX.Shape, SH.targetNode, EX.complete))
    shapes.add((EX.Shape, SH.property, EX.Property))
    shapes.add((EX.Property, SH.path, EX["count"]))
    shapes.add((EX.Property, SH.minCount, Literal(1)))
    shapes.add((EX.Property, SH.datatype, XSD.integer))
    prepared_data, prepared_shapes = prepare_source(data), prepare_source(shapes)
    before_data, before_shapes = frozenset(data), frozenset(shapes)
    barrier = Barrier(2)

    def validate(mode: str) -> ShaclResult:
        barrier.wait(timeout=5)
        return validate_shapes(
            prepared_data,
            prepared_shapes,
            focus_nodes=(str(EX.incomplete),),
            use_shapes=(str(EX.Shape),) if mode == "pairs" else (),
            mode=mode,
            preflight=mode == "targets",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        filtered, paired = executor.map(validate, ("targets", "pairs"))
    assert filtered.conforms is True and filtered.preflight
    assert filtered.report.verified and paired.report.complete
    assert filtered.mode == "targets" and filtered.shapes == ()
    assert paired.conforms is False and not paired.preflight
    assert paired.mode == "pairs" and paired.focus_nodes == (str(EX.incomplete),)
    assert paired.findings[0].focus_node == EX.incomplete
    exported = paired.report_graph()
    exported.remove((None, None, None))
    assert len(paired.report_graph()) > 0
    assert frozenset(data) == before_data and frozenset(shapes) == before_shapes
    assert frozenset(prepared_data.graph()) == before_data
    assert frozenset(prepared_shapes.graph()) == before_shapes
    assert validate_shapes(prepared_data, prepared_shapes).conforms is True
