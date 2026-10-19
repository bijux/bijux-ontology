"""Locate a catalogue contract broken by a vocabulary and context change."""

from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.jsonld.contracts.expectations import ContextExpectation
from bijux_ontology.jsonld.usages.checking import prepare_usages
from bijux_ontology.workflows.upgrades.assessment.checking import assess_upgrade

EX = "https://example.org/"
before = Graph().parse(
    data="""
@prefix ex: <https://example.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
ex:Item a owl:Class .
ex:label a owl:DatatypeProperty ; rdfs:range rdf:langString .
ex:tags a rdf:Property ; rdfs:range rdf:List .
ex:preview a rdf:Property .
""",
    format="turtle",
)
after = Graph() + before
after.remove((URIRef(EX + "label"), None, None))
after.add((URIRef(EX + "caption"), RDF.type, OWL.DatatypeProperty))
after.add((URIRef(EX + "caption"), RDFS.range, RDF.langString))
model = ModelDeclaration(
    "CatalogueItem",
    (EX + "Item",),
    (
        FieldDeclaration(
            "title", EX + "label", (ValueDeclaration("literal", language="en"),)
        ),
        FieldDeclaration(
            "tags",
            EX + "tags",
            (ValueDeclaration("literal", str(XSD.string), container="@list"),),
        ),
    ),
)


def catalogue_entry(title_predicate: str) -> dict[str, object]:
    """The title mapping is scoped to entries; tags retain RDF list meaning."""
    return {
        "@context": {
            "@vocab": EX,
            "entry": {
                "@id": EX + "entry",
                "@context": {
                    "title": {"@id": title_predicate, "@language": "en"},
                    "tags": {"@id": EX + "tags", "@container": "@list"},
                },
            },
        },
        "entry": {"@type": "Item", "title": "Microscope", "tags": ["image", "open"]},
    }


expectations = (
    ContextExpectation(("entry", "title"), EX + "label", language="en"),
    ContextExpectation(("entry", "tags"), EX + "tags", container="list"),
)
old = prepare_usages(catalogue_entry(EX + "label"))
new = prepare_usages(catalogue_entry(EX + "caption"))
old_report, new_report = old.check(expectations), new.check(expectations)
assert old_report.verified and new_report.exit_code() == 1
unchanged = assess_upgrade(
    before,
    before,
    models=(model,),
    contract_reports=((old_report, old_report),),
)
assert unchanged.verdict == "agreement"
upgrade = assess_upgrade(
    before,
    after,
    models=(model,),
    observed_usages=(old.observe(("entry", "title")), new.observe(("entry", "title"))),
    contract_reports=((old_report, new_report),),
)
assert upgrade.verdict == "demonstrated-break"
assert any(
    use.change.kind == "term-removed" and use.use.field == "title"
    for use in upgrade.affected
)
assert any(
    delta.after is not None and delta.after.code == "context.mapping_mismatch"
    for delta in upgrade.contracts
)
# A separate unknown serializer is still unverified, even without an upgrade.
unknown = ModelDeclaration(
    "Preview",
    (EX + "Item",),
    (FieldDeclaration("preview", EX + "preview", (ValueDeclaration(),)),),
)
limited = assess_upgrade(before, before, models=(unknown,))
assert limited.verdict == "unverified" and not limited.report.verified
print(
    upgrade.verdict,
    sorted({item.use.field for item in upgrade.affected if item.use.field}),
)
print("Unsupported serializer:", limited.verdict)
