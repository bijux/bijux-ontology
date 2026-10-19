"""Immutable captured theories and finite selected inference witnesses."""

from dataclasses import dataclass, field
from typing import Literal

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.snapshots import PreparedSource

from ...axioms.types import NamedEntity, ObjectProperty
from ...backend.types import OwlProfileBackend, ProfileDocument
from ...documents.types import OwlDocument
from ..entailment.types import EntailmentAnswer, EntailmentCheck


@dataclass(frozen=True)
class TheoryDocument:
    """Exact staged bytes and interpretation metadata, independent of job paths."""

    content: bytes
    metadata: tuple[tuple[str, str | None], ...]


@dataclass(frozen=True, init=False)
class PreparedReasoningTheory:
    """A captured revision, never a watcher of mutable external resources.

    A changed import/root/backend revision requires a new capture and explicit
    session binding. Exact byte identity is intentionally stronger than logical
    equivalence; no canonical RDF identity is claimed by this revision token.
    """

    backend: OwlProfileBackend
    identity: str
    input_kind: Literal["native", "rdf"]
    documents: tuple[OwlDocument | ProfileDocument, ...]
    rdf_sources: tuple[PreparedSource, ...]
    aliases: tuple[tuple[str, int], ...]
    files: tuple[TheoryDocument, ...]
    report: Report

    def __init__(self) -> None:
        raise InputError("Use prepare_reasoning_theory to capture an explicit revision")

    @classmethod
    def _owned(cls, **values: object) -> "PreparedReasoningTheory":
        """Adopt only fields qualified by the bounded capture factory."""
        result = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(result, name, value)
        return result


@dataclass(frozen=True)
class SessionPreparation:
    """Actual native preparation evidence for one owned worker generation."""

    theory_identity: str
    process_id: int | None
    loads: int | None
    classifications: int | None
    report: Report


@dataclass(frozen=True)
class InferenceSelection:
    """Explicit finite domains; never every existential consequence."""

    owners: tuple[NamedEntity, ...]
    superclasses: tuple[NamedEntity, ...] = ()
    properties: tuple[ObjectProperty, ...] = ()
    fillers: tuple[NamedEntity, ...] = ()
    families: tuple[Literal["subclass", "existential"], ...] = (
        "subclass",
        "existential",
    )


@dataclass(frozen=True)
class InferredExistential:
    """An entailed class restriction, not a named object-property assertion."""

    owner: NamedEntity
    property: ObjectProperty
    filler: NamedEntity
    answer: EntailmentAnswer
    quantifier: Literal["some"] = "some"


@dataclass(frozen=True)
class InferenceProjection:
    """Finite pair coverage and a separate ordinary RDF inference artifact.

    Unknown pairs retain their answers but withhold the complete-proof artifact.
    A complete result covers the supplied domains only, not all possible fillers.
    """

    selection: InferenceSelection
    theory_identity: str
    answers: EntailmentCheck | None
    edges: tuple[InferredExistential, ...]
    source: PreparedSource | None
    report: Report
    content: bytes | None = field(default=None, repr=False)
