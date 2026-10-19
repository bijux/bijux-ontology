"""Explicit counterfactual proposals and jointly checked ontology reductions."""

from ..editing.reductions.checking import analyze_redundancy, prepare_reduction
from ..editing.reductions.types import RedundancyAnalysis, RedundancyCheck

__all__ = [
    "RedundancyAnalysis",
    "RedundancyCheck",
    "analyze_redundancy",
    "prepare_reduction",
]
