"""Explicit counterfactual proposals and jointly checked ontology reductions."""

from .checking import analyze_redundancy, prepare_reduction
from .types import RedundancyAnalysis, RedundancyCheck

__all__ = [
    "RedundancyAnalysis",
    "RedundancyCheck",
    "analyze_redundancy",
    "prepare_reduction",
]
