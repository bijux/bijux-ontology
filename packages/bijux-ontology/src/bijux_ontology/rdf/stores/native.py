"""Exact term qualification at the maintained native database boundary."""

from collections.abc import Sequence

import pyoxigraph as ox
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.queries.access import QueryScope
from bijux_ontology.rdf.queries.literals import LiteralScopeUnsupported
from bijux_ontology.rdf.queries.protocol import QueryTerm
from bijux_ontology.rdf.terms import literal_term

from .protocol import scope_data
from .seals import verify_files
from .types import StoredRdf


def rdf_term(value: object) -> QueryTerm | None:
    """Keep supported ordinary RDF lexical terms; reject native extensions."""
    if isinstance(value, ox.DefaultGraph):
        return None
    if isinstance(value, ox.NamedNode):
        return URIRef(value.value)
    if isinstance(value, ox.BlankNode):
        return BNode(value.value)
    if isinstance(value, ox.Literal):
        if value.direction is not None:
            raise LiteralScopeUnsupported(
                "Directional literals need an explicit RDF profile"
            )
        return literal_term(
            value.value,
            datatype=None if value.language else value.datatype.value,
            language=value.language,
        )
    raise LiteralScopeUnsupported("The disk store supports ordinary RDF terms only")


def native_term(value: QueryTerm | None) -> object:
    """Encode an already qualified owned RDF selector without label rebinding."""
    if value is None:
        return ox.DefaultGraph()
    if isinstance(value, URIRef):
        return ox.NamedNode(str(value))
    if isinstance(value, BNode):
        return ox.BlankNode(str(value))
    if isinstance(value, Literal):
        return (
            ox.Literal(str(value), language=value.language)
            if value.language
            else ox.Literal(
                str(value), datatype=ox.NamedNode(str(value.datatype or XSD.string))
            )
        )
    raise InputError("Unsupported native RDF selector")


def qualify_literal(value: ox.Literal, probe: ox.Store) -> None:
    """Use the actual native store to detect lexical normalization before adoption.

    The database canonicalizes some datatype lexical forms. Such inputs are
    unsupported, never silently converted to different RDF terms. Plain and
    language strings have intrinsic RDF identity; other terms get a real
    maintained round trip through a constant-sized scratch store.
    """
    rdf_term(value)
    if value.datatype.value in (str(XSD.string), str(RDF.langString)):
        return
    probe.clear()
    probe.add(
        ox.Quad(
            ox.NamedNode("urn:bijux:store:subject"),
            ox.NamedNode("urn:bijux:store:predicate"),
            value,
        )
    )
    returned = next(iter(probe)).object
    if returned != value:
        raise LiteralScopeUnsupported(
            "The native store changes this literal's RDF lexical identity"
        )


class DeclarationReader:
    """Own one sealed native handle for bounded selected declaration reads.

    The enclosing worker owns the deadline and process lifetime. No complete
    dataset, term cache or additional child process is created.
    """

    def __init__(
        self, snapshot: StoredRdf, *, scope: QueryScope, budget: Budget
    ) -> None:
        """Retain explicit physical scope and the caller's cumulative allowance."""
        self.snapshot = snapshot
        self.scope = scope
        self.budget = budget
        self._store: ox.Store | None = None
        self._graph_name: ox.DefaultGraph | ox.NamedNode | ox.BlankNode | None = None

    def __enter__(self) -> "DeclarationReader":
        """Qualify the sealed revision and open its read-only handle once."""
        if self._store is not None:
            raise InputError("A declaration reader cannot be entered twice")
        scope_data(self.snapshot, self.scope, self.budget)
        if self.scope.kind == "dataset":
            raise InputError("Declaration reads require default, named or union scope")
        if self.scope.kind == "default":
            self._graph_name = ox.DefaultGraph()
        elif self.scope.kind == "named":
            name = self.scope.graph_name
            assert name is not None
            self._graph_name = (
                ox.BlankNode(str(name))
                if isinstance(name, BNode)
                else ox.NamedNode(str(name))
            )
        verify_files(self.snapshot.directory, self.snapshot.files, self.budget)
        self.budget.consume("resources")
        self._store = ox.Store.read_only(str(self.snapshot.directory))
        return self

    def graph(self, iris: Sequence[str]) -> Graph:
        """Read exact selected type triples without retaining previous selections."""
        if self._store is None:
            raise InputError("Declaration reads require an open owning context")
        selected = capture_items(iris, str, self.budget)
        self.budget.consume("states")
        graph = Graph(bind_namespaces="none")
        for iri in selected:
            self.budget.consume("work")
            bound_text(iri, self.budget)
            subject = URIRef(absolute_iri(iri))
            for quad in self._store.quads_for_pattern(
                ox.NamedNode(str(subject)),
                ox.NamedNode(str(RDF.type)),
                None,
                self._graph_name,
            ):
                self.budget.consume("work")
                self.budget.consume("triples")
                self.budget.consume("states")
                value = rdf_term(quad.object)
                if value is None:
                    raise InputError("Unbound RDF type declaration")
                bound_text(str(value), self.budget)
                if isinstance(value, Literal):
                    if value.datatype is not None:
                        bound_text(str(value.datatype), self.budget)
                    if value.language is not None:
                        bound_text(value.language, self.budget)
                graph.add((subject, RDF.type, value))
        return graph

    def __exit__(
        self, exception_type: object, exception: object, traceback: object
    ) -> None:
        """Close the native handle and qualify the revision before accepting reads."""
        self._store = None
        if exception_type is None:
            verify_files(self.snapshot.directory, self.snapshot.files, self.budget)


def declaration_graph(
    snapshot: StoredRdf, iris: Sequence[str], *, scope: QueryScope, budget: Budget
) -> Graph:
    """Read one bounded declaration selection inside an already owned worker."""
    with DeclarationReader(snapshot, scope=scope, budget=budget) as reader:
        return reader.graph(iris)
