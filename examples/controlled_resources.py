"""Use supplied imports and contexts once; select dataset scope explicitly."""

from rdflib.namespace import OWL

from bijux_ontology.declarations.types import TermDeclaration
from bijux_ontology.jsonld.expectations import ContextExpectation
from bijux_ontology.jsonld.processing.preparation import prepare_jsonld
from bijux_ontology.jsonld.processing.resources import (
    OfflineDocumentLoader,
    context_document_loader,
)
from bijux_ontology.jsonld.usages.checking import check_context
from bijux_ontology.rdf.sources.types import RdfSource
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.checking import check_vocabulary
from bijux_ontology.vocabulary.sources.loading import load_vocabulary

LABEL = "https://vocabulary.example/label"
import_url = "https://vocabulary.example/ontology.ttl"
vocabulary = load_vocabulary(
    RdfSource(
        "<https://example.org/ontology> <http://www.w3.org/2002/07/owl#imports> "
        f"<{import_url}> .",
        format="turtle",
        document_iri="https://example.org/ontology",
    ),
    imports={
        import_url: RdfSource(
            f"<{LABEL}> a <{OWL.DatatypeProperty}> .",
            format="turtle",
            document_iri=import_url,
        )
    },
)
view = inspect_vocabulary(vocabulary)
assert view.lookup(LABEL).scopes == ("import[0]",)
assert check_vocabulary(vocabulary, (TermDeclaration(LABEL),)).verified
context_url = "https://example.org/context"
# The default policy is offline. A custom loader would own all retrieval and its
# logical document URLs; the wrapper freezes each response for this operation.
resources = context_document_loader(
    OfflineDocumentLoader({context_url: {"@context": {"label": LABEL}}})
)
document = {
    "@context": context_url,
    "@id": "item",
    "label": "Microscope",
    "@included": [{"@id": "empty", "@graph": []}],
}
base = "https://example.org/data/"
report = check_context(
    document,
    (ContextExpectation(("label",), LABEL),),
    base=base,
    document_loader=resources,
)
assert report.verified
wrong = check_context(
    document,
    (ContextExpectation(("label",), "https://example.org/wrong"),),
    base=base,
    document_loader=resources,
)
assert wrong.exit_code() == 1
prepared = prepare_jsonld(document, base=base, document_loader=resources)
assert prepared.report.verified
assert prepared.source is not None
assert len(prepared.source.graph("@default")) == 1
assert len(prepared.source.graph(base + "empty")) == 0
assert view.lookup(LABEL).scopes == ("import[0]",)
print(report.to_dict())
print(sorted(str(graph.identifier) for graph in prepared.source.dataset().graphs()))
