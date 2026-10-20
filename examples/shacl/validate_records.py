"""Validate supplied shapes and reuse their owned preparation across records."""

from rdflib import BNode, Graph, Literal, Namespace
from rdflib.namespace import RDF, SH

from bijux_ontology.shacl.checking import validate_shapes
from bijux_ontology.shacl.execution.session import ShaclSession

EX = Namespace("https://example.org/")
shapes = Graph().add((EX.Shape, RDF.type, SH.NodeShape))
shapes.add((EX.Shape, SH.targetClass, EX.Record))
name = BNode()
shapes.add((EX.Shape, SH.property, name))
shapes.add((name, SH.path, EX.name))
shapes.add((name, SH.minCount, Literal(1)))
data = Graph().add((EX.record, RDF.type, EX.Record))
data.add((EX.record, EX.name, Literal("Image")))
assert validate_shapes(data, shapes).report.verified

with ShaclSession(shapes) as session:
    assert session.validate(data).report.verified
    data.remove((EX.record, EX.name, None))
    failed = session.validate(data)
    assert failed.conforms is False and not failed.report.verified
print("Fixed shapes reused; missing required output remains a native finding.")
