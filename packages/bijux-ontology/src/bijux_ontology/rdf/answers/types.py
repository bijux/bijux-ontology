"""Owned finite answer tables beside artifact and external execution evidence."""

from dataclasses import dataclass
from typing import Literal

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.queries.protocol import QueryTerm

AnswerFormat = Literal["json", "xml", "tsv", "csv"]
AnswerSemantics = Literal["bag", "set", "sequence"]


class AnswerUnavailable(InputError):
    """A valid RDF answer lies outside a selected codec capability."""


@dataclass(frozen=True, init=False)
class AnswerArtifact:
    """One result-scoped blank inventory and exact immutable RDF term rows.

    The report establishes exhaustive decoding of the supplied finite artifact.
    External completeness is caller evidence, never inferred from valid syntax.
    Rows align with the preserved serialized variable order; None is unbound.
    """

    kind: Literal["ASK", "SELECT"] | None
    answer: bool | None
    variables: tuple[str, ...]
    rows: tuple[tuple[QueryTerm | None, ...], ...]
    external_complete: bool | None
    report: Report

    def __init__(self) -> None:
        """Require actual bounded reading or local-result capture ownership."""
        raise InputError("Use read_answers or capture_answer to construct an artifact")

    @classmethod
    def _new(
        cls,
        kind: Literal["ASK", "SELECT"] | None,
        answer: bool | None,
        variables: tuple[str, ...],
        rows: tuple[tuple[QueryTerm | None, ...], ...],
        external_complete: bool | None,
        report: Report,
    ) -> "AnswerArtifact":
        """Publish only a fully qualified internal capture or its stopped prefix."""
        result = object.__new__(cls)
        for name, value in (
            ("kind", kind),
            ("answer", answer),
            ("variables", variables),
            ("rows", rows),
            ("external_complete", external_complete),
            ("report", report),
        ):
            object.__setattr__(result, name, value)
        return result


@dataclass(frozen=True)
class AnswerSerialization:
    """Bounded encoded output with explicit, retained information losses."""

    format: AnswerFormat
    payload: bytes | None
    losses: tuple[str, ...]
    report: Report
