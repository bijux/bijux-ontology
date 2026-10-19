"""Concrete read-only OAK BasicOntologyInterface over a selected Bijux view."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from types import MappingProxyType
from typing import Any, NoReturn

from oaklib.interfaces import BasicOntologyInterface  # type: ignore[import-untyped]
from rdflib import Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri, language_tag
from bijux_ontology.rdf.roles.classification import classify_statement
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.obo.reading import read_obo_records
from bijux_ontology.vocabulary.obo.subsets.reading import read_obo_subsets
from bijux_ontology.vocabulary.registry.types import IdentifierRegistry
from bijux_ontology.vocabulary.restrictions.projection import project_restrictions

from .types import OakOperationError


class BijuxBasicOntology(BasicOntologyInterface):  # type: ignore[misc]
    """OAK's simple lossy format, with explicit scope and mandatory completion.

    Relations mean named asserted subclass edges, directly authored existential
    projections, or supplied individual assertions. They are not OWL closure or
    invented direct RDF triples. The original view retains typed source facts.
    """

    _view: VocabularyView
    _registry: IdentifierRegistry
    _language: str | None
    _closed_subsets: bool
    _limits: Limits
    _shared_budget: Budget | None
    _owned_prefixes: Mapping[str, str]

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Require the bounded optional factory to bind an owned selected view."""
        raise InputError("Use as_oak to prepare an OAK provider")

    @classmethod
    def _from_owned(
        cls,
        view: VocabularyView,
        registry: IdentifierRegistry,
        language: str | None,
        closed_subsets: bool,
        limits: Limits,
        budget: Budget | None,
    ) -> BijuxBasicOntology:
        """Adopt only immutable view/registry handles qualified by the factory."""
        result = object.__new__(cls)
        result._owned_prefixes = MappingProxyType(dict(registry.bindings))
        BasicOntologyInterface.__init__(
            result,
            strict=True,
            auto_relax_axioms=False,
            cache_lookups=False,
            _prefix_map=result._owned_prefixes,
        )
        result._view, result._registry = view, registry
        result._closed_subsets = closed_subsets
        result._language, result._limits, result._shared_budget = (
            language,
            limits,
            budget,
        )
        return result

    @property
    def view(self) -> VocabularyView:
        """Keep exact scoped assertions and qualified OBO/axiom evidence available."""
        return self._view

    @contextmanager
    def _operation(self) -> Iterator[Budget]:
        """One finite native call owns one allowance, including its whole batch."""
        budget = (
            self._shared_budget
            if self._shared_budget is not None
            else Budget(self._limits)
        )
        try:
            yield budget
        except LimitExceeded as error:
            raise OakOperationError(
                Report(
                    "adapter.oak_operation",
                    0,
                    (Issue("execution.limit_exceeded", str(error), kind="exhausted"),),
                    requested=1,
                    complete=False,
                )
            ) from error

    def _unsupported(self, message: str, budget: Budget | None = None) -> NoReturn:
        """Refuse operations whose semantics the supplied view does not establish."""
        if budget is None:
            with self._operation() as owned:
                self._unsupported(message, owned)
        assert budget is not None
        issues = IssueCollector(budget)
        try:
            issues.append(Issue("adapter.oak_unsupported", message, kind="unverified"))
        except LimitExceeded:
            issues.exhausted("Stopped retaining the unsupported OAK operation")
        raise OakOperationError(
            Report(
                "adapter.oak_operation", 0, tuple(issues), requested=1, complete=False
            )
        )

    @staticmethod
    def _complete(report: Report) -> None:
        """Do not hide findings, missing coverage or an exhausted result prefix."""
        if not report.verified and not (
            report.outcome == "not_applicable"
            and report.complete
            and report.requested == 0
            and report.checked == 0
            and not report.issues
        ):
            raise OakOperationError(report)

    def _capture(self, values: Iterable[str], budget: Budget) -> tuple[str, ...]:
        """Bound arbitrary consumer iterables before publishing any result."""
        if isinstance(values, str | bytes):
            raise InputError("OAK batches require an identifier iterable")
        result = []
        for value in values:
            budget.consume("work")
            budget.consume("states")
            if type(value) is not str:
                raise InputError("OAK identifiers require ordinary text")
            bound_text(value, budget)
            result.append(value)
        return tuple(result)

    def _iri(self, value: str, budget: Budget) -> str:
        """Use only supplied namespace bindings; exact IRIs remain exact."""
        if type(value) is not str:
            raise InputError("OAK identifiers require ordinary text")
        bound_text(value, budget)
        for prefix, namespace in self._registry.bindings:
            bound_text(prefix, budget)
            bound_text(namespace, budget)
        budget.consume("states", len(self._registry.bindings))
        prefix = value.partition(":")[0]
        if any(prefix == item[0] for item in self._registry.bindings):
            budget.consume(
                "bytes",
                4
                * (len(value) + max(len(item[1]) for item in self._registry.bindings)),
            )
            expanded = self._registry.expand_curie(value)
            bound_text(expanded, budget)
            return expanded
        return absolute_iri(value)

    def _identifier(self, iri: str, budget: Budget) -> str:
        """Preserve reversible canonical spelling without a global OAK prefix map."""
        bound_text(iri, budget)
        maximum = 0
        for prefix, namespace in self._registry.bindings:
            bound_text(prefix, budget)
            bound_text(namespace, budget)
            maximum = max(maximum, len(prefix))
        budget.consume("bytes", 4 * (len(iri) + maximum + 1))
        budget.consume("states", len(self._registry.bindings))
        value = self._registry.compact_iri(iri)
        bound_text(value, budget)
        if value != iri and self._registry.expand_curie(value) != iri:
            self._unsupported(
                "The explicit identifier mapping is not reversible", budget
            )
        return value

    def _term(self, curie: str, budget: Budget) -> str:
        """Distinguish unknown entities from known entities lacking annotations."""
        iri = self._iri(curie, budget)
        if self._view.lookup(iri).status == "absent":
            raise KeyError(curie)
        return iri

    def _lang(self, requested: str | None, budget: Budget) -> str | None:
        """Select one exact tag; constructor policy never changes untagged data."""
        language = self._language if requested is None else requested
        if language is not None:
            if type(language) is not str:
                raise InputError("OAK language requests require text")
            bound_text(language, budget)
            language_tag(language)
            budget.consume("bytes", len(language))
            return language.lower()
        return None

    @staticmethod
    def _tag(value: str | None, budget: Budget) -> str | None:
        """Bound an original source tag before exact tag comparison."""
        if value is None:
            return None
        bound_text(value, budget)
        language_tag(value)
        budget.consume("bytes", len(value))
        return value.lower()

    def _label(self, curie: str, lang: str | None, budget: Budget) -> str | None:
        """Keep an ambiguous scalar label unknown instead of picking a value."""
        iri, language = self._term(curie, budget), self._lang(lang, budget)
        selected = self._view.labels(iri, budget=budget)
        self._complete(selected.report)
        values = set()
        for item in selected.items:
            bound_text(item.value, budget)
            if self._tag(item.language, budget) == language:
                budget.consume("states")
                values.add(item.value)
        if len(values) > 1:
            self._unsupported(
                "Multiple selected labels cannot supply one OAK label", budget
            )
        return next(iter(values), None)

    def label(self, curie: str, lang: str | None = None) -> str | None:
        """Return a unique exact-language label for an existing source identifier."""
        with self._operation() as budget:
            return self._label(curie, lang, budget)

    def labels(
        self, curies: Iterable[str], allow_none: bool = True, lang: str | None = None
    ) -> Iterable[tuple[str, str | None]]:
        """Honor language and finish one whole finite batch before publishing it."""
        if type(allow_none) is not bool:
            raise InputError("OAK allow_none must be boolean")
        with self._operation() as budget:
            result = []
            for curie in self._capture(curies, budget):
                value = self._label(curie, lang, budget)
                if value is not None or allow_none:
                    budget.consume("states")
                    result.append((curie, value))
            return tuple(result)

    def _alias_map(self, curie: str, budget: Budget) -> dict[str, list[str]]:
        """Expose actual predicates, preserving equal text across synonym roles."""
        iri = self._term(curie, budget)
        selected = self._view.annotations(
            iri,
            predicates=(str(RDFS.label), str(SKOS.prefLabel), str(SKOS.altLabel)),
            budget=budget,
        )
        self._complete(selected.report)
        records = read_obo_records(self._view, iri, budget=budget)
        self._complete(records.report)
        result: dict[str, list[str]] = {}
        for item in selected.items:
            key = self._identifier(item.predicate, budget)
            bound_text(item.value, budget)
            budget.consume("states")
            result.setdefault(key, []).append(item.value)
        for record in records.records:
            if record.role == "definition":
                continue
            if record.literal is None:
                self._unsupported(
                    "A malformed synonym cannot supply an OAK alias", budget
                )
            key = self._identifier(str(record.assertion.predicate), budget)
            value = str(record.literal)
            bound_text(value, budget)
            budget.consume("states")
            result.setdefault(key, []).append(value)
        return result

    def entity_alias_map(self, curie: str) -> dict[str, list[str]]:
        """Keep native synonym scopes keyed by their actual source predicates."""
        with self._operation() as budget:
            return self._alias_map(curie, budget)

    def entity_aliases(self, curie: str) -> list[str]:
        """Supply OAK's documented lossy textual union without changing the view."""
        with self._operation() as budget:
            result = set()
            for values in self._alias_map(curie, budget).values():
                for value in values:
                    budget.consume("work", len(value) + 1)
                    budget.consume("states")
                    result.add(value)
            budget.consume("states", len(result))
            return list(result)

    def _definition(
        self, curie: str, lang: str | None, include_metadata: bool, budget: Budget
    ) -> tuple[str | None, dict[str, list[str]]]:
        """Keep scalar text and representable annotation attachment separate."""
        iri, language = self._term(curie, budget), self._lang(lang, budget)
        selected = read_obo_records(self._view, iri, budget=budget)
        self._complete(selected.report)
        generic = self._view.annotations(
            iri, predicates=(str(SKOS.definition),), budget=budget
        )
        self._complete(generic.report)
        records = []
        for item in selected.records:
            budget.consume("work")
            if (
                item.role == "definition"
                and self._tag(item.language, budget) == language
            ):
                budget.consume("states")
                records.append(item)
        values = set()
        for item in records:
            if item.literal is None:
                self._unsupported(
                    "A malformed definition cannot supply OAK text", budget
                )
            bound_text(str(item.literal), budget)
            budget.consume("states")
            values.add(str(item.literal))
        generic_count = 0
        for annotation in generic.items:
            bound_text(annotation.value, budget)
            if self._tag(annotation.language, budget) == language:
                budget.consume("states")
                values.add(annotation.value)
                generic_count += 1
        if len(values) > 1:
            self._unsupported(
                "Multiple selected definitions cannot supply one scalar", budget
            )
        definition = next(iter(values), None)
        metadata: dict[str, list[str]] = {}
        if include_metadata and definition is not None and records:
            if len(records) != 1 or len(records[0].attachments) > 1 or generic_count:
                self._unsupported(
                    "OAK metadata cannot merge distinct definition attachments", budget
                )
            for attachment in records[0].attachments:
                for edge in attachment.qualifiers:
                    if not isinstance(edge.object, Literal | URIRef):
                        self._unsupported(
                            "Anonymous definition metadata is not representable", budget
                        )
                    key = self._identifier(str(edge.predicate), budget)
                    value = str(edge.object)
                    bound_text(value, budget)
                    budget.consume("states")
                    metadata.setdefault(key, []).append(value)
        return definition, metadata

    def definition(self, curie: str, lang: str | None = None) -> str | None:
        """Return exact-language supported OBO/SKOS text without arbitrary choice."""
        with self._operation() as budget:
            return self._definition(curie, lang, False, budget)[0]

    def definitions(
        self,
        curies: Iterable[str],
        include_metadata: bool = False,
        include_missing: bool = False,
        lang: str | None = None,
    ) -> Iterator[tuple[str, str | None, dict[str, list[str]]]]:
        """Keep native batch language and annotation coverage on one budget."""
        if type(include_metadata) is not bool or type(include_missing) is not bool:
            raise InputError("OAK definition flags must be booleans")
        with self._operation() as budget:
            result = []
            for curie in self._capture(curies, budget):
                value, metadata = self._definition(
                    curie, lang, include_metadata, budget
                )
                if value is not None or include_missing:
                    budget.consume("states")
                    result.append((curie, value, metadata))
            return iter(result)

    def _deprecated(self, iri: str, budget: Budget) -> bool:
        """Absence of an obsolete annotation is not conflicting annotation input."""
        bound_text(iri, budget)
        selected = self._view.deprecation(iri, budget=budget)
        self._complete(selected.report)
        if selected.state in {"unverified", "conflicting"}:
            self._unsupported(
                "Conflicting or malformed obsolescence is not active", budget
            )
        return selected.state == "deprecated"

    def entities(
        self, filter_obsoletes: bool = True, owl_type: str | None = None
    ) -> Iterable[str]:
        """Enumerate selected named declarations and actual supplied OWL kinds."""
        if type(filter_obsoletes) is not bool:
            raise InputError("OAK obsolete filtering must be boolean")
        with self._operation() as budget:
            kind = self._iri(owl_type, budget) if owl_type is not None else None
            selected = self._view.terms(budget=budget)
            self._complete(selected.report)
            result = []
            for term in selected.items:
                budget.consume("work")
                if self.exclude_owl_top_and_bottom and term.iri in (
                    str(OWL.Thing),
                    str(OWL.Nothing),
                ):
                    continue
                if kind is not None and kind not in term.kinds:
                    continue
                if filter_obsoletes and self._deprecated(term.iri, budget):
                    continue
                budget.consume("states")
                result.append(self._identifier(term.iri, budget))
            return tuple(result)

    def owl_types(self, entities: Iterable[str]) -> Iterable[tuple[str, str]]:
        """Expose standard declaration markers, not application class membership."""
        with self._operation() as budget:
            result = []
            for curie in self._capture(entities, budget):
                selected = self._view.relationships(
                    self._term(curie, budget),
                    predicates=(str(RDF.type),),
                    budget=budget,
                )
                self._complete(selected.report)
                for edge in selected.items:
                    occurrences = classify_statement(
                        (edge.subject, edge.predicate, edge.object),
                        source_scope=edge.scope,
                        budget=budget,
                    )
                    if isinstance(edge.object, URIRef) and any(
                        item.position == "object" and item.role == "declaration"
                        for item in occurrences
                    ):
                        budget.consume("states")
                        result.append(
                            (curie, self._identifier(str(edge.object), budget))
                        )
            return tuple(result)

    def obsoletes(self, include_merged: bool = True) -> Iterable[str]:
        """Return actual deprecated entities; merged-term filtering needs policy."""
        if type(include_merged) is not bool:
            raise InputError("OAK merged-term flag must be boolean")
        if not include_merged:
            self._unsupported("Merged-term interpretation is not supplied")
        with self._operation() as budget:
            selected = self._view.terms(budget=budget)
            self._complete(selected.report)
            return tuple(
                self._identifier(term.iri, budget)
                for term in selected.items
                if self._deprecated(term.iri, budget)
            )

    def subsets(self) -> Iterable[str]:
        """Enumerate native OBO subset definitions, without SKOS reinterpretation."""
        with self._operation() as budget:
            selected = read_obo_subsets(
                self._view, closed=self._closed_subsets, budget=budget
            )
            self._complete(selected.report)
            return tuple(
                self._identifier(str(item.iri), budget) for item in selected.definitions
            )

    def subset_members(self, subset: str) -> Iterable[str]:
        """Return a complete selected observed inventory, not an OWL closure claim."""
        with self._operation() as budget:
            selected = read_obo_subsets(
                self._view, closed=self._closed_subsets, budget=budget
            )
            self._complete(selected.report)
            values = selected.select(self._iri(subset, budget), budget=budget)
            assert values.report is not None
            self._complete(values.report)
            return tuple(
                self._identifier(str(value), budget) for value in values.values
            )

    def terms_subsets(self, curies: Iterable[str]) -> Iterable[tuple[str, str]]:
        """Bound the entire consumer batch and preserve exact membership scope."""
        with self._operation() as budget:
            requested = {
                self._term(value, budget): value
                for value in self._capture(curies, budget)
            }
            selected = read_obo_subsets(
                self._view, closed=self._closed_subsets, budget=budget
            )
            self._complete(selected.report)
            result = []
            for edge in selected.memberships:
                budget.consume("work")
                if str(edge.subject) in requested:
                    budget.consume("states")
                    result.append(
                        (
                            requested[str(edge.subject)],
                            self._identifier(str(edge.object), budget),
                        )
                    )
            return tuple(result)

    def relationships(
        self,
        subjects: Iterable[str] | None = None,
        predicates: Iterable[str] | None = None,
        objects: Iterable[str] | None = None,
        include_tbox: bool = True,
        include_abox: bool = True,
        include_entailed: bool = False,
        exclude_blank: bool = True,
        invert: bool = False,
    ) -> Iterator[tuple[str, str, str]]:
        """Complete one finite native relationship operation before publishing rows."""
        with self._operation() as budget:
            return iter(
                self._relationships(
                    budget,
                    subjects,
                    predicates,
                    objects,
                    include_tbox,
                    include_abox,
                    include_entailed,
                    exclude_blank,
                    invert,
                )
            )

    def _relationships(
        self,
        budget: Budget,
        subjects: Iterable[str] | None = None,
        predicates: Iterable[str] | None = None,
        objects: Iterable[str] | None = None,
        include_tbox: bool = True,
        include_abox: bool = True,
        include_entailed: bool = False,
        exclude_blank: bool = True,
        invert: bool = False,
    ) -> list[tuple[str, str, str]]:
        """Publish only complete asserted named edges and authored existential facts."""
        if any(
            type(flag) is not bool
            for flag in (
                include_tbox,
                include_abox,
                include_entailed,
                exclude_blank,
                invert,
            )
        ):
            raise InputError("OAK relationship flags must be booleans")
        if include_entailed or not exclude_blank or self.auto_relax_axioms:
            self._unsupported(
                "Inferred, relaxed or anonymous OAK relationships are not supplied",
                budget,
            )
        selected_subjects = (
            None
            if subjects is None
            else {self._iri(value, budget) for value in self._capture(subjects, budget)}
        )
        selected_objects = (
            None
            if objects is None
            else {self._iri(value, budget) for value in self._capture(objects, budget)}
        )
        selected_predicates = (
            None
            if predicates is None
            else {
                self._iri(value, budget) for value in self._capture(predicates, budget)
            }
        )
        if invert:
            selected_subjects, selected_objects = (
                selected_objects,
                selected_subjects,
            )
        selected = self._view.all_statements(budget=budget)
        self._complete(selected.report)
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
                selected_subjects is not None
                and str(subject) not in selected_subjects
                or selected_objects is not None
                and str(obj) not in selected_objects
                or selected_predicates is not None
                and str(predicate) not in selected_predicates
            ):
                return
            budget.consume("states")
            triples.add((str(subject), str(predicate), str(obj)))

        for edge in selected.items:
            budget.consume("work")
            if not isinstance(edge.subject, URIRef) or not isinstance(
                edge.object, URIRef
            ):
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
        if include_tbox and (
            selected_predicates is None or selected_predicates - {str(RDFS.subClassOf)}
        ):
            for owner in owners:
                budget.consume("work")
                bound_node(owner, budget)
                if (
                    selected_subjects is not None
                    and str(owner) not in selected_subjects
                ):
                    continue
                projections = project_restrictions(
                    self._view, str(owner), inherited=False, budget=budget
                )
                self._complete(projections.report)
                for item in projections.items:
                    budget.consume("work")
                    if (
                        item.status == "supported"
                        and item.quantifier == OWL.someValuesFrom
                        and isinstance(item.property, URIRef)
                        and isinstance(item.filler, URIRef)
                    ):
                        retain(item.authored_owner, item.property, item.filler)
        result = []
        for subject, predicate, obj in triples:
            row = (
                self._identifier(subject, budget),
                self._identifier(predicate, budget),
                self._identifier(obj, budget),
            )
            budget.consume("states")
            result.append((row[2], row[1], row[0]) if invert else row)
        return result

    def outgoing_relationship_map(self, curie: str) -> dict[str, list[str]]:
        """Use the same qualified native relation operation for outgoing maps."""
        with self._operation() as budget:
            result: dict[str, list[str]] = {}
            for _, predicate, obj in self._relationships(budget, subjects=(curie,)):
                budget.consume("states", 2)
                result.setdefault(predicate, []).append(obj)
            return result

    def incoming_relationship_map(self, curie: str) -> dict[str, list[str]]:
        """Use the same qualified native relation operation for incoming maps."""
        with self._operation() as budget:
            result: dict[str, list[str]] = {}
            for subject, predicate, _ in self._relationships(budget, objects=(curie,)):
                budget.consume("states", 2)
                result.setdefault(predicate, []).append(subject)
            return result

    def prefix_map(self) -> Mapping[str, str]:
        """Never fall back to OAK's ambient global prefix catalogue."""
        return self._owned_prefixes

    def curie_to_uri(self, curie: str, strict: bool = False) -> str | None:
        """Expand only the caller's explicit registered CURIE spelling."""
        if type(strict) is not bool:
            raise InputError("OAK identifier strictness must be boolean")
        with self._operation() as budget:
            if type(curie) is not str:
                raise InputError("OAK identifiers require text")
            bound_text(curie, budget)
            if curie.partition(":")[0] not in self._owned_prefixes:
                if strict:
                    raise InputError("Unknown explicitly registered OAK prefix")
                return None
            return self._iri(curie, budget)

    def uri_to_curie(
        self, uri: str, strict: bool = True, use_uri_fallback: bool = False
    ) -> str | None:
        """Contract with the existing canonical registry and explicit URI fallback."""
        if type(strict) is not bool or type(use_uri_fallback) is not bool:
            raise InputError("OAK identifier flags must be booleans")
        with self._operation() as budget:
            value = self._identifier(uri, budget)
            if value != uri or use_uri_fallback:
                return value
            if strict:
                raise InputError("No explicitly registered namespace matches the IRI")
            return None

    @property
    def entailed_edge_index(self) -> Any:
        """Reject inferred indexes at access rather than create an empty cache."""
        self._unsupported("No inferred edge index is supplied")

    def _all_entailed_relationships(self) -> NoReturn:
        """Keep native internal inference entry points inside the same boundary."""
        self._unsupported("No inferred relationship closure is supplied")

    def _all_relationships(self) -> Iterator[tuple[str, str, str]]:
        """Use only the ordinary complete asserted relation operation."""
        return self.relationships()

    def set_label(self, curie: str, label: str, lang: str | None = None) -> NoReturn:
        """Prepared source snapshots cannot be mutated by an OAK client."""
        self._unsupported("The prepared OAK provider is read-only")

    def create_entity(
        self,
        curie: str,
        label: str | None = None,
        relationships: dict[str, list[str]] | None = None,
        replace: bool = False,
        **kwargs: Any,
    ) -> NoReturn:
        """Reject entity creation without changing any source or identifier map."""
        self._unsupported("The prepared OAK provider is read-only")

    def delete_entity(
        self, curie: str, label: str | None = None, **kwargs: Any
    ) -> NoReturn:
        """Reject entity deletion without changing the selected vocabulary."""
        self._unsupported("The prepared OAK provider is read-only")

    def add_missing_property_values(
        self, curie: str, metadata_map: dict[str, list[str]]
    ) -> NoReturn:
        """Do not let native defaults manufacture undeclared OBO URL metadata."""
        self._unsupported("Metadata synthesis is not supplied")

    def save(self) -> NoReturn:
        """No persistence or mutation transaction exists on an immutable view."""
        self._unsupported("The prepared OAK provider is read-only")

    def clone(self, resource: Any) -> NoReturn:
        """No new source or remote resolver is created by the basic provider."""
        self._unsupported("The prepared OAK provider does not clone resources")

    def query(
        self,
        query: str,
        syntax: str | None = None,
        prefixes: list[str] | None = None,
        **kwargs: Any,
    ) -> NoReturn:
        """Reject an unsupported query language instead of returning empty rows."""
        self._unsupported("An OAK query language is not supplied")

    def set_metamodel_mappings(self, mappings: Any) -> NoReturn:
        """Changing predicate interpretation requires a new explicit provider."""
        self._unsupported("Metamodel rewriting is not supplied")

    def precompute_lookups(self) -> NoReturn:
        """Do not advertise a native precomputation that does no work."""
        self._unsupported("Additional OAK precomputation is not supplied")
