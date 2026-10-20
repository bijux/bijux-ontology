"""Adopt native SHACL responses against the exact captured source contract."""

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.shacl.evidence.results import ShaclResult
from bijux_ontology.shacl.validation.adoption import adopt_result

from .requests import NativeShaclRequest


def adopt_native_result(raw: bytes, request: NativeShaclRequest) -> ShaclResult:
    """Retain raw conformance, coverage and source findings under one allowance."""
    if type(request) is not NativeShaclRequest:
        raise InputError("Expected a prepared native SHACL request")
    return adopt_result(
        raw,
        request._plan._value,
        request._data,
        request._data,
        request._focus,
        graph_name=request._graph_name,
        union=request._union,
        source_usages=request._sources,
    )
