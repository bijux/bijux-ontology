"""Immutable finite projections of actual maintained OWLAPI objects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from bijux_ontology.reports import Report

from ..backend.types import BackendCapability, ProfileDocument
from ..documents.types import OwlDocument

EntityKind = Literal[
    "class",
    "object_property",
    "data_property",
    "annotation_property",
    "datatype",
    "named_individual",
]


@dataclass(frozen=True)
class NamedEntity:
    """An actual named OWL entity with its distinct native role."""

    kind: EntityKind
    iri: str


@dataclass(frozen=True)
class NativeLiteral:
    """Native parsed lexical data; this does not assert original RDF spelling."""

    lexical: str
    datatype: str
    language: str | None


@dataclass(frozen=True)
class IriValue:
    """An annotation IRI value, without inventing an OWL entity role."""

    iri: str


@dataclass(frozen=True)
class ObjectProperty:
    """A named object property or its maintained inverse expression."""

    iri: str
    inverse: bool = False


@dataclass(frozen=True)
class DataProperty:
    """A named data property, never an object property expression."""

    iri: str


@dataclass(frozen=True)
class ClassJunction:
    """A maintained intersection or union of class expressions."""

    operator: Literal["intersection", "union"]
    operands: tuple[ClassExpression, ...]


@dataclass(frozen=True)
class ClassComplement:
    """A class complement with its actual operand."""

    operand: ClassExpression


@dataclass(frozen=True)
class ObjectQuantifier:
    """An existential or universal object restriction."""

    quantifier: Literal["some", "all"]
    property: ObjectProperty
    filler: ClassExpression


@dataclass(frozen=True)
class ObjectCardinality:
    """An object cardinality with actual property, filler and qualification."""

    operator: Literal["min", "max", "exact"]
    count: int
    property: ObjectProperty
    filler: ClassExpression
    qualified: bool


@dataclass(frozen=True)
class ObjectValue:
    """A restriction to an actual named individual."""

    property: ObjectProperty
    value: NamedEntity


@dataclass(frozen=True)
class ObjectSelf:
    """An object self restriction."""

    property: ObjectProperty


@dataclass(frozen=True)
class ObjectEnumeration:
    """An enumeration of individuals, distinct from a literal data enumeration."""

    values: tuple[NamedEntity, ...]


@dataclass(frozen=True)
class DataQuantifier:
    """An existential or universal data restriction."""

    quantifier: Literal["some", "all"]
    property: DataProperty
    filler: DataRange


@dataclass(frozen=True)
class DataCardinality:
    """A data cardinality retaining its actual data range."""

    operator: Literal["min", "max", "exact"]
    count: int
    property: DataProperty
    filler: DataRange
    qualified: bool


@dataclass(frozen=True)
class DataValue:
    """A data has-value restriction on a native parsed literal."""

    property: DataProperty
    value: NativeLiteral


@dataclass(frozen=True)
class DataJunction:
    """An intersection or union of data ranges."""

    operator: Literal["intersection", "union"]
    operands: tuple[DataRange, ...]


@dataclass(frozen=True)
class DataComplement:
    """A data range complement."""

    operand: DataRange


@dataclass(frozen=True)
class DataEnumeration:
    """A literal enumeration, never an individual enumeration."""

    values: tuple[NativeLiteral, ...]


@dataclass(frozen=True)
class DatatypeFacet:
    """An actual facet IRI and native literal value, without evaluating it."""

    iri: str
    value: NativeLiteral


@dataclass(frozen=True)
class DatatypeRestriction:
    """A named datatype with its maintained facet restrictions."""

    datatype: NamedEntity
    facets: tuple[DatatypeFacet, ...]


ClassExpression: TypeAlias = (
    NamedEntity
    | ClassJunction
    | ClassComplement
    | ObjectQuantifier
    | ObjectCardinality
    | ObjectValue
    | ObjectSelf
    | ObjectEnumeration
    | DataQuantifier
    | DataCardinality
    | DataValue
)
DataRange: TypeAlias = (
    NamedEntity | DataJunction | DataComplement | DataEnumeration | DatatypeRestriction
)
PropertyExpression: TypeAlias = ObjectProperty | DataProperty


@dataclass(frozen=True)
class OwlAnnotation:
    """An actual recursively annotated annotation, with typed value."""

    property: str
    value: IriValue | NativeLiteral
    annotations: tuple[OwlAnnotation, ...]


@dataclass(frozen=True)
class Declaration:
    """An explicit entity declaration with its axiom annotations."""

    entity: NamedEntity
    annotations: tuple[OwlAnnotation, ...]


@dataclass(frozen=True)
class SubClassOf:
    """An asserted subclass axiom, without a subsumption or satisfiability claim."""

    sub_class: ClassExpression
    super_class: ClassExpression
    annotations: tuple[OwlAnnotation, ...]


@dataclass(frozen=True)
class ClassRelation:
    """An equivalent-classes or disjoint-classes axiom."""

    kind: Literal["equivalent", "disjoint"]
    classes: tuple[ClassExpression, ...]
    annotations: tuple[OwlAnnotation, ...]


@dataclass(frozen=True)
class ClassAssertion:
    """An asserted class expression on a named individual."""

    individual: NamedEntity
    expression: ClassExpression
    annotations: tuple[OwlAnnotation, ...]


@dataclass(frozen=True)
class DatatypeDefinition:
    """A maintained datatype definition, without interpreting its value space."""

    datatype: NamedEntity
    definition: DataRange
    annotations: tuple[OwlAnnotation, ...]


@dataclass(frozen=True)
class PropertyDomainRange:
    """A property domain/range with distinct object/data property expression."""

    kind: Literal["domain", "range"]
    property: PropertyExpression
    expression: ClassExpression | DataRange
    annotations: tuple[OwlAnnotation, ...]


@dataclass(frozen=True)
class SubPropertyOf:
    """A supported non-chain object/data subproperty axiom."""

    sub_property: PropertyExpression
    super_property: PropertyExpression
    annotations: tuple[OwlAnnotation, ...]


@dataclass(frozen=True)
class AnnotationAssertion:
    """An original annotation assertion on a named IRI subject."""

    property: str
    subject: IriValue
    value: IriValue | NativeLiteral
    annotations: tuple[OwlAnnotation, ...]


ProjectedAxiom: TypeAlias = (
    Declaration
    | SubClassOf
    | ClassRelation
    | ClassAssertion
    | DatatypeDefinition
    | PropertyDomainRange
    | SubPropertyOf
    | AnnotationAssertion
)


@dataclass(frozen=True)
class RdfParseMetadata:
    """Actual staged-RDF parser accounting, distinct from native syntax parsing."""

    present: bool
    unparsed: int | None
    guessed: int | None
    triples: int | None
    header: str | None


@dataclass(frozen=True)
class InspectedOntology:
    """One actually loaded original document and its observed ontology header."""

    source: OwlDocument | ProfileDocument
    ontology_iri: str | None
    version_iri: str | None
    imports: tuple[str, ...]
    axiom_count: int
    annotations: tuple[OwlAnnotation, ...]
    rdf_metadata: RdfParseMetadata | None


@dataclass(frozen=True)
class AxiomRecord:
    """One represented axiom attached to its actual loaded document."""

    document: InspectedOntology
    axiom: ProjectedAxiom


@dataclass(frozen=True)
class UnsupportedAxiom:
    """One selected axiom whose maintained object has no complete finite projection."""

    document: InspectedOntology
    kind: str


@dataclass(frozen=True)
class AxiomExclusion:
    """An explicitly unselected axiom family and its actual document count."""

    document: InspectedOntology
    kind: str
    count: int


@dataclass(frozen=True)
class AxiomInspection:
    """Owned finite inspection; its report qualifies only the explicit selection.

    No record proves consistency, satisfiability, inferred entailment or exact
    original RDF lexical preservation. Unrepresented selected axioms and input
    interpretation loss prevent a complete qualified projection.
    """

    documents: tuple[InspectedOntology, ...]
    axioms: tuple[AxiomRecord, ...]
    unsupported: tuple[UnsupportedAxiom, ...]
    exclusions: tuple[AxiomExclusion, ...]
    selected_types: tuple[str, ...] | None
    capability: BackendCapability | None
    report: Report
