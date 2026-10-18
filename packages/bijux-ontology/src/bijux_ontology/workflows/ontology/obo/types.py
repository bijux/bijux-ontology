"""Explicit application selection of finite OBO-in-OWL publication conventions."""

from dataclasses import dataclass

from rdflib import URIRef

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.annotations.types import (
    AnnotationCoverage,
    AnnotationRequirement,
)
from bijux_ontology.vocabulary.obo.subsets.types import OboSubsets
from bijux_ontology.vocabulary.obo.types import OboRecords
from bijux_ontology.vocabulary.results import Edge, Selection
from bijux_ontology.workflows.upgrades.lifecycle.types import (
    LifecycleCheck,
    LifecyclePolicy,
    Release,
)


@dataclass(frozen=True)
class OboPublicationProfile:
    """Choose requirements without imposing OBO conventions on other ontologies.

    record_annotations checks present definitions and four synonym roles using
    the native OBO-in-OWL reader. definition_xrefs requires a nonempty text dbxref
    in every attached definition annotation; presence/count/language requirements
    are separate ordinary annotation contracts. synonym_types, when supplied,
    requires exactly one allowed named type in each synonym attachment.
    subset_membership checks only selected terms' references and their supplied
    definitions. Lifecycle policy covers only the explicitly supplied interval.
    Exclusions are caller declarations; namespace prefixes never infer ownership.
    """

    annotations: tuple[AnnotationRequirement, ...] = ()
    record_annotations: bool = False
    definition_xrefs: bool = False
    synonym_types: tuple[str, ...] | None = None
    subset_membership: bool = False
    lifecycle: LifecyclePolicy | None = None
    exclude_terms: tuple[str, ...] = ()
    exclude_namespaces: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Require immutable typed selections and explicit prerequisite rules."""
        if any(
            type(value) is not bool
            for value in (
                self.record_annotations,
                self.definition_xrefs,
                self.subset_membership,
            )
        ):
            raise InputError("Publication switches require explicit booleans")
        operation = Budget()
        if type(self.annotations) is not tuple:
            raise InputError("Publication annotations require immutable contracts")
        operation.consume("states", len(self.annotations))
        if any(
            not isinstance(rule, AnnotationRequirement) for rule in self.annotations
        ):
            raise InputError("Publication annotations require immutable contracts")
        if self.lifecycle is not None and not isinstance(
            self.lifecycle, LifecyclePolicy
        ):
            raise InputError("Publication lifecycle requires LifecyclePolicy")
        for values in (
            self.exclude_terms,
            self.exclude_namespaces,
            self.synonym_types if self.synonym_types is not None else (),
        ):
            if type(values) is not tuple:
                raise InputError("Publication selections require immutable tuples")
            operation.consume("states", len(values))
            for value in values:
                if type(value) is not str:
                    raise InputError("Publication identifiers require IRI text")
                bound_text(value, operation)
                absolute_iri(value)
            if len(set(values)) != len(values):
                raise InputError("Publication selections must be unique")
        if (
            self.definition_xrefs or self.synonym_types is not None
        ) and not self.record_annotations:
            raise InputError("Qualified-value rules require record_annotations")


@dataclass(frozen=True)
class OboPublication:
    """Selected checks retain original scoped records beside their policy result."""

    release: Release
    terms: tuple[URIRef, ...]
    excluded: tuple[URIRef, ...]
    records: tuple[OboRecords, ...]
    annotations: Selection[AnnotationCoverage] | None
    subsets: OboSubsets | None
    subset_evidence: tuple[Edge, ...]
    lifecycle: LifecycleCheck | None
    report: Report
