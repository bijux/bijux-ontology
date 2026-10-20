"""Finite integrity families over the same owned normalized Cube facts."""

from rdflib import BNode, URIRef
from rdflib.term import Node

from bijux_ontology.data.keys.candidates import KeyCandidates

from ..facts import boolean_flag
from ..normalization import QB
from .progress import CubeIntegrity


def check_observation_datasets(state: CubeIntegrity) -> int:
    """Every actual normalized observation belongs to exactly one dataset."""
    for obs in state.read.observations:
        state.budget.consume("work")
        if len(obs.datasets) != 1:
            state.finding(
                "ic1_dataset_cardinality",
                obs.subject,
                "Observation requires exactly one dataset",
            )
    return len(state.read.observations)


def check_observation_dimensions(state: CubeIntegrity) -> int:
    """Every observation supplies each of its declared dimensions."""
    count = 0
    for obs, dim in state.read.observation_obligations(QB.DimensionProperty):
        count += 1
        if not state.read.values(obs, dim):
            state.finding(
                "ic11_dimension_missing",
                obs,
                "Observation lacks a required dimension",
            )
    return count


def check_coordinate_uniqueness(state: CubeIntegrity) -> int:
    """Use actual scoped native composite keys with SPARQL value equality."""
    count = 0
    for data, dsd in state.read.data_structures():
        rows = state.read.coordinate_rows(data, dsd, state.issues)
        if rows.contract is None:
            state.unknown(
                data, "IC12 requires at least one supported declared dimension"
            )
            continue
        candidates = KeyCandidates(rows.contract, state.budget)
        for position, left in enumerate(rows.keys):
            count += position
            for right in candidates.select(left):
                state.budget.consume("work")
                relation = rows.relation(left, right, state.budget, state.issues)
                if relation == "equal":
                    state.finding(
                        "ic12_duplicate_coordinates",
                        right.record.subject,
                        "Observation duplicates another dimension coordinate",
                    )
                elif relation != "different":
                    state.unknown(
                        right.record.subject,
                        "IC12 coordinate equality is unavailable",
                    )
            candidates.add(left)
    return count


def check_required_attributes(state: CubeIntegrity) -> int:
    """Required components retain source values at every observation."""
    count = 0
    for obs in state.read.observations:
        for data in obs.datasets:
            for dsd in state.read.values(data, QB.structure):
                if not isinstance(dsd, URIRef | BNode):
                    continue
                structure = state.read.by_dsd.get(dsd)
                if structure is None:
                    continue
                for component in structure.components:
                    if any(boolean_flag(value) is True for value in component.required):
                        for prop in component.properties:
                            count += 1
                            if not isinstance(prop, URIRef) or not state.read.values(
                                obs.subject, prop
                            ):
                                state.finding(
                                    "ic13_attribute_missing",
                                    obs.subject,
                                    "Observation lacks a required attribute",
                                )
    return count


def check_ordinary_measures(state: CubeIntegrity) -> int:
    """Ordinary-measure cubes require every measure on each observation."""
    count = 0
    for data, dsd in state.read.data_structures():
        if QB.measureType in state.read.properties(dsd, QB.DimensionProperty):
            continue
        for obs in state.read.observations:
            if data not in obs.datasets:
                continue
            for measure in state.read.properties(dsd, QB.MeasureProperty):
                count += 1
                if not state.read.values(obs.subject, measure):
                    state.finding(
                        "ic14_measure_missing",
                        obs.subject,
                        "Ordinary observation lacks a declared measure",
                    )
    return count


def check_selected_measures(state: CubeIntegrity) -> int:
    """The actual measureType selects the measure value that must be present."""
    count = 0
    for _, dsd, obs, measures in state.read.measure_observations():
        declared = state.read.properties(dsd, QB.MeasureProperty)
        if len(measures) != 1:
            state.unknown(obs, "IC15 needs one supplied measureType value")
        for measure in measures:
            count += 1
            if not isinstance(measure, URIRef) or measure not in declared:
                state.unknown(
                    obs,
                    "IC15 measureType lacks a supplied measure declaration",
                )
            if not isinstance(measure, URIRef) or not state.read.values(obs, measure):
                state.finding(
                    "ic15_measure_value_missing",
                    obs,
                    "Measure-dimension observation lacks its selected measure",
                )
    return count


def check_measure_exclusivity(state: CubeIntegrity) -> int:
    """Measure-dimension observations cannot also supply another measure."""
    count = 0
    for _, dsd, obs, chosen in state.read.measure_observations():
        for measure in state.read.properties(dsd, QB.MeasureProperty):
            if state.read.values(obs, measure):
                count += 1
                if any(measure != actual for actual in chosen):
                    state.finding(
                        "ic16_multiple_measures",
                        obs,
                        "Observation supplies a different declared measure",
                    )
    return count


def check_measure_groups(state: CubeIntegrity) -> int:
    """Each non-measure coordinate group contains exactly all declared measures."""
    count = 0
    for data, dsd in state.read.data_structures():
        if QB.measureType not in state.read.properties(dsd, QB.DimensionProperty):
            continue
        rows = state.read.coordinate_rows(data, dsd, state.issues)
        if rows.contract is None:
            state.unknown(data, "IC17 coordinate extraction is unavailable")
            continue
        declared = set(state.read.properties(dsd, QB.MeasureProperty))
        for left in rows.keys:
            found: set[Node] = set()
            matched = 0
            uncertain = len(rows.keys) != rows.count
            for right in rows.keys:
                state.budget.consume("work")
                relation = rows.relation(
                    left, right, state.budget, state.issues, measures=True
                )
                if relation == "equal":
                    matched += 1
                    found.update(
                        state.read.values(right.record.subject, QB.measureType)
                    )
                elif relation != "different":
                    uncertain = True
            count += 1
            if uncertain:
                state.unknown(
                    left.record.subject,
                    "IC17 complete measure-coordinate grouping is unavailable",
                )
            elif matched != len(declared) or found != declared:
                state.finding(
                    "ic17_measure_group_incomplete",
                    left.record.subject,
                    "Coordinate group lacks exactly one observation per measure",
                )
    return count


def check_slice_datasets(state: CubeIntegrity) -> int:
    """Slice membership and observation dataset links agree exactly."""
    count = 0
    for data, _, slice_node in state.graph.triples((None, QB.slice, None)):
        state.budget.consume("work")
        for obs in state.read.values(slice_node, QB.observation):
            count += 1
            if data not in state.read.values(obs, QB.dataSet):
                state.finding(
                    "ic18_slice_dataset_conflict",
                    obs,
                    "Slice observation lacks its containing dataset link",
                )
    return count
