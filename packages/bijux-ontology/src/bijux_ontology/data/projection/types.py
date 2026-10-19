"""Explicit RDF read intent and immutable term-backed application records."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from rdflib import BNode, URIRef
from rdflib.paths import Path
from rdflib.term import Node

from bijux_ontology.declarations.types import SubjectIdentity
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.limits import Budget, bound_text
from bijux_ontology.rdf.paths import RdfPath, capture_path
from bijux_ontology.rdf.snapshots import PreparedSource

Decoder = Literal["raw", "string", "integer", "decimal", "boolean", "reference"]
FieldState = Literal["decoded", "raw", "missing", "ambiguous", "invalid", "unverified"]


@dataclass(frozen=True)
class ReadField:
    """Read distinct endpoints, or one actual ordered list, under a narrow decoder.

    Scalar/list modes allow at most one endpoint/head. Many mode returns a
    deterministic evidence order, not an RDF ordering assertion. Raw never claims
    datatype interpretation. External references require explicit permission.
    """

    name: str
    path: URIRef | Path | RdfPath
    mode: Literal["scalar", "many", "list"] = "scalar"
    decoder: Decoder = "raw"
    minimum: int = 1
    maximum: int | None = None
    minimum_members: int = 0
    maximum_members: int | None = None
    references: Literal["selected", "external"] = "selected"

    def __post_init__(self) -> None:
        """Capture paths before caller mutation and reject contradictory intent."""
        bound_text(self.name, Budget())
        if type(self.name) is not str or not self.name.strip():
            raise InputError("Read fields require ordinary nonempty names")
        if (
            self.mode not in ("scalar", "many", "list")
            or self.decoder
            not in ("raw", "string", "integer", "decimal", "boolean", "reference")
            or self.references not in ("selected", "external")
        ):
            raise InputError("Unsupported projection mode, decoder or reference policy")
        for count in (
            self.minimum,
            self.maximum,
            self.minimum_members,
            self.maximum_members,
        ):
            if count is not None and (
                type(count) is not int or count < 0 or count.bit_length() > 63
            ):
                raise InputError("Read bounds require finite nonnegative integers")
        if self.minimum is None or self.minimum_members is None:
            raise InputError("Read minima require integers")
        if (
            self.maximum is not None
            and self.minimum > self.maximum
            or self.maximum_members is not None
            and self.minimum_members > self.maximum_members
        ):
            raise InputError("Read minimum exceeds maximum")
        if self.mode != "many" and (self.minimum > 1 or self.maximum not in (None, 1)):
            raise InputError("Scalar/list projections require at most one endpoint")
        if self.mode != "list" and (
            self.minimum_members or self.maximum_members is not None
        ):
            raise InputError("Member bounds require an ordered list")
        object.__setattr__(self, "path", capture_path(self.path))


@dataclass(frozen=True)
class ReadBinding:
    """Select one subject and its read fields; no child binding is guessed."""

    name: str
    subject: URIRef | BNode
    fields: tuple[ReadField, ...]
    types: tuple[str, ...] = ()
    identity: SubjectIdentity = SubjectIdentity()

    def __post_init__(self) -> None:
        """Require immutable typed inventories; operation bounds qualify scalars."""
        if type(self.name) is not str or not isinstance(self.subject, URIRef | BNode):
            raise InputError("Read bindings require a name and RDF resource subject")
        if (
            type(self.fields) is not tuple
            or not self.fields
            or any(not isinstance(item, ReadField) for item in self.fields)
        ):
            raise InputError("Read bindings require immutable nonempty read fields")
        if type(self.types) is not tuple or not isinstance(
            self.identity, SubjectIdentity
        ):
            raise InputError(
                "Read bindings require immutable types and subject identity"
            )


@dataclass(frozen=True)
class ReferenceHandle:
    """Resource identity in this exact source and physical graph, never OWL equality."""

    source: PreparedSource
    graph: URIRef | BNode | None
    subject: URIRef | BNode


ProjectedValue = Node | str | int | bool | Decimal | ReferenceHandle


@dataclass(frozen=True)
class ProjectedField:
    """Exact endpoints and member terms accompany all declared Python values.

    Terms are endpoints for ordinary fields and positional members for lists;
    endpoints always retain the original head. A broken list exposes only its
    proved prefix. No value is selected from an ambiguous scalar/head inventory.
    """

    name: str
    state: FieldState
    endpoints: tuple[Node, ...]
    terms: tuple[Node, ...]
    values: tuple[ProjectedValue, ...]


@dataclass(frozen=True)
class ProjectedRecord:
    """One immutable selected record, suitable for an explicitly trusted factory."""

    name: str
    reference: ReferenceHandle
    fields: tuple[ProjectedField, ...]


@dataclass(frozen=True, init=False, eq=False)
class ProjectionRead:
    """Owned evidence; only a complete verified result authorizes construction."""

    records: tuple[ProjectedRecord, ...]
    report: Report

    def __init__(self) -> None:
        raise InputError("Use project_records to qualify read contracts")

    @classmethod
    def _owned(
        cls, records: tuple[ProjectedRecord, ...], report: Report
    ) -> "ProjectionRead":
        result = object.__new__(cls)
        object.__setattr__(result, "records", records)
        object.__setattr__(result, "report", report)
        return result
