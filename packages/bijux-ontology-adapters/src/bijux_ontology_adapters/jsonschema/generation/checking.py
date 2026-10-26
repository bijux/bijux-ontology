"""Compile explicit output contracts through owned native projection stages."""

import json
from collections.abc import Sequence
from urllib.parse import urldefrag

from rdflib import BNode, Literal

from bijux_ontology.declarations.types import (
    ModelDeclaration,
)
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import (
    Budget,
    Limits,
    bound_text,
)
from bijux_ontology.rdf.identifiers import absolute_iri

from .preparation import bound_contract
from .projection.models import discriminated_roots, project_model
from .projection.state import SchemaProjection
from .types import CompiledOutputSchema, JsonRepresentation
from .validation import DIALECT, backend, capture, operation, owned_json, validator


def compile_output_schema(
    models: Sequence[ModelDeclaration],
    roots: Sequence[str],
    *,
    schema_iri: str,
    representations: Sequence[JsonRepresentation] = (),
    profile: str = "native-jsonld",
    labels: Sequence[tuple[str, str]] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> CompiledOutputSchema:
    """Compile an explicitly selected JSON representation with qualified losses.

    The profile is authored intent, not inferred universal serializer behavior.
    General anyOf schemas remain usable but are outside the import adapter's
    finite discriminated-oneOf subset. Linked mappings keep RDF meaning explicit.
    """
    backend()
    selected = operation(limits, budget)
    compiler = SchemaProjection(selected)
    schema_json = None
    annotations_json = "{}"
    complete = False
    try:
        if not isinstance(schema_iri, str) or isinstance(schema_iri, BNode | Literal):
            raise InputError("Schema resource identifier requires a named IRI")
        bound_text(schema_iri, selected)
        absolute_iri(schema_iri)
        if urldefrag(schema_iri).fragment:
            raise InputError(
                "Generated schema resource identifiers cannot have fragments"
            )
        bound_text(profile, selected)
        compiler.iri, compiler.profile = schema_iri, profile
        for model in capture(models, selected):
            if not isinstance(model, ModelDeclaration):
                raise InputError("Expected explicit ModelDeclaration output contracts")
            bound_contract(model, selected)
            if model.identifier in compiler.models:
                raise InputError("Generated model identifiers must be unique")
            compiler.models[model.identifier] = model
        requested = capture(roots, selected)
        if not requested:
            raise InputError("Select at least one output root model")
        for name in requested:
            bound_text(name, selected)
            if name not in compiler.models:
                raise InputError("Selected root model is not supplied")
        for item in capture(representations, selected):
            if not isinstance(item, JsonRepresentation):
                raise InputError("Expected an explicit JSON representation")
            bound_contract(item, selected)
            key = (item.model, item.field)
            if item.model in compiler.models:
                selected.consume("work", len(compiler.models[item.model].fields))
            if (
                item.model not in compiler.models
                or item.field
                not in {field.name for field in compiler.models[item.model].fields}
                or key in compiler.representations
            ):
                raise InputError(
                    "Representation selects an absent or duplicate output field"
                )
            compiler.representations[key] = item.shape
        for pair in capture(labels, selected):
            captured = capture(pair, selected)
            if len(captured) != 2:
                raise InputError("Ontology labels require IRI and text pairs")
            iri, label = captured
            if not isinstance(iri, str) or isinstance(iri, BNode | Literal):
                raise InputError("Ontology labels require named IRIs")
            bound_text(iri, selected)
            absolute_iri(iri)
            bound_text(label, selected)
            if iri in compiler.labels:
                raise InputError("Ontology annotation labels must be unique")
            compiler.labels[iri] = label
        if profile != "native-jsonld":
            compiler.record(
                "output",
                None,
                "serializer profile",
                "A custom serializer has no qualified universal native output profile",
                supported=False,
            )
        for name in requested:
            project_model(compiler, name, frozenset())
        refs = [{"$ref": compiler.reference(name)} for name in requested]
        schema: dict[str, object] = {
            "$schema": DIALECT,
            "$id": schema_iri,
            "$defs": compiler.definitions,
        }
        if len(refs) == 1:
            schema.update(refs[0])
        else:
            schema["oneOf" if discriminated_roots(compiler, requested) else "anyOf"] = (
                refs
            )
        owned, _ = owned_json(schema, selected)
        validator(owned, selected)
        schema_json = json.dumps(
            owned, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        selected.consume("bytes", len(schema_json.encode("utf-8")))
        annotations, _ = owned_json(
            {
                "schema": schema_iri,
                "models": compiler.annotations,
                "labels": compiler.labels,
            },
            selected,
        )
        annotations_json = json.dumps(
            annotations, ensure_ascii=False, separators=(",", ":")
        )
        complete = True
    except (LimitExceeded, MemoryError, RecursionError):
        schema_json = None
        compiler.issues.exhausted("JSON output schema compilation exceeded its limits")
    except (InputError, ValueError, TypeError):
        schema_json = None
        try:
            compiler.issues.append(
                Issue(
                    "schema.translation_invalid",
                    "Output contracts or representations could not be compiled",
                    kind="input_error",
                )
            )
        except LimitExceeded:
            compiler.issues.exhausted(
                "JSON output schema diagnostics exceeded their limit"
            )
    return CompiledOutputSchema(
        schema_json,
        annotations_json,
        tuple(compiler.mappings) if complete else (),
        tuple(compiler.items),
        Report(
            "jsonschema.translation",
            len(compiler.items),
            tuple(compiler.issues),
            requested=max(1, len(compiler.items) + int(not complete)),
            complete=complete,
            children=tuple(compiler.children),
        ),
    )
