"""Explicit local ShEx resources and named obligations for the installed command."""

import argparse
from pathlib import Path

from rdflib import URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.interfaces.cli.inputs import (
    command_limits,
    protect_inputs,
    rdf_input,
)
from bijux_ontology.interfaces.cli.results import CommandResult
from bijux_ontology.limits import Budget, bound_text
from bijux_ontology.shex.checking import validate_shex
from bijux_ontology.shex.types import ShexBinding, ShexSchema


def _schema(path: Path, iri: str, budget: Budget) -> ShexSchema:
    """Read actual UTF-8 schema text without interpreting its filename as a base."""
    bound_text(iri, budget)
    budget.consume("resources")
    maximum = min(budget.limits.max_bytes, budget.remaining("bytes"))
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise LimitExceeded("Supplied ShEx schema exceeds its byte allowance")
    budget.consume("bytes", len(raw))
    budget.consume("work", len(raw))
    return ShexSchema(iri, raw.decode("utf-8"))


def check_shex(options: argparse.Namespace) -> CommandResult:
    """Delegate native execution and prefix recovery to the owned ShEx operation."""
    if options.lookup or options.sources is not None or options.lookup_format:
        raise InputError("ShEx uses --schema-import for explicit supplied schemas")
    import_paths = tuple(Path(path) for _, path in options.schema_import)
    protect_inputs(options, import_paths)
    limits = command_limits(options)
    budget = Budget(limits)
    schema = _schema(options.schema, options.schema_iri, budget)
    supplied: dict[str, ShexSchema] = {}
    for (iri, _), path in zip(options.schema_import, import_paths, strict=True):
        bound_text(iri, budget)
        budget.consume("states")
        if iri in supplied:
            raise InputError("Each ShEx import IRI requires one explicit local file")
        supplied[iri] = _schema(path, iri, budget)
    data = prepare_source(
        rdf_input(
            options.ontology, format=options.format, base=options.base, limits=limits
        ),
        budget=budget,
    )
    bindings = []
    for focus, shape in options.binding:
        bound_text(focus, budget)
        bound_text(shape, budget)
        budget.consume("states")
        bindings.append(ShexBinding(URIRef(focus), URIRef(shape)))
    result = validate_shex(
        data,
        schema,
        bindings=tuple(bindings),
        imports=supplied,
        graph_name=options.graph,
        union=options.union,
        budget=budget,
        timeout=options.timeout if options.timeout is not None else 30.0,
    )
    return CommandResult(
        result.report,
        {
            "backend_version": result.backend_version,
            "diagnostics": result.diagnostics,
            "schema_iri": result.schema_iri,
            "imports": result.imports,
            "graph_scope": result.graph_scope,
            "obligations": [
                {
                    "focus": str(item.binding.focus),
                    "shape": str(item.binding.shape),
                    "status": item.status,
                    "native": item.native,
                }
                for item in result.obligations
            ],
        },
    )
