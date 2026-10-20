"""Navigate asserted SKOS concepts and separate integrity from publication policy."""

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import SKOS

from bijux_ontology.skos.checking import check_skos
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.browsing.navigation import traverse

EX = "https://example.org/"
graph = Graph().parse(
    data="""
@prefix ex: <https://example.org/> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
ex:scheme a skos:ConceptScheme .
ex:Imaging a skos:Concept ; skos:inScheme ex:scheme ;
    skos:prefLabel "Imaging"@en, "Avbildning"@sv .
ex:Microscopy a skos:Concept ; skos:inScheme ex:scheme ;
    skos:prefLabel "Microscopy"@en, "Mikroskopi"@sv ; skos:broader ex:Imaging .
""",
    format="turtle",
)
view = inspect_vocabulary(graph)
labels = view.labels(EX + "Microscopy")
assert {(value.value, value.language) for value in labels.items} == {
    ("Microscopy", "en"),
    ("Mikroskopi", "sv"),
}
parents = traverse(view, EX + "Microscopy", predicates=(str(SKOS.broader),))
assert [value.iri for value in parents.items] == [EX + "Imaging"]
assert check_skos(graph).verified
publication = check_skos(
    graph, rules=("language-coverage", "scheme-required"), languages=("en", "sv")
)
assert publication.verified
# A second preferred label in the same language violates selected SKOS integrity.
graph.add(
    (URIRef(EX + "Microscopy"), SKOS.prefLabel, Literal("Microscope work", lang="en"))
)
conflict = check_skos(graph)
assert "skos.preferred_label_conflict" in {issue.code for issue in conflict.issues}
graph.remove(
    (URIRef(EX + "Microscopy"), SKOS.prefLabel, Literal("Microscope work", lang="en"))
)
assert check_skos(graph).verified
# Missing Swedish is an explicit publication policy, not a universal SKOS conflict.
graph.remove(
    (URIRef(EX + "Microscopy"), SKOS.prefLabel, Literal("Mikroskopi", lang="sv"))
)
assert check_skos(graph).verified
policy = check_skos(graph, rules=("language-coverage",), languages=("en", "sv"))
assert "skos.policy.language_missing" in {issue.code for issue in policy.issues}
print("Integrity conflict:", conflict.to_dict())
print("Publication requirement:", policy.to_dict())
