"""Explicit endpoint acquisition into ordinary owned offline RDF snapshots."""

from .acquisition import acquire_endpoint
from .types import EndpointCompleteness, EndpointExtraction, EndpointPage, EndpointQuery

__all__ = [
    "EndpointCompleteness",
    "EndpointExtraction",
    "EndpointPage",
    "EndpointQuery",
    "acquire_endpoint",
]
