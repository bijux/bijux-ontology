"""Coherent explicit SKOS creation, label promotion, membership and deprecation."""

from __future__ import annotations

from typing import cast

from rdflib import Literal, URIRef
from rdflib.namespace import OWL, RDF, SKOS

from bijux_ontology.absence import ABSENT
from bijux_ontology.rdf.patches.planning import Rejected
from bijux_ontology.rdf.patches.types import PatchLiteral, PatchResource

from .types import (
    CreateSkosResource,
    DeprecateResource,
    PreferredLabelChange,
    SchemeMembership,
    VocabularyEditContext,
    _iri,
    _label,
)


def _require_role(
    state: VocabularyEditContext, subject: PatchResource, *roles: URIRef
) -> None:
    """Use selected explicit types, including declarations authored in this overlay."""
    kinds = state.values(subject, RDF.type)
    if not any(role in kinds for role in roles):
        raise Rejected(
            "vocabulary.authoring_rejected",
            "The selected resource lacks its required explicit SKOS role",
        )


def change_skos(
    state: VocabularyEditContext,
    edit: CreateSkosResource
    | PreferredLabelChange
    | SchemeMembership
    | DeprecateResource,
) -> None:
    """Apply only the structural consequences explicitly selected by typed intent."""
    operation = state.operation
    if isinstance(edit, SchemeMembership):
        _iri(edit.concept, operation)
        _iri(edit.scheme, operation)
        concept = cast(PatchResource, state.ref(edit.concept))
        scheme = cast(PatchResource, state.ref(edit.scheme))
        _require_role(state, concept, SKOS.Concept)
        _require_role(state, scheme, SKOS.ConceptScheme)
        (state.put if edit.member else state.drop)(concept, SKOS.inScheme, scheme)
        for subject, predicate, related in (
            (concept, SKOS.topConceptOf, scheme),
            (scheme, SKOS.hasTopConcept, concept),
        ):
            (state.put if edit.top else state.drop)(subject, predicate, related)
        return
    _iri(edit.subject, operation)
    subject = cast(PatchResource, state.ref(edit.subject))
    if isinstance(edit, CreateSkosResource):
        if state.outgoing(subject):
            raise Rejected(
                "vocabulary.authoring_rejected",
                "Creation cannot overwrite an existing selected resource",
            )
        state.put(
            subject,
            RDF.type,
            SKOS.Concept if edit.kind == "concept" else SKOS.ConceptScheme,
        )
    elif isinstance(edit, PreferredLabelChange):
        _require_role(state, subject, SKOS.Concept, SKOS.ConceptScheme)
        _label(edit.replacement, operation)
        replacement = state.ref(edit.replacement)
        existing = state.values(subject, SKOS.prefLabel)
        if edit.previous is ABSENT:
            language = (edit.replacement.language or "").lower()
            if any(
                isinstance(value, PatchLiteral)
                and (value.language or "").lower() == language
                for value in existing
            ):
                raise Rejected(
                    "vocabulary.authoring_rejected",
                    "A preferred label already exists in the selected language",
                )
        else:
            _label(edit.previous, operation)
            previous = state.ref(edit.previous)
            if previous not in existing:
                raise Rejected(
                    "vocabulary.authoring_rejected",
                    "The exact preferred label before-state is absent",
                )
            if previous == replacement:
                return
            state.drop(subject, SKOS.prefLabel, previous)
            if edit.demote_previous:
                state.put(subject, SKOS.altLabel, previous)
        state.put(subject, SKOS.prefLabel, replacement)
    else:
        if not state.outgoing(subject):
            raise Rejected(
                "vocabulary.authoring_rejected",
                "Deprecation requires an existing selected resource",
            )
        expected = () if edit.previous is ABSENT else (state.ref(edit.previous),)
        actual = state.values(subject, OWL.deprecated)
        if set(expected) != set(actual):
            raise Rejected(
                "vocabulary.authoring_rejected",
                "Deprecation differs from its exact before-state",
            )
        for value in actual:
            state.drop(subject, OWL.deprecated, value)
        state.put(subject, OWL.deprecated, state.ref(Literal(edit.value)))
