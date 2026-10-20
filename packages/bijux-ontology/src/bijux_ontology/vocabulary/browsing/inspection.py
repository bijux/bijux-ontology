"""Owned vocabulary snapshots for bounded exact-IRI inspection."""

from collections.abc import Iterable, Iterator, Mapping, Sequence
from functools import cmp_to_key, partial
from threading import Event
from typing import Literal as Direction
from typing import TypeVar, cast

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.owl.encoding.constructs import unsupported_constructs
from bijux_ontology.rdf.identifiers import absolute_iri, language_tag
from bijux_ontology.rdf.sources.snapshots import copy_graph
from bijux_ontology.vocabulary.browsing.results import (
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
from bijux_ontology.vocabulary.registry.prefixes import expand_curie
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.vocabulary.sources.view import (
    Vocabulary,
    _compare_edges,
    _lexical_candidates,
    _ordered_terms,
    _VocabularyIndex,
)

from .admission import _PageBudget, _text
from .matching import _Search

T = TypeVar("T")


class VocabularyView:
    """Inspect one owned scope snapshot; every multi-result operation is bounded."""

    def __init__(
        self,
        vocabulary: Vocabulary | Graph,
        *,
        scopes: Sequence[str] = ("local", "imports"),
        limits: Limits | None = None,
        budget: Budget | None = None,
    ) -> None:
        source = (
            vocabulary
            if isinstance(vocabulary, Vocabulary)
            else load_vocabulary(vocabulary, limits=limits)
        )
        self.limits = limits if limits is not None else source.limits
        self._reader = source._read(
            scopes, limits=self.limits, budget=budget, prepare_union=False
        )
        self._vocabulary = source
        self._scopes = self._reader.scopes
        self._roles = tuple(
            role for role in ("local", "imports", "lookup") if role in scopes
        )
        self._preparation_budget = budget

    def _operation_budget(self, budget: Budget | None) -> Budget:
        """Share an explicit operation allowance, including deferred preparation."""
        if budget is not None:
            return budget
        if self._preparation_budget is not None:
            return self._preparation_budget
        return Budget(self.limits)

    def _prepare_index(self, budget: Budget | None = None) -> _VocabularyIndex:
        """Request broad inspection only for operations that actually need it."""
        return self._vocabulary._inspect(self._reader, self._operation_budget(budget))

    @property
    def _index(self) -> _VocabularyIndex:
        """Expose the shared qualified derivative to internal complete readers."""
        return self._prepare_index()

    def _select(
        self, values: Iterable[T | None], family: str, *, budget: Budget | None = None
    ) -> Selection[T]:
        budget = self._operation_budget(budget)
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

    def lookup(self, iri: str, *, budget: Budget | None = None) -> TermInfo:
        """Distinguish supplied declaration, referenced-only identifier and absence."""
        selected = self._operation_budget(budget)
        _text(iri, selected)
        absolute_iri(iri)
        node = URIRef(iri)
        kinds: set[str] = set()
        declarations: list[str] = []
        references: list[str] = []
        for scope, graph in self._scopes:
            declared = False
            for kind in graph.objects(node, RDF.type):
                selected.consume("work")
                if isinstance(kind, URIRef):
                    selected.consume("states")
                    selected.consume("bytes", 128 + 4 * len(kind))
                    kinds.add(str(kind))
                    declared = True
            if declared:
                declarations.append(scope)
                continue
            selected.consume("work", 3)
            used = (
                next(graph.triples((node, None, None)), None) is not None
                or next(graph.triples((None, node, None)), None) is not None
                or next(graph.triples((None, None, node)), None) is not None
            )
            if not used:
                # RDF stores do not generally index literal datatype references.
                # Absence therefore needs a bounded scan of these positions.
                for value in graph.objects():
                    selected.consume("work")
                    if isinstance(value, Literal) and value.datatype == node:
                        used = True
                        break
            if used:
                references.append(scope)
        selected.consume("work", max(1, len(kinds) * max(1, len(kinds).bit_length())))
        return TermInfo(
            iri,
            "declared" if kinds else "referenced" if references else "absent",
            tuple(sorted(kinds)),
            tuple(sorted(declarations if kinds else references)),
        )

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
        selected = self._operation_budget(budget)

        def candidates() -> Iterator[TermInfo | None]:
            """Keep derivative preparation within the selection's failure boundary."""
            index = self._prepare_index(selected)
            for iri, term in sorted(index.terms.items()):
                yield (
                    term
                    if (
                        term.status == "declared"
                        and (not kinds or set(kinds).intersection(term.kinds))
                        and (not namespaces or iri.startswith(tuple(namespaces)))
                    )
                    else None
                )

        return self._select(
            candidates(),
            "terms.inventory",
            budget=selected,
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
        selected = self._operation_budget(budget)

        def candidates() -> Iterator[Edge]:
            """Build complete evidence only inside the bounded result operation."""
            yield from self._prepare_index(selected).edges

        return self._select(candidates(), "terms.statements", budget=selected)

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
        selected_predicates = frozenset(URIRef(predicate) for predicate in predicates)
        selected = self._operation_budget(budget)

        def candidates() -> Iterator[Edge]:
            """Order only adjacent selected assertions, retaining source duplicates."""
            outgoing: list[Edge] = []
            incoming: list[Edge] = []
            for scope, graph in self._scopes:
                patterns = (
                    ((node, None, None), outgoing),
                    ((None, None, node), incoming),
                )
                for pattern, values in patterns:
                    if (values is outgoing and direction == "incoming") or (
                        values is incoming and direction == "outgoing"
                    ):
                        continue
                    for subject, predicate, value in graph.triples(pattern):
                        selected.consume("work")
                        if (
                            values is incoming
                            and direction == "both"
                            and subject == node
                        ):
                            continue
                        if selected_predicates and predicate not in selected_predicates:
                            continue
                        size = (
                            len(cast(str, subject))
                            + len(cast(str, predicate))
                            + len(cast(str, value))
                            + len(scope)
                        )
                        if isinstance(value, Literal):
                            size += len(value.datatype or "") + len(
                                value.language or ""
                            )
                        selected.consume("states")
                        selected.consume("bytes", 512 + 24 * size)
                        values.append(
                            Edge(subject, cast(URIRef, predicate), value, scope)
                        )
            for values in (outgoing, incoming):
                keyed: list[tuple[tuple[str, str, str, str], Edge]] = []
                for edge in values:
                    selected.consume(
                        "work",
                        len(cast(str, edge.subject))
                        + len(edge.predicate)
                        + len(cast(str, edge.object))
                        + len(edge.scope),
                    )
                    keyed.append(
                        (
                            (
                                str(edge.subject),
                                str(edge.predicate),
                                edge.object.n3(),
                                edge.scope,
                            ),
                            edge,
                        )
                    )
                keyed.sort(key=cmp_to_key(partial(_compare_edges, budget=selected)))
                for _, edge in keyed:
                    yield edge

        return self._select(
            (
                edge
                if not selected_predicates or edge.predicate in selected_predicates
                else None
                for edge in candidates()
            ),
            "terms.relationships",
            budget=selected,
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
            unsupported_constructs(self._read(), URIRef(iri)),
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
        budget: Budget | None = None,
        cancel: Event | None = None,
    ) -> Selection[SearchMatch]:
        """Collect bounded exact/substring evidence in the shared ranked order."""
        if budget is not None and not isinstance(budget, Budget):
            raise InputError("Search budget requires Budget")
        if cancel is not None and not isinstance(cancel, Event):
            raise InputError("Search cancellation requires threading.Event")
        selected = self._operation_budget(budget)
        operation = _PageBudget(selected, cancel)
        issues = IssueCollector(selected)
        matches: list[SearchMatch] = []
        complete = False
        try:
            search = _Search.prepare(
                query, fields, languages, exact, casefold, operation
            )
            search = search.admit(operation)
            index = self._prepare_index(operation)
            terms = _ordered_terms(index, operation)
            candidates = _lexical_candidates(index, terms, operation)
            for phase in range(3):
                for candidate in candidates:
                    operation.consume("work")
                    match = search.match(candidate, phase, operation)
                    if match is not None:
                        operation.consume("states")
                        operation.consume("bytes", 16)
                        matches.append(match)
            complete = True
        except LimitExceeded as error:
            issues.exhausted(str(error), location="terms.search")
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
        index = self._index
        for term in index.terms.values():
            for kind in term.kinds:
                kinds[kind] = kinds.get(kind, 0) + 1
        return Summary(
            tuple(scope for scope, _ in self._scopes),
            len(index.edges),
            sum(term.status == "declared" for term in index.terms.values()),
            sum(term.status == "referenced" for term in index.terms.values()),
            tuple(sorted(kinds.items())),
            tuple(
                sorted(
                    term.iri
                    for term in index.terms.values()
                    if str(OWL.Ontology) in term.kinds
                )
            ),
            tuple(
                sorted(
                    {
                        edge.object.language
                        for edge in index.edges
                        if isinstance(edge.object, Literal)
                        and edge.object.language is not None
                    }
                )
            ),
        )

    def graph(self) -> Graph:
        """Export an independent explicit union of this already-selected graph scope."""
        return copy_graph(
            self._read(),
            max_triples=self.limits.max_triples,
            standardize_apart=False,
        )

    def _read(self, *, budget: Budget | None = None) -> Graph:
        """Read a selected union within the active operation allowance."""
        return self._vocabulary._read(
            self._roles, budget=self._operation_budget(budget), limits=self.limits
        ).graph

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
    budget: Budget | None = None,
) -> VocabularyView:
    """Prepare one owned view for many term queries without reparsing sources."""
    return VocabularyView(vocabulary, scopes=scopes, limits=limits, budget=budget)
