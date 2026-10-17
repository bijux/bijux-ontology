"""Owned vocabulary snapshots for bounded exact-IRI inspection."""

from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Literal as Direction
from typing import TypeVar, cast

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD
from rdflib.term import Node

from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.owl.constructs import unsupported_constructs
from bijux_ontology.rdf.identifiers import absolute_iri, language_tag
from bijux_ontology.rdf.snapshots import bind_namespaces, copy_graph
from bijux_ontology.vocabulary.identifiers import expand_curie

from ..errors import InputError, LimitExceeded
from ..reports import Report
from .loading import load_vocabulary
from .results import (
    Annotation,
    Deprecation,
    Description,
    Edge,
    LabelSelection,
    SearchMatch,
    Selection,
    StructuralView,
    Summary,
    TermInfo,
)
from .view import Vocabulary

T = TypeVar("T")


class VocabularyView:
    """Inspect one owned scope snapshot; every multi-result operation is bounded."""

    def __init__(
        self,
        vocabulary: Vocabulary | Graph,
        *,
        scopes: Sequence[str] = ("local", "imports"),
        limits: Limits | None = None,
    ) -> None:
        source = (
            vocabulary
            if isinstance(vocabulary, Vocabulary)
            else load_vocabulary(vocabulary, limits=limits)
        )
        if (
            isinstance(scopes, (str, bytes))
            or not scopes
            or set(scopes) - {"local", "imports", "lookup"}
        ):
            raise InputError("Select local/imports/lookup source roles explicitly")
        self.limits = limits if limits is not None else source.limits
        selected: list[tuple[str, Graph]] = []
        if "local" in scopes:
            selected.append(("local", source.local))
        if "imports" in scopes:
            selected.extend(
                (f"import[{index}]", graph)
                for index, graph in enumerate(source.imports)
            )
        if "lookup" in scopes:
            selected.extend(
                (f"lookup[{index}]", graph) for index, graph in enumerate(source.lookup)
            )
        self._scopes = tuple(selected)
        budget = Budget(self.limits)
        for _, graph in selected:
            budget.consume("resources")
            budget.consume("triples", len(graph))
        self._edges = tuple(
            sorted(
                (
                    Edge(subject, cast(URIRef, predicate), value, scope)
                    for scope, graph in selected
                    for subject, predicate, value in graph
                ),
                key=lambda edge: (
                    str(edge.subject),
                    str(edge.predicate),
                    edge.object.n3(),
                    edge.scope,
                ),
            )
        )
        outgoing: dict[Node, list[Edge]] = {}
        incoming: dict[Node, list[Edge]] = {}
        for edge in self._edges:
            outgoing.setdefault(edge.subject, []).append(edge)
            incoming.setdefault(edge.object, []).append(edge)
        self._outgoing = {node: tuple(edges) for node, edges in outgoing.items()}
        self._incoming = {node: tuple(edges) for node, edges in incoming.items()}
        kinds: dict[str, set[str]] = {}
        locations: dict[str, set[str]] = {}
        declared_locations: dict[str, set[str]] = {}
        for edge in self._edges:
            for node in (edge.subject, edge.predicate, edge.object):
                if isinstance(node, URIRef):
                    locations.setdefault(str(node), set()).add(edge.scope)
                if isinstance(node, Literal) and node.datatype is not None:
                    locations.setdefault(str(node.datatype), set()).add(edge.scope)
            if (
                edge.predicate == RDF.type
                and isinstance(edge.subject, URIRef)
                and isinstance(edge.object, URIRef)
            ):
                kinds.setdefault(str(edge.subject), set()).add(str(edge.object))
                declared_locations.setdefault(str(edge.subject), set()).add(edge.scope)
        self._terms = {
            iri: TermInfo(
                iri,
                "declared" if iri in kinds else "referenced",
                tuple(sorted(kinds.get(iri, ()))),
                tuple(sorted(declared_locations.get(iri, locations[iri]))),
            )
            for iri in locations
        }

    def _select(
        self, values: Iterable[T | None], family: str, *, budget: Budget | None = None
    ) -> Selection[T]:
        budget = budget if budget is not None else Budget(self.limits)
        issues = IssueCollector(budget)
        items: list[T] = []
        complete = True
        try:
            for value in values:
                budget.consume("work")
                if value is not None:
                    budget.consume("states")
                    items.append(value)
        except LimitExceeded as error:
            issues.exhausted(str(error), location=family)
            complete = False
        return Selection(
            tuple(items),
            Report(
                family,
                len(items),
                tuple(issues),
                allow_empty=True,
                complete=complete,
                requested=len(items) if complete else len(items) + 1,
                regime="asserted",
                families=(family,),
            ),
        )

    def lookup(self, iri: str) -> TermInfo:
        """Distinguish supplied declaration, referenced-only identifier and absence."""
        absolute_iri(iri)
        return self._terms.get(iri, TermInfo(iri, "absent", (), ()))

    def terms(
        self,
        *,
        kinds: Sequence[str] = (),
        namespaces: Sequence[str] = (),
        budget: Budget | None = None,
    ) -> Selection[TermInfo]:
        """Enumerate actual declared identifiers and all their kinds and roles."""
        for iri in (*kinds, *namespaces):
            absolute_iri(iri)
        return self._select(
            (
                term
                if (
                    term.status == "declared"
                    and (not kinds or set(kinds).intersection(term.kinds))
                    and (not namespaces or iri.startswith(tuple(namespaces)))
                )
                else None
                for iri, term in sorted(self._terms.items())
            ),
            "terms.inventory",
            budget=budget,
        )

    def resolve(
        self, identifiers: Iterable[str], *, prefixes: Mapping[str, str] | None = None
    ) -> Selection[TermInfo]:
        """Resolve supplied identifiers, retaining order and duplicate requests."""
        return self._select(
            (
                self.lookup(
                    expand_curie(value, prefixes) if prefixes is not None else value
                )
                for value in identifiers
            ),
            "terms.resolve",
        )

    def all_statements(self, *, budget: Budget | None = None) -> Selection[Edge]:
        """Enumerate actual selected assertions with their source ownership intact."""
        return self._select(self._edges, "terms.statements", budget=budget)

    def relationships(
        self,
        iri: str,
        *,
        predicates: Sequence[str] = (),
        direction: Direction["incoming", "outgoing", "both"] = "outgoing",
        budget: Budget | None = None,
    ) -> Selection[Edge]:
        """Inspect asserted positions, including literal and anonymous objects."""
        absolute_iri(iri)
        return self.statements(
            URIRef(iri), predicates=predicates, direction=direction, budget=budget
        )

    def statements(
        self,
        node: URIRef | BNode | Literal,
        *,
        predicates: Sequence[str] = (),
        direction: Direction["incoming", "outgoing", "both"] = "outgoing",
        budget: Budget | None = None,
    ) -> Selection[Edge]:
        """Inspect owned anonymous resources or literal objects using raw RDF terms."""
        if not isinstance(node, URIRef | BNode | Literal):
            raise InputError("Statement lookup requires a supported RDF node")
        if isinstance(node, URIRef):
            absolute_iri(str(node))
        if direction not in ("incoming", "outgoing", "both"):
            raise InputError("Unknown relationship direction")
        for predicate in predicates:
            absolute_iri(predicate)
        candidates = (
            self._outgoing.get(node, ())
            if direction == "outgoing"
            else self._incoming.get(node, ())
            if direction == "incoming"
            else tuple(
                dict.fromkeys(
                    (*self._outgoing.get(node, ()), *self._incoming.get(node, ()))
                )
            )
        )
        return self._select(
            (
                edge if not predicates or str(edge.predicate) in predicates else None
                for edge in candidates
            ),
            "terms.relationships",
            budget=budget,
        )

    def structure(self, iri: str) -> StructuralView:
        """Inspect class/property assertions without flattening simultaneous values."""
        predicates = (
            RDFS.subClassOf,
            RDFS.subPropertyOf,
            RDFS.domain,
            RDFS.range,
            OWL.equivalentClass,
            OWL.equivalentProperty,
            OWL.disjointWith,
            OWL.propertyDisjointWith,
            OWL.inverseOf,
        )
        selected = self.relationships(iri, predicates=tuple(map(str, predicates)))
        characteristics = (
            OWL.FunctionalProperty,
            OWL.InverseFunctionalProperty,
            OWL.TransitiveProperty,
            OWL.SymmetricProperty,
            OWL.AsymmetricProperty,
            OWL.ReflexiveProperty,
            OWL.IrreflexiveProperty,
        )
        kinds = self.lookup(iri).kinds
        return StructuralView(
            tuple(edge for edge in selected.items if isinstance(edge.object, URIRef)),
            tuple(edge for edge in selected.items if isinstance(edge.object, BNode)),
            tuple(str(kind) for kind in characteristics if str(kind) in kinds),
            unsupported_constructs(self.graph(), URIRef(iri)),
            selected.report,
        )

    def annotations(
        self, iri: str, *, predicates: Sequence[str], budget: Budget | None = None
    ) -> Selection[Annotation]:
        """Retain every selected literal's lexical form, language and datatype."""
        edges = self.relationships(iri, predicates=predicates, budget=budget)
        values = tuple(
            Annotation(
                str(edge.predicate),
                str(edge.object),
                edge.object.language,
                str(edge.object.datatype) if edge.object.datatype is not None else None,
                edge.scope,
                edge.object,
            )
            for edge in edges.items
            if isinstance(edge.object, Literal)
        )
        return Selection(values, edges.report)

    def labels(
        self, iri: str, *, budget: Budget | None = None
    ) -> Selection[Annotation]:
        """Return RDFS and SKOS preferred labels without discarding alternatives."""
        return self.annotations(
            iri, predicates=(str(RDFS.label), str(SKOS.prefLabel)), budget=budget
        )

    def metadata(
        self,
        iri: str,
        *,
        predicates: Sequence[str] = (
            str(SKOS.definition),
            str(SKOS.altLabel),
            str(SKOS.hiddenLabel),
            str(SKOS.notation),
        ),
        budget: Budget | None = None,
    ) -> Selection[Annotation]:
        """Expose chosen definitions, synonyms and notation."""
        return self.annotations(iri, predicates=predicates, budget=budget)

    def preferred_label(
        self, iri: str, *, languages: Sequence[str] = ()
    ) -> LabelSelection:
        """Rank requested languages, then untagged, then other languages; keep ties."""
        for tag in languages:
            language_tag(tag)
        selected = self.labels(iri)
        preferences = tuple(tag.lower() for tag in languages)

        def rank(value: Annotation) -> tuple[int, str]:
            """Rank languages without inferring identity from lexical text."""
            tag = (value.language or "").lower()
            return (
                (preferences.index(tag), "")
                if tag in preferences
                else (len(preferences), "")
                if not tag
                else (len(preferences) + 1, tag)
            )

        best = min((rank(value) for value in selected.items), default=None)
        preferred = tuple(value for value in selected.items if rank(value) == best)
        return LabelSelection(preferred, selected.items, selected.report)

    def prefixes(self) -> dict[str, str]:
        """Export selected bindings, rejecting disagreement between source roles."""
        result: dict[str, str] = {}
        for _, graph in self._scopes:
            for prefix, namespace in graph.namespaces():
                if prefix in result and result[prefix] != str(namespace):
                    raise InputError("Ambiguous selected prefix", location=prefix)
                result[prefix] = str(namespace)
        return result

    def search(
        self,
        query: str,
        *,
        fields: Sequence[str] = (
            "iri",
            str(RDFS.label),
            str(SKOS.prefLabel),
            str(SKOS.altLabel),
            str(SKOS.notation),
        ),
        languages: Sequence[str] = (),
        exact: bool = False,
        casefold: bool = False,
    ) -> Selection[SearchMatch]:
        """Search bounded evidence, ranking exact identifiers before labels."""
        if not isinstance(query, str) or not query:
            raise InputError("Search requires nonempty text")
        for field in fields:
            if field != "iri":
                absolute_iri(field)
        for tag in languages:
            language_tag(tag)
        match_query = query.casefold() if casefold else query
        wanted_languages = {tag.lower() for tag in languages}
        matches: list[SearchMatch] = []
        budget = Budget(self.limits)
        issues = IssueCollector(budget)
        complete = True
        try:
            for term in sorted(self._terms.values(), key=lambda term: term.iri):
                budget.consume("work")
                if term.status != "declared":
                    continue

                def candidates(
                    selected: TermInfo,
                ) -> Iterator[tuple[str, str, str | None, str]]:
                    """Count scanned annotations, including excluded fields."""
                    if "iri" in fields:
                        for scope in selected.scopes:
                            yield "iri", selected.iri, None, scope
                    for edge in self._outgoing.get(URIRef(selected.iri), ()):
                        budget.consume("work")
                        if str(edge.predicate) in fields and isinstance(
                            edge.object, Literal
                        ):
                            yield (
                                str(edge.predicate),
                                str(edge.object),
                                edge.object.language,
                                edge.scope,
                            )

                for field, value, language, scope in candidates(term):
                    budget.consume("work")
                    if (
                        wanted_languages
                        and field != "iri"
                        and (language or "").lower() not in wanted_languages
                    ):
                        continue
                    compared = value.casefold() if casefold else value
                    if compared == match_query or (
                        not exact and match_query in compared
                    ):
                        budget.consume("states")
                        matches.append(
                            SearchMatch(
                                term,
                                field,
                                value,
                                language,
                                scope,
                                0
                                if field == "iri" and compared == match_query
                                else 1
                                if compared == match_query
                                else 2,
                            )
                        )
        except LimitExceeded as error:
            issues.exhausted(str(error), location="terms.search")
            complete = False
        matches.sort(
            key=lambda match: (
                match.rank,
                match.term.iri,
                match.field,
                match.language or "",
                match.value,
                match.scope,
            )
        )
        return Selection(
            tuple(matches),
            Report(
                "terms.search",
                len(matches),
                tuple(issues),
                allow_empty=True,
                complete=complete,
                requested=len(matches) if complete else len(matches) + 1,
                regime="asserted",
                families=("terms.search",),
            ),
        )

    def deprecation(
        self,
        iri: str | BNode,
        *,
        replacement_predicates: Sequence[str] = (),
        budget: Budget | None = None,
    ) -> Deprecation:
        """Inspect deprecation and every selected replacement hint."""
        selected = self.statements(
            iri if isinstance(iri, BNode) else URIRef(absolute_iri(iri)),
            predicates=(str(OWL.deprecated), str(RDF.type), *replacement_predicates),
            budget=budget,
        )
        assertions = tuple(
            edge
            for edge in selected.items
            if edge.predicate == OWL.deprecated
            or edge.predicate == RDF.type
            and edge.object in (OWL.DeprecatedClass, OWL.DeprecatedProperty)
        )
        states: set[bool] = set()
        unknown = False
        for edge in assertions:
            if edge.predicate == RDF.type:
                states.add(True)
            elif (
                isinstance(edge.object, Literal)
                and edge.object.datatype == XSD.boolean
                and str(edge.object) in ("true", "false", "1", "0")
            ):
                states.add(str(edge.object) in ("true", "1"))
            else:
                unknown = True
        state: Direction[
            "deprecated", "active", "absent", "conflicting", "unverified"
        ] = (
            "unverified"
            if unknown
            else "conflicting"
            if len(states) > 1
            else "deprecated"
            if True in states
            else "active"
            if states
            else "absent"
        )
        return Deprecation(
            state,
            assertions,
            tuple(
                edge
                for edge in selected.items
                if str(edge.predicate) in replacement_predicates
            ),
            selected.report,
        )

    def summary(self) -> Summary:
        """Summarise selected facts without inferred world counts."""
        kinds: dict[str, int] = {}
        for term in self._terms.values():
            for kind in term.kinds:
                kinds[kind] = kinds.get(kind, 0) + 1
        return Summary(
            tuple(scope for scope, _ in self._scopes),
            len(self._edges),
            sum(term.status == "declared" for term in self._terms.values()),
            sum(term.status == "referenced" for term in self._terms.values()),
            tuple(sorted(kinds.items())),
            tuple(
                sorted(
                    term.iri
                    for term in self._terms.values()
                    if str(OWL.Ontology) in term.kinds
                )
            ),
            tuple(
                sorted(
                    {
                        edge.object.language
                        for edge in self._edges
                        if isinstance(edge.object, Literal)
                        and edge.object.language is not None
                    }
                )
            ),
        )

    def graph(self) -> Graph:
        """Export an independent explicit union of this already-selected graph scope."""
        graph = Graph(bind_namespaces="none")
        for edge in self._edges:
            graph.add((edge.subject, edge.predicate, edge.object))
        bind_namespaces(graph, *(source for _, source in self._scopes))
        return copy_graph(
            graph, max_triples=self.limits.max_triples, standardize_apart=False
        )

    def describe(
        self, iri: str, *, replacement_predicates: Sequence[str] = ()
    ) -> Description:
        """Compose the public lookup, literal and relationship semantics."""
        budget = Budget(self.limits)
        relationships = self.relationships(iri, direction="both", budget=budget)
        labels = self.labels(iri, budget=budget)
        metadata = self.metadata(iri, budget=budget)
        deprecated = self.deprecation(
            iri, replacement_predicates=replacement_predicates, budget=budget
        )
        reports = (
            relationships.report,
            labels.report,
            metadata.report,
            deprecated.report,
        )
        return Description(
            self.lookup(iri),
            labels.items,
            metadata.items,
            relationships.items,
            deprecated,
            Report(
                "terms.describe",
                sum(report.checked for report in reports),
                tuple(issue for report in reports for issue in report.issues),
                allow_empty=True,
                complete=all(report.complete for report in reports),
                regime="asserted",
                families=("terms.describe",),
                children=reports,
            ),
        )


def inspect_vocabulary(
    vocabulary: Vocabulary | Graph,
    *,
    scopes: Sequence[str] = ("local", "imports"),
    limits: Limits | None = None,
) -> VocabularyView:
    """Prepare one owned view for many term queries without reparsing sources."""
    return VocabularyView(vocabulary, scopes=scopes, limits=limits)
