"""Finite exact accession catalogues over existing owned vocabulary indexes."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from rdflib import Literal, URIRef
from rdflib.namespace import OWL, RDFS

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.vocabulary.browsing.inspection import (
    VocabularyView,
    inspect_vocabulary,
)
from bijux_ontology.vocabulary.browsing.navigation import traverse
from bijux_ontology.vocabulary.browsing.results import Edge
from bijux_ontology.vocabulary.sources.types import source_role

from .types import SdrfOntology, SdrfTerm


def _fold(value: str, budget: Budget) -> str:
    """Match the consumer's actual lower(), preserving original identifiers."""
    bound_text(value, budget)
    budget.consume("bytes", 64 + 4 * len(value))
    return value.lower()


@dataclass(frozen=True)
class SdrfTerminology:
    """Immutable prepared lookup indexes with no network or global registration."""

    terms: tuple[SdrfTerm, ...]
    report: Report
    _catalogues: tuple[tuple[str, VocabularyView, bool], ...]
    _accessions: Mapping[str, tuple[SdrfTerm, ...]]
    _labels: Mapping[tuple[str, str], tuple[SdrfTerm, ...]]
    _indexed: bool = False

    def resolve(self, accession: str, budget: Budget) -> SdrfTerm | None:
        """Resolve only a unique original accession in the selected catalogue."""
        values = self._accessions.get(_fold(accession, budget), ())
        budget.consume("work", len(values) + 1)
        return values[0] if self._indexed and len(values) == 1 else None

    def search(
        self, term: str, ontology: str | None, budget: Budget
    ) -> tuple[SdrfTerm, ...]:
        """Search asserted labels/synonyms without inventing a primary label."""
        label = _fold(term, budget)
        result = []
        for name, _, _ in self._catalogues:
            budget.consume("work")
            if ontology is not None and _fold(name, budget) != _fold(ontology, budget):
                continue
            for candidate in self._labels.get((name, label), ()):
                budget.consume("work")
                if self.resolve(candidate.accession, budget) is candidate:
                    budget.consume("states")
                    result.append(candidate)
        return tuple(result)

    def under_parent(self, accession: str, parent: str, budget: Budget) -> bool | None:
        """Use the existing named asserted-subclass traversal and its uncertainty."""
        child, ancestor = self.resolve(accession, budget), self.resolve(parent, budget)
        if child is None or ancestor is None or child.ontology != ancestor.ontology:
            return None
        for name, view, closed in self._catalogues:
            budget.consume("work")
            if name != child.ontology:
                continue
            if child.iri == ancestor.iri:
                return True
            selection = traverse(
                view, str(child.iri), predicates=(str(RDFS.subClassOf),), budget=budget
            )
            for visit in selection.items:
                budget.consume("work")
                if visit.iri == str(ancestor.iri):
                    return True
            return (
                False
                if closed
                and selection.report.complete
                and selection.report.outcome in ("agreement", "not_applicable")
                else None
            )
        return None

    def closed(self, names: Sequence[str], budget: Budget) -> bool:
        """Absence is known only for explicitly complete selected catalogues."""
        selected = {_fold(name, budget) for name in names}
        known = set()
        complete = self.report.verified
        for name, _, closed in self._catalogues:
            budget.consume("work")
            key = _fold(name, budget)
            if not selected or key in selected:
                known.add(key)
                complete &= closed
        return complete and bool(known) and (not selected or known == selected)


def prepare_sdrf_terminology(
    ontologies: Sequence[SdrfOntology],
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> SdrfTerminology:
    """Compile explicit local RDF accession/lexical intent under one shared budget."""
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("SDRF limits conflict with the shared budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    terms: list[SdrfTerm] = []
    catalogues: list[tuple[str, VocabularyView, bool]] = []
    accessions: dict[str, list[SdrfTerm]] = {}
    labels: dict[tuple[str, str], list[SdrfTerm]] = {}
    checked = 0
    requested = 1
    indexed = False
    try:
        selected = capture_items(ontologies, SdrfOntology, operation)
        requested = max(1, len(selected))
        names: set[str] = set()
        for source in selected:
            name = _fold(source.name, operation)
            if name in names:
                raise InputError("SDRF ontology names must be unique")
            names.add(name)
            count = 1 + len(source.label_predicates) + len(source.synonym_predicates)
            operation.consume("states", count)
            operation.consume("bytes", 64 + 24 * count)
            for predicate in (
                source.accession_predicate,
                *source.label_predicates,
                *source.synonym_predicates,
            ):
                bound_node(predicate, operation)
            view = (
                source.vocabulary
                if isinstance(source.vocabulary, VocabularyView)
                else inspect_vocabulary(source.vocabulary, budget=operation)
            )
            selected_edges = view.all_statements(budget=operation)
            retain_report(selected_edges.report, operation, issues)
            operation.consume("states")
            identifiers: dict[URIRef, list[Edge]] = {}
            lexical: dict[URIRef, list[Edge]] = {}
            predicates = frozenset(
                (*source.label_predicates, *source.synonym_predicates)
            )
            has_imports = False
            lookup_imports = False
            for edge in selected_edges.items:
                operation.consume("work")
                for node in (edge.subject, edge.predicate, edge.object):
                    bound_node(node, operation)
                if edge.predicate == OWL.imports:
                    has_imports = True
                    lookup_imports |= source_role(edge.scope) == "lookup"
                if not isinstance(edge.subject, URIRef):
                    continue
                if edge.predicate == source.accession_predicate:
                    if not isinstance(edge.object, Literal) or edge.object.language:
                        issues.append(
                            Issue(
                                "sdrf.unverified",
                                "Accessions require explicit untagged literals",
                                kind="unverified",
                            )
                        )
                        continue
                    operation.consume("states")
                    identifiers.setdefault(edge.subject, []).append(edge)
                if edge.predicate in predicates and isinstance(edge.object, Literal):
                    operation.consume("states")
                    lexical.setdefault(edge.subject, []).append(edge)
            imports_qualified = not has_imports or (
                not lookup_imports and view._vocabulary.required_imports_qualified
            )
            if not imports_qualified:
                issues.append(
                    Issue(
                        "sdrf.unverified",
                        "Selected required ontology imports have not been qualified",
                        kind="unverified",
                    )
                )
            catalogues.append((name, view, source.closed and imports_qualified))
            for iri, assertions in identifiers.items():
                operation.consume("work")
                spellings = {str(edge.object) for edge in assertions}
                if len(spellings) != 1:
                    issues.append(
                        Issue(
                            "sdrf.unverified",
                            "A term has conflicting accession identifiers",
                            kind="unverified",
                        )
                    )
                    continue
                accession = next(iter(spellings))
                key = _fold(accession, operation)
                edges = lexical.get(iri, [])
                operation.consume("states", len(edges) + len(assertions) + 1)
                values = tuple(dict.fromkeys(str(edge.object) for edge in edges))
                operation.consume("bytes", 256 + 32 * (len(edges) + len(assertions)))
                term = SdrfTerm(name, iri, accession, values, (*assertions, *edges))
                terms.append(term)
                accessions.setdefault(key, []).append(term)
                for value in values:
                    folded = _fold(value, operation)
                    operation.consume("states")
                    operation.consume("bytes", 128)
                    labels.setdefault((name, folded), []).append(term)
            checked += 1
        indexed = True
        if not terms:
            issues.append(
                Issue(
                    "sdrf.unverified",
                    "No explicit accession/lexical terminology was established",
                    kind="unverified",
                )
            )
    except LimitExceeded as error:
        issues.exhausted(str(error))
    return SdrfTerminology(
        tuple(terms),
        Report("sdrf.terminology", checked, tuple(issues), requested=requested),
        tuple(catalogues),
        MappingProxyType({key: tuple(value) for key, value in accessions.items()}),
        MappingProxyType({key: tuple(value) for key, value in labels.items()}),
        indexed,
    )
