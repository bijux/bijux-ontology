"""Owned mapping occurrences and explicit application policies, without inference."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from rdflib import BNode, Literal

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri


def _iri(value: str, budget: Budget) -> str:
    """Capture an actual named identifier before hashing or formatting it."""
    if isinstance(value, BNode | Literal) or not isinstance(value, str):
        raise InputError("Mapping identifiers require named IRI strings")
    bound_text(value, budget)
    absolute_iri(value)
    return str(value)


def _iris(values: Sequence[str], budget: Budget) -> tuple[str, ...]:
    """Own one finite caller inventory using its measured length once."""
    if isinstance(values, str | bytes) or not isinstance(values, Sequence):
        raise InputError("Mapping inventories require finite nontext sequences")
    count = len(values)
    budget.consume("states", count)
    result = tuple(_iri(values[index], budget) for index in range(count))
    if len(set(result)) != count:
        raise InputError("Mapping inventories contain duplicate identifiers")
    return result


@dataclass(frozen=True)
class MappingRow:
    """One original table occurrence, including lexical cells and expanded IRIs."""

    subject: str
    predicate: str
    object: str
    justification: str
    cells: tuple[tuple[str, str], ...]
    subject_type: str = ""
    object_type: str = ""
    predicate_type: str = ""


@dataclass(frozen=True, init=False)
class MappingSet:
    """Factory-owned supported table; no mutable maintained dataframe escapes."""

    rows: tuple[MappingRow, ...]
    columns: tuple[str, ...]
    prefixes: tuple[tuple[str, str], ...]
    metadata_json: str
    source: bytes
    _content: bytes
    backend_version: str
    schema_version: str
    supplied_id: str
    supplied_license: str
    uninterpreted: tuple[str, ...]

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise InputError("Use read_sssom to qualify an owned mapping set")

    @classmethod
    def _owned(cls, **values: object) -> "MappingSet":
        """Adopt fields only after bounded worker response qualification."""
        result = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(result, name, value)
        return result


@dataclass(frozen=True)
class MappingPolicy:
    """Application requirements over actual selected rows, never OWL axioms.

    Kinds are exact asserted type IRIs and alternatives are disjunctive. Minimum
    coverage requires an explicit subject inventory. Counts use distinct named
    endpoints, while original duplicate row occurrences remain retained.
    """

    relations: tuple[str, ...]
    subject_kinds: tuple[str, ...] = ()
    object_kinds: tuple[str, ...] = ()
    subjects: tuple[str, ...] = ()
    minimum_targets: int = 0
    maximum_targets: int | None = None
    maximum_sources: int | None = None
    conflicts: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        budget = Budget()
        for name in ("relations", "subject_kinds", "object_kinds", "subjects"):
            object.__setattr__(self, name, _iris(getattr(self, name), budget))
        if not self.relations:
            raise InputError("A mapping policy requires explicit allowed relations")
        for name in ("minimum_targets", "maximum_targets", "maximum_sources"):
            number = getattr(self, name)
            if (
                name == "minimum_targets"
                and number is None
                or number is not None
                and (type(number) is not int or number < 0 or number.bit_length() > 63)
            ):
                raise InputError(
                    "Mapping cardinalities require finite nonnegative integers"
                )
        if self.minimum_targets and not self.subjects:
            raise InputError(
                "Minimum mapping coverage needs explicit subject identifiers"
            )
        if (
            self.maximum_targets is not None
            and self.maximum_targets < self.minimum_targets
        ):
            raise InputError("Mapping minimum exceeds maximum targets")
        raw = cast(Sequence[Sequence[str]], self.conflicts)
        if isinstance(raw, str | bytes) or not isinstance(raw, Sequence):
            raise InputError("Mapping conflicts require finite relation pairs")
        count = len(raw)
        budget.consume("states", count)
        pairs = []
        for index in range(count):
            pair = _iris(raw[index], budget)
            if len(pair) != 2:
                raise InputError("A mapping conflict requires two different relations")
            pairs.append((pair[0], pair[1]))
        object.__setattr__(self, "conflicts", tuple(pairs))


@dataclass(frozen=True)
class MappingRead:
    """Raw interchange qualification and its independently usable owned table."""

    mapping_set: MappingSet | None
    report: Report


@dataclass(frozen=True)
class MappingCheck:
    """Consumer application truth over explicitly supplied endpoint vocabularies."""

    report: Report


@dataclass(frozen=True)
class MappingExport:
    """Faithful TSV bytes; schema interchange never implies semantic equivalence."""

    content: bytes | None
    report: Report
