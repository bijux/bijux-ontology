"""Explicit bounded Data Cube integrity, with every normative constraint accounted."""

from collections.abc import Callable, Iterator
from itertools import islice
from typing import Literal as Choice
from typing import cast

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD
from rdflib.term import Node

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.literals import check_literal, datatype_relation
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_node, literal_datatype
from bijux_ontology.reports import Issue, Report
from bijux_ontology.skos.subsets.selection import select_concepts
from bijux_ontology.vocabulary.inspection import VocabularyView
from bijux_ontology.vocabulary.navigation import traverse
from bijux_ontology.vocabulary.sources.loading import load_vocabulary

from .inspection import CubeRead, Resource
from .normalization import QB, normalize
from .types import CubeCheck, CubeConstraintCoverage


def _boolean(value: Node) -> bool | None:
    """Interpret only the four actual XSD boolean lexical forms."""
    if isinstance(value, Literal) and value.datatype == XSD.boolean:
        if str(value) in ("true", "1"):
            return True
        if str(value) in ("false", "0"):
            return False
    return None


class _Checks:
    """One Cube operation reuses actual code lists and extracted coordinates."""

    def __init__(self, read: CubeRead, closed: frozenset[str]) -> None:
        self.read = read
        self.graph = read.graph
        self.budget = read.budget
        self.closed = closed
        self.issues = IssueCollector(self.budget)
        self.view: VocabularyView | None = None

    def finding(self, code: str, subject: Node, message: str) -> None:
        """Retain the actual located integrity violation under the shared allowance."""
        bound_node(subject, self.budget)
        self.issues.append(Issue("cube." + code, message, str(subject)))

    def unknown(self, subject: Node, message: str) -> None:
        """Unavailable scope or capabilities cannot count as completed conformance."""
        bound_node(subject, self.budget)
        self.issues.append(
            Issue(
                "cube.constraint_unverified", message, str(subject), kind="unverified"
            )
        )

    def accept(self, report: Report) -> None:
        """Qualify native child evidence before retaining any diagnostic references."""
        retain_report(report, self.budget, self.issues)

    def code_view(self) -> VocabularyView:
        """Prepare existing native vocabulary inspection once for all code lists."""
        if self.view is None:
            count = len(self.graph)
            self.budget.consume("states", 8 * count)
            self.budget.consume("bytes", 2048 * count)
            self.budget.consume("work", count * max(1, count.bit_length()))
            vocabulary = load_vocabulary(
                self.read.source, graph_name=self.read.graph_name, budget=self.budget
            )
            self.view = VocabularyView(
                vocabulary, scopes=("local",), budget=self.budget
            )
        return self.view

    def ic0(self) -> int:
        """Qualify the builtin literal/direct-range D-consistency profile only."""
        count = 0
        for subject, predicate, value in self.graph:
            self.budget.consume("work")
            if isinstance(value, Literal):
                count += 1
                datatype = literal_datatype(value)
                self.budget.consume("work", len(value) + 1)
                self.budget.consume("bytes", 256 + 4 * len(value))
                bound_node(subject, self.budget)
                if any(
                    not (
                        char in "\t\n\r"
                        or 0x20 <= ord(char) <= 0xD7FF
                        or 0xE000 <= ord(char) <= 0xFFFD
                        or 0x10000 <= ord(char) <= 0x10FFFF
                    )
                    for char in value
                ):
                    self.unknown(
                        subject,
                        "IC0 text is outside the supported XML-character profile",
                    )
                elif (
                    datatype not in (XSD.string, RDF.langString)
                    and value
                    and (value[0].isspace() or value[-1].isspace())
                ):
                    self.unknown(
                        subject,
                        "IC0 whitespace-facet interpretation is unsupported",
                    )
                else:
                    self.accept(
                        check_literal(value, location=str(subject), budget=self.budget)
                    )
            if predicate in (RDFS.subClassOf, RDFS.subPropertyOf, RDFS.domain):
                self.unknown(
                    subject,
                    "IC0 datatype-sensitive schema entailment is unsupported",
                )
            if predicate == RDF.type and (
                str(value).startswith(str(XSD))
                or value in (RDFS.Datatype, RDF.langString, RDF.XMLLiteral, RDF.HTML)
            ):
                self.unknown(
                    subject,
                    "IC0 resource datatype membership is unsupported",
                )
        for prop, _, required in self.graph.triples((None, RDFS.range, None)):
            self.budget.consume("work")
            if not isinstance(required, URIRef) or not (
                str(required).startswith(str(XSD))
                or required in (RDF.langString, RDF.XMLLiteral, RDF.HTML)
            ):
                continue
            for subject, _, value in self.graph.triples((None, prop, None)):
                self.budget.consume("work")
                count += 1
                if not isinstance(value, Literal):
                    self.unknown(
                        subject,
                        "IC0 resource range needs datatype interpretation",
                    )
                    continue
                relation = datatype_relation(
                    str(literal_datatype(value)),
                    str(required),
                    regime="rdf",
                    budget=self.budget,
                )
                if relation == "disjoint":
                    self.finding(
                        "ic0_datatype_conflict",
                        subject,
                        "Literal value is disjoint from its asserted datatype range",
                    )
                elif relation not in ("equal", "subset"):
                    self.unknown(
                        subject,
                        "IC0 overlapping datatype membership is unavailable",
                    )
        return max(1, count)

    def ic1(self) -> int:
        """Every actual normalized observation belongs to exactly one dataset."""
        for obs in self.read.observations:
            self.budget.consume("work")
            if len(obs.datasets) != 1:
                self.finding(
                    "ic1_dataset_cardinality",
                    obs.subject,
                    "Observation requires exactly one dataset",
                )
        return len(self.read.observations)

    def ic2(self) -> int:
        """Every dataset has exactly one actual structure link."""
        for data in self.read.datasets:
            if len(self.read.values(data, QB.structure)) != 1:
                self.finding(
                    "ic2_structure_cardinality",
                    data,
                    "Dataset requires exactly one DSD",
                )
        return len(self.read.datasets)

    def ic3(self) -> int:
        """Declared structures contain at least one actual declared measure."""
        for dsd in self.read.structures:
            if not self.read.properties(dsd.identifier, QB.MeasureProperty):
                self.finding(
                    "ic3_measure_undeclared",
                    dsd.identifier,
                    "DSD has no declared measure",
                )
        return len(self.read.structures)

    def ic4(self) -> int:
        """Each declared dimension has an explicitly supplied range."""
        dimensions = self.read.typed(QB.DimensionProperty)
        for dim in dimensions:
            if not self.read.values(dim, RDFS.range):
                self.finding(
                    "ic4_dimension_range_missing",
                    dim,
                    "Dimension has no declared range",
                )
        return len(dimensions)

    def ic5(self) -> int:
        """Concept-valued dimensions declare their code list."""
        count = 0
        for dim in self.read.typed(QB.DimensionProperty):
            if SKOS.Concept in self.read.values(dim, RDFS.range):
                count += 1
                if not self.read.values(dim, QB.codeList):
                    self.finding(
                        "ic5_code_list_missing",
                        dim,
                        "Concept dimension has no code list",
                    )
        return count

    def ic6(self) -> int:
        """Explicit optionality is reserved for attribute components."""
        count = 0
        for dsd in self.read.structures:
            for component in dsd.components:
                if any(_boolean(value) is False for value in component.required):
                    count += 1
                    for prop in component.properties:
                        if (prop, RDF.type, QB.AttributeProperty) not in self.graph:
                            self.finding(
                                "ic6_nonattribute_optional",
                                component.specification,
                                "Only attributes may be marked optional",
                            )
        return count

    def ic7(self) -> int:
        """Every actual SliceKey belongs to an explicitly declared DSD."""
        for key in self.read.keys:
            if not any(
                (dsd.identifier, QB.sliceKey, key) in self.graph
                and (dsd.identifier, RDF.type, QB.DataStructureDefinition) in self.graph
                for dsd in self.read.structures
            ):
                self.finding(
                    "ic7_slice_key_undeclared", key, "Slice key has no declared DSD"
                )
        return len(self.read.keys)

    def ic8(self) -> int:
        """Slice-key properties are components of every associated DSD."""
        count = 0
        for key in self.read.keys:
            for dsd in self.read.subjects(QB.sliceKey, key):
                declared = {
                    prop
                    for spec in self.read.values(dsd, QB.component)
                    for prop in self.read.values(spec, QB.componentProperty)
                }
                for prop in self.read.values(key, QB.componentProperty):
                    count += 1
                    if prop not in declared:
                        self.finding(
                            "ic8_slice_key_inconsistent",
                            key,
                            "Slice-key property is absent from its DSD",
                        )
        return count

    def ic9(self) -> int:
        """Every slice has exactly one supplied slice structure."""
        for slice_node in self.read.slices:
            if len(self.read.values(slice_node, QB.sliceStructure)) != 1:
                self.finding(
                    "ic9_slice_structure_cardinality",
                    slice_node,
                    "Slice requires exactly one structure",
                )
        return len(self.read.slices)

    def ic10(self) -> int:
        """Slices carry all properties fixed by their keys."""
        count = 0
        for slice_node, _, key in self.graph.triples((None, QB.sliceStructure, None)):
            self.budget.consume("work")
            for prop in self.read.values(key, QB.componentProperty):
                count += 1
                if not isinstance(prop, URIRef) or not self.read.values(
                    slice_node, prop
                ):
                    self.finding(
                        "ic10_slice_dimension_missing",
                        slice_node,
                        "Slice lacks a value fixed by its key",
                    )
        return count

    def obligations(self, kind: URIRef) -> Iterator[tuple[Node, URIRef]]:
        """Join actual observations to every associated declared component role."""
        for obs in self.read.observations:
            for data in obs.datasets:
                for dsd in self.read.values(data, QB.structure):
                    for prop in self.read.properties(dsd, kind):
                        self.budget.consume("work")
                        yield obs.subject, prop

    def ic11(self) -> int:
        """Every observation supplies each of its declared dimensions."""
        count = 0
        for obs, dim in self.obligations(QB.DimensionProperty):
            count += 1
            if not self.read.values(obs, dim):
                self.finding(
                    "ic11_dimension_missing",
                    obs,
                    "Observation lacks a required dimension",
                )
        return count

    def ic12(self) -> int:
        """Use actual scoped native composite keys with SPARQL value equality."""
        count = 0
        for data, dsd in self.read.data_structures():
            rows = self.read.coordinate_rows(data, dsd, self.issues)
            if rows.contract is None:
                self.unknown(
                    data, "IC12 requires at least one supported declared dimension"
                )
                continue
            for position, left in enumerate(rows.keys):
                for right in islice(rows.keys, position):
                    self.budget.consume("work")
                    count += 1
                    relation = rows.relation(left, right, self.budget, self.issues)
                    if relation == "equal":
                        self.finding(
                            "ic12_duplicate_coordinates",
                            right.record.subject,
                            "Observation duplicates another dimension coordinate",
                        )
                    elif relation != "different":
                        self.unknown(
                            right.record.subject,
                            "IC12 coordinate equality is unavailable",
                        )
        return count

    def ic13(self) -> int:
        """Required components retain source values at every observation."""
        count = 0
        for obs in self.read.observations:
            for data in obs.datasets:
                for dsd in self.read.values(data, QB.structure):
                    if not isinstance(dsd, URIRef | BNode):
                        continue
                    structure = self.read.by_dsd.get(dsd)
                    if structure is None:
                        continue
                    for component in structure.components:
                        if any(_boolean(value) is True for value in component.required):
                            for prop in component.properties:
                                count += 1
                                if not isinstance(prop, URIRef) or not self.read.values(
                                    obs.subject, prop
                                ):
                                    self.finding(
                                        "ic13_attribute_missing",
                                        obs.subject,
                                        "Observation lacks a required attribute",
                                    )
        return count

    def ic14(self) -> int:
        """Ordinary-measure cubes require every measure on each observation."""
        count = 0
        for data, dsd in self.read.data_structures():
            if QB.measureType in self.read.properties(dsd, QB.DimensionProperty):
                continue
            for obs in self.read.observations:
                if data not in obs.datasets:
                    continue
                for measure in self.read.properties(dsd, QB.MeasureProperty):
                    count += 1
                    if not self.read.values(obs.subject, measure):
                        self.finding(
                            "ic14_measure_missing",
                            obs.subject,
                            "Ordinary observation lacks a declared measure",
                        )
        return count

    def measure_observations(
        self,
    ) -> Iterator[tuple[Resource, Node, Node, tuple[Node, ...]]]:
        """Locate actual measure-dimension observations without choosing a value."""
        for data, dsd in self.read.data_structures():
            if QB.measureType in self.read.properties(dsd, QB.DimensionProperty):
                for obs in self.read.observations:
                    if data in obs.datasets:
                        yield (
                            data,
                            dsd,
                            obs.subject,
                            self.read.values(obs.subject, QB.measureType),
                        )

    def ic15(self) -> int:
        """The actual measureType selects the measure value that must be present."""
        count = 0
        for _, dsd, obs, measures in self.measure_observations():
            declared = self.read.properties(dsd, QB.MeasureProperty)
            if len(measures) != 1:
                self.unknown(obs, "IC15 needs one supplied measureType value")
            for measure in measures:
                count += 1
                if not isinstance(measure, URIRef) or measure not in declared:
                    self.unknown(
                        obs,
                        "IC15 measureType lacks a supplied measure declaration",
                    )
                if not isinstance(measure, URIRef) or not self.read.values(
                    obs, measure
                ):
                    self.finding(
                        "ic15_measure_value_missing",
                        obs,
                        "Measure-dimension observation lacks its selected measure",
                    )
        return count

    def ic16(self) -> int:
        """Measure-dimension observations cannot also supply another measure."""
        count = 0
        for _, dsd, obs, chosen in self.measure_observations():
            for measure in self.read.properties(dsd, QB.MeasureProperty):
                if self.read.values(obs, measure):
                    count += 1
                    if any(measure != actual for actual in chosen):
                        self.finding(
                            "ic16_multiple_measures",
                            obs,
                            "Observation supplies a different declared measure",
                        )
        return count

    def ic17(self) -> int:
        """Each non-measure coordinate group contains exactly all declared measures."""
        count = 0
        for data, dsd in self.read.data_structures():
            if QB.measureType not in self.read.properties(dsd, QB.DimensionProperty):
                continue
            rows = self.read.coordinate_rows(data, dsd, self.issues)
            if rows.contract is None:
                self.unknown(data, "IC17 coordinate extraction is unavailable")
                continue
            declared = set(self.read.properties(dsd, QB.MeasureProperty))
            for left in rows.keys:
                found: set[Node] = set()
                matched = 0
                uncertain = len(rows.keys) != rows.count
                for right in rows.keys:
                    self.budget.consume("work")
                    relation = rows.relation(
                        left, right, self.budget, self.issues, measures=True
                    )
                    if relation == "equal":
                        matched += 1
                        found.update(
                            self.read.values(right.record.subject, QB.measureType)
                        )
                    elif relation != "different":
                        uncertain = True
                count += 1
                if uncertain:
                    self.unknown(
                        left.record.subject,
                        "IC17 complete measure-coordinate grouping is unavailable",
                    )
                elif matched != len(declared) or found != declared:
                    self.finding(
                        "ic17_measure_group_incomplete",
                        left.record.subject,
                        "Coordinate group lacks exactly one observation per measure",
                    )
        return count

    def ic18(self) -> int:
        """Slice membership and observation dataset links agree exactly."""
        count = 0
        for data, _, slice_node in self.graph.triples((None, QB.slice, None)):
            self.budget.consume("work")
            for obs in self.read.values(slice_node, QB.observation):
                count += 1
                if data not in self.read.values(obs, QB.dataSet):
                    self.finding(
                        "ic18_slice_dataset_conflict",
                        obs,
                        "Slice observation lacks its containing dataset link",
                    )
        return count

    def code_obligations(self) -> Iterator[tuple[Node, Node, Node]]:
        """Keep actual dimension values, code-list resources and observation anchors."""
        for obs, dim in self.obligations(QB.DimensionProperty):
            for code_list in self.read.values(dim, QB.codeList):
                for value in self.read.values(obs, dim):
                    yield obs, code_list, value

    def ic19(self) -> int:
        """Preserve normative membership through existing concept selection."""
        count = 0
        cache: dict[Node, tuple[frozenset[Node], bool, bool]] = {}
        for obs, code_list, value in self.code_obligations():
            if code_list not in cache:
                self.budget.consume("states")
                self.budget.consume("bytes", 128)
                kinds = set(self.read.values(code_list, RDF.type))
                if QB.HierarchicalCodeList in kinds and not kinds & {
                    SKOS.ConceptScheme,
                    SKOS.Collection,
                    SKOS.OrderedCollection,
                }:
                    cache[code_list] = frozenset(), False, False
                    continue
                if (
                    not isinstance(code_list, URIRef)
                    or int(SKOS.ConceptScheme in kinds)
                    + int(bool(kinds & {SKOS.Collection, SKOS.OrderedCollection}))
                    != 1
                ):
                    self.unknown(
                        code_list,
                        "IC19 needs a named unambiguous scheme or collection",
                    )
                    cache[code_list] = frozenset(), False, True
                else:
                    scheme = SKOS.ConceptScheme in kinds
                    selection = select_concepts(
                        self.code_view(),
                        scheme=str(code_list) if scheme else None,
                        collection=None if scheme else str(code_list),
                        closed=str(code_list) in self.closed,
                        deprecation="allow",
                        budget=self.budget,
                    )
                    self.accept(selection.report)
                    qualified = selection.closed and selection.report.verified
                    if not qualified:
                        self.unknown(
                            code_list,
                            "IC19 supplied code-list membership is open or incomplete",
                        )
                    cache[code_list] = frozenset(selection.values), qualified, True
            values, qualified, applicable = cache[code_list]
            if not applicable:
                continue
            count += 1
            valid = value in values and (value, RDF.type, SKOS.Concept) in self.graph
            if (code_list, RDF.type, SKOS.ConceptScheme) in self.graph:
                valid &= (value, SKOS.inScheme, code_list) in self.graph
            if not valid:
                if qualified:
                    self.finding(
                        "ic19_code_not_member",
                        obs,
                        "Dimension value is outside its declared code list",
                    )
                else:
                    self.unknown(
                        obs,
                        "IC19 cannot establish complete code-list membership",
                    )
        return count

    def hierarchy(self, inverse: bool) -> int:
        """Reuse bounded traversal for forward or inverse hierarchies."""
        count = 0
        cache: dict[Node, tuple[frozenset[Node], bool] | None] = {}
        for obs, code_list, value in self.code_obligations():
            if (code_list, RDF.type, QB.HierarchicalCodeList) not in self.graph:
                continue
            parents = self.read.values(code_list, QB.parentChildProperty)
            is_inverse = len(parents) == 1 and isinstance(parents[0], BNode)
            if inverse != is_inverse:
                continue
            count += 1
            if code_list not in cache:
                self.budget.consume("states")
                self.budget.consume("bytes", 128)
                cache[code_list] = None
                if len(parents) != 1 or not isinstance(code_list, URIRef):
                    self.unknown(
                        code_list,
                        "Hierarchy needs one supported property and named list",
                    )
                    continue
                prop = parents[0]
                if inverse:
                    values = self.read.values(prop, OWL.inverseOf)
                    prop = values[0] if len(values) == 1 else prop
                roots = self.read.values(code_list, QB.hierarchyRoot)
                if (
                    not isinstance(prop, URIRef)
                    or not roots
                    or any(not isinstance(root, URIRef) for root in roots)
                ):
                    self.unknown(
                        code_list,
                        "Hierarchy needs named roots and a forward/inverse property",
                    )
                    continue
                reached: set[Node] = set()
                qualified = str(code_list) in self.closed
                for root in roots:
                    walk = traverse(
                        self.code_view(),
                        str(root),
                        predicates=(str(prop),),
                        direction="incoming" if inverse else "outgoing",
                        include_self=True,
                        budget=self.budget,
                    )
                    self.accept(walk.report)
                    qualified &= walk.report.complete
                    self.budget.consume("states", len(walk.items))
                    self.budget.consume("bytes", 64 * len(walk.items))
                    reached.update(URIRef(item.iri) for item in walk.items)
                cache[code_list] = frozenset(reached), qualified
                if not qualified:
                    self.unknown(
                        code_list,
                        "Supplied hierarchy is not declared closed and complete",
                    )
            result = cache[code_list]
            if result is not None and value not in result[0]:
                if result[1]:
                    self.finding(
                        "ic21_code_not_reachable"
                        if inverse
                        else "ic20_code_not_reachable",
                        obs,
                        "Dimension value is outside the declared code hierarchy",
                    )
                else:
                    self.unknown(obs, "Code hierarchy membership cannot be established")
        return count

    def ic20(self) -> int:
        """Check the supplied ordinary parent-to-child hierarchy interpretation."""
        return self.hierarchy(False)

    def ic21(self) -> int:
        """Check the supplied explicitly inverse parent-to-child interpretation."""
        return self.hierarchy(True)


def _profile(checks: _Checks) -> None:
    """Unknown inventories or DSD roles never pass through empty extraction."""
    read = checks.read
    if not read.datasets or not read.observations:
        checks.unknown(QB.DataSet, "Required Cube observation scope is empty")
    for data, dsd in read.data_structures():
        if (
            not isinstance(dsd, URIRef | BNode)
            or (dsd, RDF.type, QB.DataStructureDefinition) not in read.graph
        ):
            checks.unknown(
                data, "Dataset structure has no supplied typed DSD definition"
            )
    for structure in read.structures:
        if not structure.components:
            checks.unknown(
                structure.identifier, "DSD component inventory is unavailable"
            )
        for component in structure.components:
            if not isinstance(component.specification, URIRef | BNode):
                checks.unknown(
                    component.specification,
                    "DSD component reference has no supported resource specification",
                )
                continue
            if len(component.properties) != 1 or not isinstance(
                component.properties[0], URIRef
            ):
                checks.unknown(
                    component.specification,
                    "Component needs one supplied named property",
                )
            for prop in component.properties:
                kinds = [
                    kind
                    for kind in (
                        QB.DimensionProperty,
                        QB.MeasureProperty,
                        QB.AttributeProperty,
                    )
                    if (prop, RDF.type, kind) in read.graph
                ]
                if len(kinds) != 1:
                    checks.unknown(
                        component.specification,
                        "Component property role is absent or ambiguous",
                    )
            flags = {_boolean(value) for value in component.required}
            if None in flags or len(flags) > 1:
                checks.unknown(
                    component.specification,
                    "Component requiredness is malformed or conflicting",
                )
            if len(component.attachments) > 1 or any(
                value not in (QB.Observation, QB.DataSet, QB.Slice, QB.MeasureProperty)
                for value in component.attachments
            ):
                checks.unknown(
                    component.specification,
                    "Component attachment level is unsupported or conflicting",
                )


def check_cube(
    source: PreparedSource,
    *,
    graph_name: GraphSelection | None = None,
    union: bool = False,
    complete: bool = False,
    code_lists_complete: tuple[str, ...] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> CubeCheck:
    """Evaluate IC0–IC21 over one explicitly selected, owned physical RDF graph.

    complete declares the supplied dataset/observation inventory; it never fills
    missing DSDs or code-list definitions. Named code lists close independently.
    Normalization and ordered-collection membership affect only the owned view.
    Each constraint records actual applicability, method and execution coverage.
    Unsupported datatype/schema/comparison profiles retain required uncertainty.
    """
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("Cube checking requires Limits")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Cube checking requires Budget")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Cube limits conflict with the supplied Budget")
    if type(complete) is not bool or type(union) is not bool:
        raise InputError("Cube inventory and union choices must be booleans")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    normalized = None
    selected = None
    metadata = None
    read = None
    methods = tuple(
        "builtin-literals-and-direct-datatype-ranges"
        if index == 0
        else "native-scoped-key-and-sparql-value-comparison"
        if index in (12, 17)
        else "owned-supplied-code-list-membership"
        if index >= 19
        else "normalized-rdf-integrity"
        for index in range(22)
    )
    coverage = [
        CubeConstraintCoverage(f"IC-{index}", "not_executed", method, None)
        for index, method in enumerate(methods)
    ]
    checked = 0
    prior: frozenset[int] = frozenset()
    handoff = min(operation.limits.max_states, operation.limits.max_issues)
    try:
        operation.consume("states", 44 + len(operation._retained_issues))
        operation.consume(
            "bytes", 22 * 512 + 64 * len(operation._retained_issues) + 8 * handoff
        )
        prior = frozenset(operation._retained_issues)
        if type(source) is not PreparedSource:
            raise InputError("Cube checking requires an ordinary owned PreparedSource")
        metadata = SnapshotMetadata.capture(source, operation)
        if union or metadata.union:
            issues.append(
                Issue(
                    "cube.scope_unverified",
                    "Cube profile selects one physical graph",
                    kind="unverified",
                )
            )
        elif (
            metadata.literal_fidelity not in ("source_terms", "supplied_terms")
            or metadata.graph_fidelity != "complete"
        ):
            issues.append(
                Issue(
                    "cube.scope_unverified",
                    "Cube checking needs original literal and physical graph fidelity",
                    kind="unverified",
                )
            )
        else:
            if type(code_lists_complete) is not tuple:
                raise InputError(
                    "Cube code-list closure requires an immutable IRI tuple"
                )
            operation.consume("states", len(code_lists_complete))
            operation.consume("bytes", 64 * len(code_lists_complete))
            for iri in code_lists_complete:
                if type(iri) is not str:
                    raise InputError(
                        "Cube code-list closure requires plain IRI strings"
                    )
                bound_text(iri, operation)
                absolute_iri(iri)
            closed = frozenset(code_lists_complete)
            normalized, selected, metadata = normalize(
                source, graph_name, operation, issues
            )
            read = CubeRead(normalized, selected, operation)
            checks = _Checks(read, closed)
            _profile(checks)
            retain_report(
                Report("cube.profile", 1, tuple(checks.issues)), operation, issues
            )
            if not complete:
                issues.append(
                    Issue(
                        "cube.scope_unverified",
                        "Selected Cube inventory is not declared complete",
                        kind="unverified",
                    )
                )
            for index, method in enumerate(methods):
                checks.issues = IssueCollector(operation)
                procedure = cast(Callable[[], int], getattr(checks, f"ic{index}"))
                try:
                    count = procedure()
                except LimitExceeded:
                    issues.retain_completed(
                        Report(
                            "cube.paid_prefix",
                            1,
                            tuple(
                                issue
                                for issue in islice(
                                    operation._retained_issues.values(), handoff
                                )
                                if id(issue) not in prior
                            ),
                        )
                    )
                    raise
                report = Report(
                    f"cube.IC-{index}",
                    count,
                    tuple(checks.issues),
                    allow_empty=True,
                    requested=count,
                )
                status: Choice["complete", "not_applicable", "unverified"] = (
                    "unverified"
                    if not report.complete
                    else "complete"
                    if count
                    else "not_applicable"
                )
                retain_report(report, operation, issues)
                coverage[index] = CubeConstraintCoverage(
                    f"IC-{index}", status, method, report
                )
                checked += 1
            if not metadata.same_scope(SnapshotMetadata.capture(source, operation)):
                raise InputError("Cube source metadata changed during checking")
    except LimitExceeded as error:
        issues.retain_completed(
            Report(
                "cube.paid_prefix",
                1,
                tuple(
                    issue
                    for issue in islice(operation._retained_issues.values(), handoff)
                    if id(issue) not in prior
                ),
            )
        )
        issues.exhausted(str(error))
        if checked < 22:
            coverage[checked] = CubeConstraintCoverage(
                f"IC-{checked}", "exhausted", methods[checked], None
            )
    except InputError as error:
        try:
            issues.append(Issue("cube.input_error", str(error), kind="input_error"))
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return CubeCheck(
        Report(
            "data.cube",
            checked,
            tuple(issues),
            requested=22,
            complete=checked == 22
            and all(item.status in ("complete", "not_applicable") for item in coverage),
        ),
        normalized,
        () if read is None else read.structures,
        () if read is None else read.observations,
        tuple(coverage),
        () if read is None else read.datasets,
        metadata,
        selected,
    )
