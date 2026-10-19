"""Owned runnable public requests and exact completed-failure evidence."""

import json
from dataclasses import dataclass
from typing import ClassVar, TypeAlias, TypeVar

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.json.paths import SourcePath
from bijux_ontology.rdf.patches.types import PatchResource, SnapshotWitness

_T = TypeVar("_T", bound="_Owned")


class ReproductionUnavailable(InputError):
    """A request needs unsupported identity or operation capabilities."""


class _Owned:
    """Construction belongs to the request factories and native evidence reader."""

    def __init__(self) -> None:
        raise InputError("Use the reproduction request factories")

    @classmethod
    def _owned(cls: type[_T], **values: object) -> _T:
        result = object.__new__(cls)
        for key, value in values.items():
            object.__setattr__(result, key, value)
        return result


@dataclass(frozen=True, init=False)
class JsonResource(_Owned):
    """One owned loader response; response identity is distinct from its lookup IRI."""

    iri: str
    document_json: str
    effective_url: str | None = None
    context_url: str | None = None


@dataclass(frozen=True, init=False)
class RdfResource(_Owned):
    """Supplied RDF bytes and parser/base metadata; no live path or retrieval."""

    iri: str
    content: bytes
    format: str
    document_iri: str | None = None


@dataclass(frozen=True, init=False)
class ContextRequest(_Owned):
    """Check ordinary usages with established public JSON expectation declarations."""

    kind: ClassVar[str] = "context"
    document_json: str
    expectations_json: str
    resources: tuple[JsonResource, ...]
    base: str | None

    @property
    def document(self) -> object:
        """Return an independent ordinary JSON document for native public replay."""
        return json.loads(self.document_json)


@dataclass(frozen=True, init=False)
class EmissionRequest(_Owned):
    """Check selected model emissions using the existing public declaration codecs."""

    kind: ClassVar[str] = "emission"
    document_json: str
    models_json: str
    usages_json: str
    resources: tuple[JsonResource, ...]
    base: str | None

    @property
    def document(self) -> object:
        """Return an independent ordinary JSON document for native public replay."""
        return json.loads(self.document_json)


@dataclass(frozen=True, init=False)
class SkosRequest(_Owned):
    """Explicit named concept roots and finite native SKOS rules over one scope."""

    kind: ClassVar[str] = "skos"
    source: SnapshotWitness
    concepts: tuple[str, ...]
    rules: tuple[str, ...]


@dataclass(frozen=True, init=False)
class ShaclRequest(_Owned):
    """Explicit Core pairs keep focus/shape obligations fixed while data is reduced."""

    kind: ClassVar[str] = "shacl"
    data: SnapshotWitness
    shapes: SnapshotWitness
    focus_nodes: tuple[PatchResource, ...]
    use_shapes: tuple[PatchResource, ...]
    imports: tuple[RdfResource, ...]


ReproductionRequest: TypeAlias = (
    ContextRequest | EmissionRequest | SkosRequest | ShaclRequest
)


@dataclass(frozen=True)
class SourceObligation:
    """Actual selected owner identity and unchanged raw value, anchored in source."""

    path: SourcePath
    subjects: tuple[str, ...]
    graphs: tuple[str | None, ...]
    raw_json: str | None
    named: bool


@dataclass(frozen=True, init=False)
class OperationResult(_Owned):
    """Actual native report with source obligations or original SHACL finding fields."""

    _request: ReproductionRequest
    report: Report
    obligations: tuple[SourceObligation, ...] = ()
    shacl_findings_json: str | None = None


@dataclass(frozen=True, init=False)
class FailureSignature(_Owned):
    """Only complete nonvacuous native contradiction evidence can establish a match."""

    _request: ReproductionRequest
    kind: str
    report: Report
    finding: Issue
    obligations: tuple[SourceObligation, ...]
    shacl_findings_json: str | None


@dataclass(frozen=True)
class SelectorRebinding:
    """A concrete deletion changes a selected path only with qualified named owners."""

    before: SourcePath
    after: SourcePath


@dataclass(frozen=True)
class FailureReduction:
    """Best native-verified runnable request survives a separately incomplete search."""

    original: ReproductionRequest
    request: ReproductionRequest | None
    original_result: OperationResult | None
    result: OperationResult | None
    signature: FailureSignature | None
    trials: int
    accepted: int
    rebindings: tuple[SelectorRebinding, ...]
    report: Report
