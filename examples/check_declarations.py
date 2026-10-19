"""Inspect and check explicit RDF declarations; no general OWL reasoning or network."""

from bijux_ontology.declarations.types import TermDeclaration
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.vocabulary.checking import check_vocabulary
from bijux_ontology.vocabulary.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.sources.loading import load_vocabulary

vocabulary = load_vocabulary(
    RdfSource(
        """
@prefix ex: <https://example.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
ex:Image a owl:Class ; rdfs:label "Image"@en .
""",
        format="turtle",
    )
)
view = inspect_vocabulary(vocabulary)
assert view.lookup("https://example.org/Image").status == "declared"
assert view.labels("https://example.org/Image").items[0].value == "Image"
accepted = check_vocabulary(
    vocabulary,
    (TermDeclaration("https://example.org/Image"),),
    local_namespaces=("https://example.org/",),
    require_coverage=True,
)
assert accepted.verified
missing = check_vocabulary(
    vocabulary, (TermDeclaration("https://example.org/Missing"),)
)
assert missing.exit_code() == 1
assert {issue.code for issue in missing.issues} == {"vocabulary.term_missing"}
print("Declared term:", accepted.to_dict())
print("Missing term:", missing.to_dict())
