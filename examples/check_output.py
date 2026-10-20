"""Check independent intent against actual JSON-LD and native RDF output."""

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.jsonld.contracts.expectations import ContextExpectation
from bijux_ontology.jsonld.usages.checking import check_context
from bijux_ontology.models.contracts.checking import check_models
from bijux_ontology.models.emission.checking import check_emission
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import ModelUsage, RdfModelUsage
from bijux_ontology.workflows.projects.contracts import (
    ContractObligation,
    check_contract,
)

EX = Namespace("https://example.org/")
model = ModelDeclaration(
    "record",
    (str(EX.Record),),
    (
        FieldDeclaration(
            "name",
            str(EX.name),
            (ValueDeclaration("literal", datatype=str(XSD.string)),),
            emission="guaranteed",
        ),
    ),
)
ontology = Graph().add((EX.Record, RDF.type, OWL.Class))
ontology.add((EX.name, RDF.type, OWL.DatatypeProperty))
ontology.add((EX.name, RDFS.range, XSD.string))
assert check_models((model,), ontology).verified

# The context and emitted data are independent of the declaration above.
document = {"@context": {"@vocab": str(EX)}, "@type": "Record", "name": "Image"}
mapping = (ContextExpectation(("name",), str(EX.name)),)
assert check_context(document, mapping).verified
assert not check_context(
    {**document, "@context": {"@vocab": "https://other.example/"}},
    mapping,
).verified
output = check_emission(document, (model,), (ModelUsage("record"),))
check_contract((ContractObligation("output", lambda: output),)).require_verified()
assert not check_emission(
    {**document, "name": 7},
    (model,),
    (ModelUsage("record"),),
).verified

data = Graph().add((EX.image, RDF.type, EX.Record))
data.add((EX.image, EX.name, Literal("Image")))
source = prepare_source(data)
binding = RdfModelUsage("record", URIRef(EX.image))
assert check_rdf_emission(source, (model,), (binding,)).verified
data.remove((EX.image, EX.name, None))
assert not check_rdf_emission(prepare_source(data), (model,), (binding,)).verified
print("Actual JSON-LD/RDF output checked; wrong and missing values rejected.")
