"""Project finite authored TBox and ABox facts independently of OAK presentation."""

from rdflib import URIRef
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.execution.limits import Budget
from bijux_ontology.rdf.roles.classification import classify_statement
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.restrictions.projection import project_restrictions

from .completion import require_complete


def project_relationships(
    view: VocabularyView,
    *,
    subjects: set[str] | None,
    predicates: set[str] | None,
    objects: set[str] | None,
    include_tbox: bool,
    include_abox: bool,
    budget: Budget,
) -> set[tuple[str, str, str]]:
    """Retain only complete selected assertions and supported authored existentials."""
    selected = view.all_statements(budget=budget)
    require_complete(selected.report)
    object_properties = set()
    owners = set()
    for edge in selected.items:
        budget.consume("work")
        if edge.predicate == RDF.type and edge.object == OWL.ObjectProperty:
            budget.consume("states")
            object_properties.add(edge.subject)
        if edge.predicate == RDFS.subClassOf and isinstance(edge.subject, URIRef):
            budget.consume("states")
            owners.add(edge.subject)
    triples = set()

    def retain(subject: URIRef, predicate: URIRef, obj: URIRef) -> None:
        """Apply exact native filters before bounded tuple retention."""
        budget.consume("work")
        for node in (subject, predicate, obj):
            bound_node(node, budget)
        if (
            subjects is not None
            and str(subject) not in subjects
            or objects is not None
            and str(obj) not in objects
            or predicates is not None
            and str(predicate) not in predicates
        ):
            return
        budget.consume("states")
        triples.add((str(subject), str(predicate), str(obj)))

    for edge in selected.items:
        budget.consume("work")
        if not isinstance(edge.subject, URIRef) or not isinstance(edge.object, URIRef):
            continue
        if include_tbox and edge.predicate == RDFS.subClassOf:
            retain(edge.subject, edge.predicate, edge.object)
        elif include_abox:
            if edge.predicate in object_properties:
                retain(edge.subject, edge.predicate, edge.object)
            elif edge.predicate == RDF.type:
                occurrences = classify_statement(
                    (edge.subject, edge.predicate, edge.object),
                    source_scope=edge.scope,
                    budget=budget,
                )
                individual = any(
                    item.position == "subject" and item.role == "individual"
                    for item in occurrences
                )
                application_class = any(
                    item.position == "object" and item.role == "class"
                    for item in occurrences
                )
                if individual and application_class:
                    retain(edge.subject, edge.predicate, edge.object)
    if include_tbox and (predicates is None or predicates - {str(RDFS.subClassOf)}):
        for owner in owners:
            budget.consume("work")
            bound_node(owner, budget)
            if subjects is not None and str(owner) not in subjects:
                continue
            projections = project_restrictions(
                view, str(owner), inherited=False, budget=budget
            )
            require_complete(projections.report)
            for item in projections.items:
                budget.consume("work")
                if (
                    item.status == "supported"
                    and item.quantifier == OWL.someValuesFrom
                    and isinstance(item.property, URIRef)
                    and isinstance(item.filler, URIRef)
                ):
                    retain(item.authored_owner, item.property, item.filler)
    return triples
