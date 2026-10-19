"""Plain declarations supplied by application-owned extraction adapters."""

from collections.abc import Iterable
from dataclasses import KW_ONLY, dataclass
from typing import Literal, TypeVar

from rdflib import RDF, URIRef
from rdflib import Literal as RDFLiteral

from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.rdf.identifiers import absolute_iri, language_tag

from .resource_roles import ResourceRole
from .value_constraints import ValueConstraints

ValueKind = Literal["literal", "iri", "object", "unknown"]
Emission = Literal["unknown", "optional", "guaranteed"]


_T = TypeVar("_T")


def _sequence(value: Iterable[_T], location: str) -> tuple[_T, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise InputError("Expected a sequence", location=location)
    return tuple(value)


def _text(value: str, location: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise InputError("Expected a nonempty string", location=location)
    return value


def _iris(values: tuple[str, ...], location: str) -> tuple[str, ...]:
    return tuple(
        str(absolute_iri(value, location=location))
        for value in _sequence(values, location)
    )


@dataclass(frozen=True)
class TermDeclaration:
    """An explicit application IRI and its acceptable RDF declaration kinds."""

    iri: str
    kinds: tuple[str, ...] = ()
    location: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "iri", str(absolute_iri(self.iri, location=self.location))
        )
        object.__setattr__(self, "kinds", _iris(self.kinds, self.location))
        _text(self.location, "location", empty=True)


@dataclass(frozen=True)
class ValueDeclaration:
    """One permitted value branch; its RDF types apply simultaneously.

    Language and direction are unchecked with None, required absent with ABSENT,
    or compared to the supplied tag/direction. RDF direction encodings are
    selected by the checking operation; native datatypes stay physical.
    """

    kind: ValueKind = "unknown"
    datatype: str | None = None
    language: str | Absent | None = None
    types: tuple[str, ...] = ()
    model: str | None = None
    container: str | None = None
    _: KW_ONLY
    samples: tuple[RDFLiteral, ...] = ()
    direction: Literal["ltr", "rtl"] | Absent | None = None
    constraints: ValueConstraints | None = None
    role: ResourceRole | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in {
            "literal",
            "iri",
            "object",
            "unknown",
        }:
            raise InputError(f"Unknown RDF value kind: {self.kind}")
        object.__setattr__(self, "types", _iris(self.types, "types"))
        if self.datatype is not None:
            object.__setattr__(
                self, "datatype", str(absolute_iri(self.datatype, location="datatype"))
            )
        if isinstance(self.language, str):
            language_tag(self.language, location="language")
        elif self.language is not None and self.language is not ABSENT:
            raise InputError("Expected a language tag, ABSENT or None")
        if (
            self.direction is not None
            and self.direction is not ABSENT
            and (
                not isinstance(self.direction, str)
                or self.direction not in ("ltr", "rtl")
            )
        ):
            raise InputError("Expected ltr, rtl, ABSENT or None direction")
        if self.model is not None:
            _text(self.model, "model")
        if self.container is not None:
            _text(self.container, "container")
        if self.kind == "literal" and (self.types or self.model is not None):
            raise InputError("Literal branches cannot declare resource types or models")
        if self.kind in {"iri", "object"} and (
            self.datatype is not None
            or self.language is not None
            or self.direction is not None
        ):
            raise InputError("Resource branches cannot declare literal metadata")
        if isinstance(self.language, str) and self.datatype not in (
            None,
            str(RDF.langString),
        ):
            raise InputError("A language-tagged literal has datatype rdf:langString")
        if self.language is ABSENT and self.datatype == str(RDF.langString):
            raise InputError("rdf:langString requires a language tag")
        if self.constraints is not None and not isinstance(
            self.constraints, ValueConstraints
        ):
            raise InputError("Expected explicit application value constraints")
        if self.role is not None and (
            not isinstance(self.role, ResourceRole)
            or self.kind not in ("iri", "object")
        ):
            raise InputError("Resource role contracts require a resource value branch")
        samples = _sequence(self.samples, "samples")
        if any(not isinstance(sample, RDFLiteral) for sample in samples):
            raise InputError("Literal samples must be RDFLib Literal values")
        if samples and self.kind != "literal":
            raise InputError("Literal samples belong only to literal branches")
        object.__setattr__(self, "samples", samples)


@dataclass(frozen=True)
class OutputRequirement:
    """Application output requirements, independent of Python and OWL cardinality.

    Value counts use distinct RDF objects, counting each nonempty list head and
    RDF nil for an empty list. Member bounds apply separately to each actual RDF
    list, preserving repeated positions. Omission/null tests concern source JSON.
    """

    require_property: bool = False
    allow_null: bool = False
    allow_empty: bool = False
    minimum_values: int | None = None
    maximum_values: int | None = None
    minimum_list_members: int | None = None
    maximum_list_members: int | None = None

    def __post_init__(self) -> None:
        for name in ("require_property", "allow_null", "allow_empty"):
            if type(getattr(self, name)) is not bool:
                raise InputError(f"{name} must be a boolean")
        for lower, upper in (
            ("minimum_values", "maximum_values"),
            ("minimum_list_members", "maximum_list_members"),
        ):
            minimum, maximum = getattr(self, lower), getattr(self, upper)
            for name, value in ((lower, minimum), (upper, maximum)):
                if value is not None and (type(value) is not int or value < 0):
                    raise InputError(f"{name} must be a nonnegative integer or null")
            if minimum is not None and maximum is not None and minimum > maximum:
                raise InputError("Output minimum exceeds maximum", location=lower)


@dataclass(frozen=True)
class FieldDeclaration:
    """A predicate and alternative values emitted when a field is present."""

    name: str
    predicate: str
    values: tuple[ValueDeclaration, ...]
    key: str | None = None
    optional: bool = False
    location: str = ""
    reverse: bool = False
    _: KW_ONLY
    emission: Emission = "unknown"
    output: OutputRequirement | None = None

    def __post_init__(self) -> None:
        _text(self.name, "name")
        _text(self.location, "location", empty=True)
        _text(self.predicate, "predicate")
        if self.predicate not in {"@id", "@type"}:
            absolute_iri(self.predicate, location=self.location or self.name)
        object.__setattr__(self, "predicate", str(self.predicate))
        if self.key is not None:
            _text(self.key, "key")
        if type(self.optional) is not bool or type(self.reverse) is not bool:
            raise InputError("optional and reverse must be booleans")
        if not isinstance(self.emission, str) or self.emission not in {
            "unknown",
            "optional",
            "guaranteed",
        }:
            raise InputError(f"Unknown emission guarantee: {self.emission}")
        if self.output is not None and not isinstance(self.output, OutputRequirement):
            raise InputError("Expected an explicit OutputRequirement")
        if (
            self.emission == "guaranteed"
            and self.output is not None
            and self.output.maximum_values == 0
        ):
            raise InputError(
                "Guaranteed emission conflicts with zero permitted RDF values"
            )
        if self.optional:
            if (
                self.emission == "guaranteed"
                or self.output is not None
                and (
                    self.output.require_property
                    or (self.output.minimum_values or 0) > 0
                )
            ):
                raise InputError("Legacy optional conflicts with required output")
            if self.emission == "unknown":
                object.__setattr__(self, "emission", "optional")
        values = _sequence(self.values, "values")
        if not values or any(
            not isinstance(value, ValueDeclaration) for value in values
        ):
            raise InputError("A field requires explicit alternative value declarations")
        object.__setattr__(self, "values", values)


@dataclass(frozen=True)
class SubjectIdentity:
    """Application-owned expected RDF subject identity, without OWL inference."""

    expected_iri: str | None = None
    allow_blank: bool = True

    def __post_init__(self) -> None:
        if self.expected_iri is not None:
            object.__setattr__(
                self,
                "expected_iri",
                str(absolute_iri(self.expected_iri, location="expected_iri")),
            )
        if type(self.allow_blank) is not bool:
            raise InputError("allow_blank must be a boolean")


def _field_names(values: tuple[str, ...], location: str) -> tuple[str, ...]:
    names = tuple(_text(name, location) for name in _sequence(values, location))
    if len(names) != len(set(names)):
        raise InputError("Field references must be unique", location=location)
    return names


@dataclass(frozen=True)
class EmissionCondition:
    """Select a scenario by one exact emitted IRI or literal discriminator.

    Missing, multiple or unavailable discriminator terms do not establish a
    condition. Blank labels cannot serve as cross-output discriminator identity.
    """

    field: str
    equals: URIRef | RDFLiteral

    def __post_init__(self) -> None:
        _text(self.field, "condition.field")
        if not isinstance(self.equals, URIRef | RDFLiteral):
            raise InputError("Conditions require an exact RDF IRI or literal")
        if isinstance(self.equals, URIRef):
            absolute_iri(str(self.equals), location="condition.equals")


@dataclass(frozen=True)
class EmissionScenario:
    """One exact emitted field set within its group's finite field inventory."""

    identifier: str
    present: tuple[str, ...]
    condition: EmissionCondition | None = None

    def __post_init__(self) -> None:
        _text(self.identifier, "scenario.identifier")
        object.__setattr__(self, "present", _field_names(self.present, self.identifier))
        if self.condition is not None and not isinstance(
            self.condition, EmissionCondition
        ):
            raise InputError("Expected a typed emission condition")


@dataclass(frozen=True)
class EmissionGroup:
    """Finite co-emission bounds and optional caller-authored allowed scenarios.

    Bounds always apply. Without scenarios they define the allowed field sets.
    With scenarios, closed=True excludes unlisted cases; otherwise uncovered
    cases remain unverified. Source null/presence remains a separate contract.
    """

    identifier: str
    fields: tuple[str, ...]
    minimum: int = 0
    maximum: int | None = None
    scenarios: tuple[EmissionScenario, ...] = ()
    closed: bool = False

    def __post_init__(self) -> None:
        _text(self.identifier, "group.identifier")
        names = _field_names(self.fields, self.identifier)
        if not names:
            raise InputError("Emission groups require a finite field inventory")
        if type(self.minimum) is not int or self.minimum < 0:
            raise InputError("Group minimum requires a nonnegative integer")
        if self.maximum is not None and (
            type(self.maximum) is not int or self.maximum < self.minimum
        ):
            raise InputError("Group maximum must be an integer at least its minimum")
        if self.minimum > len(names) or (
            self.maximum is not None and self.maximum > len(names)
        ):
            raise InputError("Group bounds exceed its field inventory")
        if type(self.closed) is not bool:
            raise InputError("Group closed must be a boolean")
        scenarios = _sequence(self.scenarios, self.identifier)
        identifiers = set()
        for scenario in scenarios:
            if not isinstance(scenario, EmissionScenario):
                raise InputError("Expected typed emission scenarios")
            if scenario.identifier in identifiers:
                raise InputError("Scenario identifiers must be unique")
            identifiers.add(scenario.identifier)
            if set(scenario.present) - set(names):
                raise InputError("Scenario references fields outside its group")
            if len(scenario.present) < self.minimum or (
                self.maximum is not None and len(scenario.present) > self.maximum
            ):
                raise InputError("Scenario contradicts its group's emission bounds")
        object.__setattr__(self, "fields", names)
        object.__setattr__(self, "scenarios", scenarios)


@dataclass(frozen=True)
class EmissionCoverage:
    """Close selected output nodes over declared fields and explicit extensions.

    Extension predicates remain visible without acquiring a value contract.
    Aliases are (source key, declared field name) pairs; they check actual values
    and children without relaxing required primary-key presence. Metadata
    permission never suppresses the existing subject or type checks. Structural
    keywords cannot be excluded as metadata. Native RDF cannot prove source-key
    coverage, which remains independently unverified.
    """

    extensions: tuple[str, ...] = ()
    reverse_extensions: tuple[str, ...] = ()
    metadata: tuple[str, ...] = ("@context",)
    aliases: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for name in ("extensions", "reverse_extensions"):
            values = _iris(getattr(self, name), name)
            if len(values) != len(set(values)):
                raise InputError("Coverage extension predicates must be unique")
            object.__setattr__(self, name, values)
        metadata = _field_names(self.metadata, "coverage.metadata")
        if set(metadata) - {"@context", "@id", "@type", "@index"}:
            raise InputError("Coverage metadata must name supported metadata keywords")
        object.__setattr__(self, "metadata", metadata)
        aliases = []
        keys = set()
        for pair in _sequence(self.aliases, "coverage.aliases"):
            values = _sequence(pair, "coverage.aliases")
            if len(values) != 2:
                raise InputError("Coverage aliases require source-key/field-name pairs")
            key, target = values
            _text(key, "coverage.alias.key")
            _text(target, "coverage.alias.field")
            if key.startswith("@") or key in keys:
                raise InputError("Coverage alias keys must be unique ordinary keys")
            keys.add(key)
            aliases.append((key, target))
        object.__setattr__(self, "aliases", tuple(aliases))


@dataclass(frozen=True)
class ModelDeclaration:
    """A stable model identity, simultaneous RDF types and serialised fields."""

    identifier: str
    types: tuple[str, ...]
    fields: tuple[FieldDeclaration, ...] = ()
    location: str = ""
    _: KW_ONLY
    subject: SubjectIdentity = SubjectIdentity()
    emission_groups: tuple[EmissionGroup, ...] = ()
    coverage: EmissionCoverage | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.subject, SubjectIdentity):
            raise InputError("Expected a typed subject identity contract")
        _text(self.identifier, "identifier")
        _text(self.location, "location", empty=True)
        object.__setattr__(self, "types", _iris(self.types, self.identifier))
        fields = _sequence(self.fields, "fields")
        if any(not isinstance(field, FieldDeclaration) for field in fields):
            raise InputError(
                "Expected typed field declarations", location=self.identifier
            )
        by_key: dict[str, FieldDeclaration] = {}
        by_name: dict[str, FieldDeclaration] = {}
        for field in fields:
            for identity, seen in (
                (field.name, by_name),
                (field.key or field.name, by_key),
            ):
                if identity in seen and seen[identity] != field:
                    raise InputError(
                        "Conflicting field occurrence",
                        location=f"{self.identifier}.{identity}",
                    )
                seen[identity] = field
        object.__setattr__(self, "fields", tuple(by_name.values()))
        groups = _sequence(self.emission_groups, self.identifier)
        group_names = set()
        for group in groups:
            if not isinstance(group, EmissionGroup):
                raise InputError("Expected typed emission groups")
            if group.identifier in group_names:
                raise InputError("Emission group identifiers must be unique")
            group_names.add(group.identifier)
            referenced = set(group.fields)
            referenced.update(
                scenario.condition.field
                for scenario in group.scenarios
                if scenario.condition is not None
            )
            if referenced - by_name.keys():
                raise InputError("Emission group references unknown model fields")
        object.__setattr__(self, "emission_groups", groups)
        if self.coverage is not None:
            if not isinstance(self.coverage, EmissionCoverage):
                raise InputError("Expected a typed emission coverage contract")
            for key, target in self.coverage.aliases:
                if target not in by_name:
                    raise InputError("Coverage alias references an unknown model field")
                if key in by_key:
                    raise InputError("Coverage alias collides with a primary field key")
