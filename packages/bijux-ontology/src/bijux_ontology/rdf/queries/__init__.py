"""Bounded local SPARQL ASK/SELECT over explicitly selected RDF resources."""

from bijux_ontology.rdf.queries.access import query_rdf
from bijux_ontology.rdf.queries.types import QueryResult, QueryScope

__all__ = ["QueryResult", "QueryScope", "query_rdf"]
