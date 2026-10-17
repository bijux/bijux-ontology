"""Keep custom annotation meaning in consumer configuration and trusted rules."""

from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF

from bijux_ontology.reports import Issue
from bijux_ontology.data.rules import DataView, run_rules
from bijux_ontology.vocabulary.inspection import inspect_vocabulary

EX = "https://example.org/"
conventions = {
    "label": EX + "caption",
    "definition": EX + "explanation",
    "synonym": EX + "alternative",
    "deprecated": EX + "retired",
    "replacement": EX + "replacedBy",
}
graph = Graph().parse(
    data="""
@prefix ex: <https://example.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
ex:Image a owl:Class ; ex:caption "Image"@en ; ex:explanation "A captured image."@en .
ex:OldImage a owl:Class ; ex:caption "Old image"@en ;
    ex:explanation "An older image term."@en ; ex:alternative "Picture"@en ;
    ex:retired true ; ex:replacedBy ex:Image .
""",
    format="turtle",
)
view = inspect_vocabulary(graph)
for role in ("label", "definition", "synonym", "deprecated"):
    values = view.annotations(EX + "OldImage", predicates=(conventions[role],))
    assert values.items
replacement = view.relationships(
    EX + "OldImage", predicates=(conventions["replacement"],)
)
assert str(replacement.items[0].object) == EX + "Image"


def require_definitions(data: DataView) -> list[Issue]:
    """This consumer requires its own definition predicate on declared classes."""
    definitions = URIRef(conventions["definition"])
    return [
        Issue(
            "example.definition_missing",
            "A class needs a supplied definition",
            str(term),
        )
        for term, _, _ in data.triples((None, RDF.type, OWL.Class))
        if not any(data.triples((term, definitions, None)))
    ]


assert run_rules(graph, {"publication-definitions": require_definitions}).verified
broken = view.graph()
broken.remove((URIRef(EX + "Image"), URIRef(conventions["definition"]), None))
report = run_rules(broken, {"publication-definitions": require_definitions})
assert report.exit_code() == 1
assert [issue.location for issue in report.issues] == [EX + "Image"]
assert run_rules(graph, {"publication-definitions": require_definitions}).verified
print(report.to_dict())
