"""Finite integrity families over the same owned normalized Cube facts."""

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import RDF, RDFS, SKOS, XSD

from bijux_ontology.rdf.literals.checking import check_literal, datatype_relation
from bijux_ontology.rdf.terms import bound_node, literal_datatype

from ..facts import boolean_flag
from ..normalization import QB
from .progress import CubeIntegrity


def check_datatype_consistency(state: CubeIntegrity) -> int:
    """Qualify the builtin literal/direct-range D-consistency profile only."""
    count = 0
    for subject, predicate, value in state.graph:
        state.budget.consume("work")
        if isinstance(value, Literal):
            count += 1
            datatype = literal_datatype(value)
            state.budget.consume("work", len(value) + 1)
            state.budget.consume("bytes", 256 + 4 * len(value))
            bound_node(subject, state.budget)
            if any(
                not (
                    char in "\t\n\r"
                    or 0x20 <= ord(char) <= 0xD7FF
                    or 0xE000 <= ord(char) <= 0xFFFD
                    or 0x10000 <= ord(char) <= 0x10FFFF
                )
                for char in value
            ):
                state.unknown(
                    subject,
                    "IC0 text is outside the supported XML-character profile",
                )
            elif (
                datatype not in (XSD.string, RDF.langString)
                and value
                and (value[0].isspace() or value[-1].isspace())
            ):
                state.unknown(
                    subject,
                    "IC0 whitespace-facet interpretation is unsupported",
                )
            else:
                state.accept(
                    check_literal(value, location=str(subject), budget=state.budget)
                )
        if predicate in (RDFS.subClassOf, RDFS.subPropertyOf, RDFS.domain):
            state.unknown(
                subject,
                "IC0 datatype-sensitive schema entailment is unsupported",
            )
        if predicate == RDF.type and (
            str(value).startswith(str(XSD))
            or value in (RDFS.Datatype, RDF.langString, RDF.XMLLiteral, RDF.HTML)
        ):
            state.unknown(
                subject,
                "IC0 resource datatype membership is unsupported",
            )
    for prop, _, required in state.graph.triples((None, RDFS.range, None)):
        state.budget.consume("work")
        if not isinstance(required, URIRef) or not (
            str(required).startswith(str(XSD))
            or required in (RDF.langString, RDF.XMLLiteral, RDF.HTML)
        ):
            continue
        for subject, _, value in state.graph.triples((None, prop, None)):
            state.budget.consume("work")
            count += 1
            if not isinstance(value, Literal):
                state.unknown(
                    subject,
                    "IC0 resource range needs datatype interpretation",
                )
                continue
            relation = datatype_relation(
                str(literal_datatype(value)),
                str(required),
                regime="rdf",
                budget=state.budget,
            )
            if relation == "disjoint":
                state.finding(
                    "ic0_datatype_conflict",
                    subject,
                    "Literal value is disjoint from its asserted datatype range",
                )
            elif relation not in ("equal", "subset"):
                state.unknown(
                    subject,
                    "IC0 overlapping datatype membership is unavailable",
                )
    return max(1, count)


def check_dataset_structures(state: CubeIntegrity) -> int:
    """Every dataset has exactly one actual structure link."""
    for data in state.read.datasets:
        if len(state.read.values(data, QB.structure)) != 1:
            state.finding(
                "ic2_structure_cardinality",
                data,
                "Dataset requires exactly one DSD",
            )
    return len(state.read.datasets)


def check_declared_measures(state: CubeIntegrity) -> int:
    """Declared structures contain at least one actual declared measure."""
    for dsd in state.read.structures:
        if not state.read.properties(dsd.identifier, QB.MeasureProperty):
            state.finding(
                "ic3_measure_undeclared",
                dsd.identifier,
                "DSD has no declared measure",
            )
    return len(state.read.structures)


def check_dimension_ranges(state: CubeIntegrity) -> int:
    """Each declared dimension has an explicitly supplied range."""
    dimensions = state.read.typed(QB.DimensionProperty)
    for dim in dimensions:
        if not state.read.values(dim, RDFS.range):
            state.finding(
                "ic4_dimension_range_missing",
                dim,
                "Dimension has no declared range",
            )
    return len(dimensions)


def check_dimension_code_lists(state: CubeIntegrity) -> int:
    """Concept-valued dimensions declare their code list."""
    count = 0
    for dim in state.read.typed(QB.DimensionProperty):
        if SKOS.Concept in state.read.values(dim, RDFS.range):
            count += 1
            if not state.read.values(dim, QB.codeList):
                state.finding(
                    "ic5_code_list_missing",
                    dim,
                    "Concept dimension has no code list",
                )
    return count


def check_component_optionality(state: CubeIntegrity) -> int:
    """Explicit optionality is reserved for attribute components."""
    count = 0
    for dsd in state.read.structures:
        for component in dsd.components:
            if any(boolean_flag(value) is False for value in component.required):
                count += 1
                for prop in component.properties:
                    if (prop, RDF.type, QB.AttributeProperty) not in state.graph:
                        state.finding(
                            "ic6_nonattribute_optional",
                            component.specification,
                            "Only attributes may be marked optional",
                        )
    return count


def qualify_profile(checks: CubeIntegrity) -> None:
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
            flags = {boolean_flag(value) for value in component.required}
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
