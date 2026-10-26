"""Bounded supplied Cube structures and shared native coordinate extraction."""

from collections.abc import Iterator
from dataclasses import replace
from typing import cast

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import RDF, XSD
from rdflib.term import Node

from bijux_ontology.data.keys.evidence import compare_keys
from bijux_ontology.data.keys.extraction import _Key, _KeyExtraction
from bijux_ontology.data.keys.types import KeyRecord
from bijux_ontology.declarations.keys import KeyComponent, KeyContract
from bijux_ontology.execution.limits import Budget, IssueCollector
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.rdf.value_comparison.observations import ScalarComparisons
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.sources.loading import load_vocabulary

from .normalization import QB
from .types import CubeComponent, CubeObservation, CubeStructure

Resource = URIRef | BNode


def boolean_flag(value: Node) -> bool | None:
    """Interpret only the four actual XSD boolean lexical forms."""
    if isinstance(value, Literal) and value.datatype == XSD.boolean:
        if str(value) in ("true", "1"):
            return True
        if str(value) in ("false", "0"):
            return False
    return None


class CubeFacts:
    """One operation's typed graph observations, without another RDF index engine."""

    def __init__(
        self, source: PreparedSource, graph_name: GraphSelection | None, budget: Budget
    ) -> None:
        self.source = source
        self.graph_name = graph_name
        self.budget = budget
        self.comparisons = ScalarComparisons(budget)
        self.graph = source._read(graph_name, budget=budget)
        self._types: dict[URIRef, tuple[Resource, ...]] = {}
        self._properties: dict[tuple[Node, URIRef], tuple[URIRef, ...]] = {}
        self._code_view: VocabularyView | None = None
        self.datasets = self.typed(QB.DataSet)
        self.slices = self.typed(QB.Slice)
        self.keys = self.typed(QB.SliceKey)
        self.structures = self._structures()
        self.observations = self._observations()
        budget.consume("states", len(self.structures))
        budget.consume("bytes", 64 * len(self.structures))
        self.by_dsd = {dsd.identifier: dsd for dsd in self.structures}
        self.coordinates: dict[tuple[Resource, Node], Coordinates] = {}

    def values(self, subject: Node, predicate: URIRef) -> tuple[Node, ...]:
        """Capture actual RDF values with charges before retaining each reference."""
        result: list[Node] = []
        for value in self.graph.objects(subject, predicate):
            self.budget.consume("work")
            self.budget.consume("states")
            self.budget.consume("bytes", 16)
            bound_node(value, self.budget)
            result.append(value)
        return tuple(result)

    def subjects(self, predicate: URIRef, obj: Node) -> tuple[Node, ...]:
        """Capture inverse links in the same selected owned graph."""
        result: list[Node] = []
        for value in self.graph.subjects(predicate, obj):
            self.budget.consume("work")
            self.budget.consume("states")
            self.budget.consume("bytes", 16)
            bound_node(value, self.budget)
            result.append(value)
        return tuple(result)

    def typed(self, kind: URIRef) -> tuple[Resource, ...]:
        """Ordinary RDF subjects are resources, never inferred from IRI spelling."""
        if kind not in self._types:
            values = cast(tuple[Resource, ...], self.subjects(RDF.type, kind))
            self.budget.consume("states")
            self.budget.reserve_storage(128)
            self._types[kind] = values
        return self._types[kind]

    def properties(self, dsd: Node, kind: URIRef) -> tuple[URIRef, ...]:
        """Resolve declared component properties using their actual normalized type."""
        key = dsd, kind
        if key in self._properties:
            return self._properties[key]
        result: list[URIRef] = []
        for spec in self.values(dsd, QB.component):
            for prop in self.values(spec, QB.componentProperty):
                self.budget.consume("work", 1 + len(result))
                if (
                    isinstance(prop, URIRef)
                    and (prop, RDF.type, kind) in self.graph
                    and prop not in result
                ):
                    self.budget.consume("states")
                    self.budget.consume("bytes", 16)
                    result.append(prop)
        values = tuple(result)
        self.budget.consume("states")
        self.budget.reserve_storage(128)
        self._properties[key] = values
        return values

    def _structures(self) -> tuple[CubeStructure, ...]:
        """Retain declared and referenced DSDs, including incomplete definitions."""
        typed = self.typed(QB.DataStructureDefinition)
        self.budget.consume("bytes", 64 + 8 * len(typed))
        identifiers = list(typed)
        for data in self.datasets:
            for dsd in self.values(data, QB.structure):
                if isinstance(dsd, URIRef | BNode) and dsd not in identifiers:
                    self.budget.consume("states")
                    self.budget.consume("bytes", 16)
                    identifiers.append(dsd)
        result: list[CubeStructure] = []
        for dsd in identifiers:
            components: list[CubeComponent] = []
            for spec in self.values(dsd, QB.component):
                self.budget.consume("states")
                self.budget.consume("bytes", 128)
                components.append(
                    CubeComponent(
                        spec,
                        self.values(spec, QB.componentProperty),
                        self.values(spec, QB.componentRequired),
                        self.values(spec, QB.componentAttachment),
                    )
                )
            result.append(CubeStructure(dsd, tuple(components)))
        return tuple(result)

    def _observations(self) -> tuple[CubeObservation, ...]:
        """Expose actual predicates and terms without erasing inherited conflicts."""
        result: list[CubeObservation] = []
        for subject in self.typed(QB.Observation):
            predicates: set[URIRef] = set()
            for predicate in self.graph.predicates(subject):
                self.budget.consume("work")
                self.budget.consume("states")
                self.budget.consume("bytes", 64)
                predicates.add(cast(URIRef, predicate))
            self.budget.consume("bytes", 64 + 32 * len(predicates))
            result.append(
                CubeObservation(
                    subject,
                    self.values(subject, QB.dataSet),
                    tuple(
                        (predicate, self.values(subject, predicate))
                        for predicate in predicates
                    ),
                )
            )
        return tuple(result)

    def data_structures(self) -> Iterator[tuple[Resource, Node]]:
        """Yield actual dataset/structure links without silently selecting one."""
        for data in self.datasets:
            for dsd in self.values(data, QB.structure):
                yield data, dsd

    def coordinate_rows(
        self, data: Resource, dsd: Node, issues: IssueCollector
    ) -> "Coordinates":
        """Share one native extraction across coordinate and measure-group checks."""
        key = data, dsd
        if key not in self.coordinates:
            self.budget.consume("states")
            self.budget.consume("bytes", 128)
            self.coordinates[key] = Coordinates(self, data, dsd, issues)
        return self.coordinates[key]

    def observation_obligations(self, kind: URIRef) -> Iterator[tuple[Node, URIRef]]:
        """Join actual observations to every associated declared component role."""
        for obs in self.observations:
            for data in obs.datasets:
                for dsd in self.values(data, QB.structure):
                    for prop in self.properties(dsd, kind):
                        self.budget.consume("work")
                        yield obs.subject, prop

    def measure_observations(
        self,
    ) -> Iterator[tuple[Resource, Node, Node, tuple[Node, ...]]]:
        """Locate actual measure-dimension observations without choosing a value."""
        for data, dsd in self.data_structures():
            if QB.measureType in self.properties(dsd, QB.DimensionProperty):
                for obs in self.observations:
                    if data in obs.datasets:
                        yield (
                            data,
                            dsd,
                            obs.subject,
                            self.values(obs.subject, QB.measureType),
                        )

    def code_obligations(self) -> Iterator[tuple[Node, Node, Node]]:
        """Keep actual dimension values, code-list resources and observation anchors."""
        for obs, dim in self.observation_obligations(QB.DimensionProperty):
            for code_list in self.values(dim, QB.codeList):
                for value in self.values(obs, dim):
                    yield obs, code_list, value

    def code_view(self) -> VocabularyView:
        """Prepare existing native vocabulary inspection once for all code lists."""
        if self._code_view is None:
            count = len(self.graph)
            self.budget.consume("states", 8 * count)
            self.budget.consume("bytes", 2048 * count)
            self.budget.consume("work", count * max(1, count.bit_length()))
            vocabulary = load_vocabulary(
                self.source, graph_name=self.graph_name, budget=self.budget
            )
            self._code_view = VocabularyView(
                vocabulary, scopes=("local",), budget=self.budget
            )
        return self._code_view


class Coordinates:
    """Cube-only application of existing scoped key extraction and equality."""

    def __init__(
        self, read: CubeFacts, data: Resource, dsd: Node, issues: IssueCollector
    ) -> None:
        self.dimensions = read.properties(dsd, QB.DimensionProperty)
        components: list[KeyComponent] = []
        for dim in self.dimensions:
            semantic = False
            for obs in read.observations:
                read.budget.consume("work", 1 + len(obs.datasets))
                if data in obs.datasets:
                    for value in read.values(obs.subject, dim):
                        if isinstance(value, Literal) and value.datatype not in (
                            None,
                            XSD.string,
                            RDF.langString,
                        ):
                            semantic = True
            components.append(
                KeyComponent(dim, equality="sparql-1.1" if semantic else "rdf-term")
            )
        self.contract = (
            KeyContract(
                str(data),
                tuple(components),
                absence="unverified",
                multiplicity="unverified",
            )
            if components
            else None
        )
        records = []
        for obs in read.observations:
            read.budget.consume("work")
            if data in obs.datasets:
                read.budget.consume("states")
                read.budget.consume("bytes", 128)
                records.append(
                    KeyRecord(
                        str(obs.subject),
                        read.source,
                        obs.subject,
                        graph_name=read.graph_name,
                    )
                )
        self.comparisons = read.comparisons
        self.count = len(records)
        self.keys: tuple[_Key, ...] = ()
        if self.contract is not None:
            read.budget.consume("bytes", 64 + 16 * len(records))
            self.keys = tuple(
                _KeyExtraction(
                    records, self.contract, read.budget, issues, len(records)
                )
            )

    def measure_rows(
        self, budget: Budget
    ) -> tuple[KeyContract | None, tuple[_Key, ...]]:
        """Project the non-measure coordinates once for every IC17 obligation."""
        assert self.contract is not None
        positions = tuple(
            index for index, dim in enumerate(self.dimensions) if dim != QB.measureType
        )
        budget.consume("states", len(positions))
        budget.reserve_storage(64 + 16 * len(positions))
        if not positions:
            return None, self.keys
        budget.consume("states", len(positions) + len(self.keys))
        budget.reserve_storage(128 + 32 * len(positions))
        contract = replace(
            self.contract,
            components=tuple(self.contract.components[index] for index in positions),
        )
        projected = []
        for key in self.keys:
            budget.consume("work", len(positions))
            budget.consume("states", 2 * len(positions))
            budget.reserve_storage(128 + 32 * len(positions))
            projected.append(
                replace(
                    key,
                    values=tuple(key.values[index] for index in positions),
                    tokens=tuple(key.tokens[index] for index in positions),
                )
            )
        return contract, tuple(projected)

    def relation(
        self,
        left: _Key,
        right: _Key,
        budget: Budget,
        issues: IssueCollector,
        *,
        contract: KeyContract | None = None,
    ) -> str:
        """Use SPARQL-aware native key relations for the exact selected coordinates."""
        assert self.contract is not None
        relation, pending = compare_keys(
            left,
            right,
            self.contract if contract is None else contract,
            budget,
            comparisons=self.comparisons,
        )
        issues.extend(pending)
        return relation
