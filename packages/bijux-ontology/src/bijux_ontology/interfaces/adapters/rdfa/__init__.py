"""Optional maintained HTML5 RDFa extraction with independently selected JSON-LD."""

from .extraction import extract_rdfa
from .runtime import prepare_rdfa_runtime
from .types import RdfaExtraction, RdfaProducer, RdfaRuntime

__all__ = [
    "RdfaExtraction",
    "RdfaProducer",
    "RdfaRuntime",
    "extract_rdfa",
    "prepare_rdfa_runtime",
]
