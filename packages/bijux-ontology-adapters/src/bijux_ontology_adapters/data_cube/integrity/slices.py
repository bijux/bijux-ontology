"""Finite integrity families over the same owned normalized Cube facts."""

from rdflib import URIRef
from rdflib.namespace import RDF

from ..normalization import QB
from .progress import CubeIntegrity


def check_slice_keys(state: CubeIntegrity) -> int:
    """Every actual SliceKey belongs to an explicitly declared DSD."""
    for key in state.read.keys:
        if not any(
            (dsd.identifier, QB.sliceKey, key) in state.graph
            and (dsd.identifier, RDF.type, QB.DataStructureDefinition) in state.graph
            for dsd in state.read.structures
        ):
            state.finding(
                "ic7_slice_key_undeclared", key, "Slice key has no declared DSD"
            )
    return len(state.read.keys)


def check_slice_key_properties(state: CubeIntegrity) -> int:
    """Slice-key properties are components of every associated DSD."""
    count = 0
    for key in state.read.keys:
        for dsd in state.read.subjects(QB.sliceKey, key):
            declared = {
                prop
                for spec in state.read.values(dsd, QB.component)
                for prop in state.read.values(spec, QB.componentProperty)
            }
            for prop in state.read.values(key, QB.componentProperty):
                count += 1
                if prop not in declared:
                    state.finding(
                        "ic8_slice_key_inconsistent",
                        key,
                        "Slice-key property is absent from its DSD",
                    )
    return count


def check_slice_structures(state: CubeIntegrity) -> int:
    """Every slice has exactly one supplied slice structure."""
    for slice_node in state.read.slices:
        if len(state.read.values(slice_node, QB.sliceStructure)) != 1:
            state.finding(
                "ic9_slice_structure_cardinality",
                slice_node,
                "Slice requires exactly one structure",
            )
    return len(state.read.slices)


def check_slice_dimensions(state: CubeIntegrity) -> int:
    """Slices carry all properties fixed by their keys."""
    count = 0
    for slice_node, _, key in state.graph.triples((None, QB.sliceStructure, None)):
        state.budget.consume("work")
        for prop in state.read.values(key, QB.componentProperty):
            count += 1
            if not isinstance(prop, URIRef) or not state.read.values(slice_node, prop):
                state.finding(
                    "ic10_slice_dimension_missing",
                    slice_node,
                    "Slice lacks a value fixed by its key",
                )
    return count
