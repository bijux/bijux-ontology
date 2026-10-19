"""Local OBOGraphs JSON validated against the unchanged upstream Draft 3 schema.

Schema provenance: geneontology/obographs, BSD-3-Clause (upstream pom.xml),
commit 459a44e45b56188912bf7272239c984d94b1af4a, schema/obographs-schema.json.
The bundled bytes are unchanged; their digest qualifies the offline resource.
"""

import hashlib
import json
from importlib.resources import files

from rdflib import URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.budgets import operation
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.json.schema import backend
from bijux_ontology.json.values import _decode_json, array_value, owned_json

from .projection import (
    ImportProjection,
    UnsupportedProjection,
    keys,
    literals,
    named,
    obj,
)
from .types import OboGraphsDocument

_SCHEMA_SIZE = 9765
_SCHEMA_HASH = "a54a564ff47014b626003e54e6a61392b65be7301939d878cc658d4186e88165"


def _operation(limits: Limits | None, budget: Budget | None) -> Budget:
    """Keep one valid caller allowance across source, validation and mapping."""
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("OBOGraphs limits require Limits")
    return operation(limits, budget)


def _validate(document: object, budget: Budget) -> None:
    """Run the maintained Draft 3 implementation with actual local URN resolution."""
    import jsonschema  # type: ignore[import-untyped]
    from referencing import Resource
    from referencing.jsonschema import DRAFT3

    budget.consume("resources")
    budget.consume("bytes", 4 * _SCHEMA_SIZE)
    budget.consume("work", 4 * _SCHEMA_SIZE)
    with files(__package__).joinpath("schema.json").open("rb") as handle:
        schema_bytes = handle.read(_SCHEMA_SIZE + 1)
    if (
        len(schema_bytes) != _SCHEMA_SIZE
        or hashlib.sha256(schema_bytes).hexdigest() != _SCHEMA_HASH
    ):
        raise UnsupportedProjection(
            "The pinned OBOGraphs schema resource is unavailable or changed"
        )
    schema = json.loads(schema_bytes)
    _, size = owned_json(document, budget)
    budget.consume("work", 32 * size)
    budget.consume("states", 4 * size)

    def deny(uri: str) -> object:
        """The pinned schema's internal URNs are the whole permitted registry."""
        raise InputError("OBOGraphs schema cannot fetch an external reference")

    _, registry_factory, _ = backend()
    registry = (
        registry_factory(retrieve=deny)
        .with_resource(
            schema["id"], Resource.from_contents(schema, default_specification=DRAFT3)
        )
        .crawl()
    )
    jsonschema.Draft3Validator.check_schema(schema)
    validator = jsonschema.Draft3Validator(schema, registry=registry)
    if next(validator.iter_errors(document), None) is not None:
        raise InputError("Supplied JSON violates the official OBOGraphs schema")
    budget.consume("work", 0)


def read_obo_graphs(
    content: bytes | str,
    *,
    graph_id: str,
    literal_predicates: tuple[URIRef, ...] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> OboGraphsDocument:
    """Read one explicit ontology graph without Java, context fetching or inference.

    Class nodes and declared object-property edges become subclass/existential
    axioms. Generic metadata values require caller-selected plain-literal
    predicates; unsupported RDF term ambiguity is not guessed. Original JSON is
    retained, while a partial or unsupported mapping publishes no checked source.
    """
    owned = _operation(limits, budget)
    issues = IssueCollector(owned)
    payload = b""
    selected_id = None
    source = None
    checked = 0
    try:
        if type(content) not in (bytes, str):
            raise InputError("OBOGraphs requires supplied JSON bytes or text")
        if len(content) > owned.limits.max_bytes:
            raise LimitExceeded("OBOGraphs source exceeds max_bytes")
        if isinstance(content, str):
            owned.consume("work", len(content) + 1)
            owned.consume("bytes", 4 * len(content))
        encoded = content.encode("utf-8") if isinstance(content, str) else content
        if len(encoded) > owned.limits.max_bytes:
            raise LimitExceeded("OBOGraphs encoded source exceeds max_bytes")
        owned.consume("bytes", 6 * len(encoded))
        owned.consume("work", 6 * len(encoded))
        payload = encoded
        bound_text(graph_id, owned)
        identifier = named(graph_id)
        selected_literals = literals(literal_predicates, owned)

        def number(token: str, _path: tuple[str | int, ...], integer: bool) -> object:
            """Never round an unsupported fractional source token through a float."""
            if not integer:
                raise UnsupportedProjection(
                    "Fractional source metadata is "
                    "outside the supported string/boolean profile"
                )
            owned.consume("work", len(token))
            return int(token)

        document = obj(
            _decode_json(
                payload,
                max_bytes=owned.limits.max_bytes,
                max_depth=owned.limits.max_depth,
                number_hook=number,
                budget=owned,
            )
        )
        _validate(document, owned)
        keys(document, {"graphs"})
        matches = [
            obj(item)
            for item in array_value(document.get("graphs"))
            if obj(item).get("id") == graph_id
        ]
        if len(matches) != 1:
            raise InputError(
                "Graph selection requires exactly one matching authored ID"
            )
        projection = ImportProjection(owned, selected_literals)
        graph = projection.convert(matches[0], identifier)
        source = prepare_source(graph, budget=owned)
        owned.consume("work", 0)
        selected_id = str(identifier)
        checked = 1
    except (ModuleNotFoundError, FileNotFoundError):
        try:
            issues.append(
                Issue(
                    "adapter.obographs_unverified",
                    "Install "
                    "bijux-ontology[jsonschema] with its bundled OBOGraphs schema",
                    kind="unverified",
                )
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining the unavailable schema capability")
    except UnsupportedProjection as error:
        try:
            issues.append(
                Issue("adapter.obographs_unverified", str(error), kind="unverified")
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining the unsupported source feature")
    except (LimitExceeded, MemoryError, RecursionError) as error:
        issues.exhausted(str(error))
    except (InputError, ValueError, TypeError, UnicodeError):
        try:
            issues.append(
                Issue(
                    "adapter.obographs_input_error",
                    "The supplied graph, metadata or explicit role contract is invalid",
                    kind="input_error",
                )
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining OBOGraphs input failure")
    report = Report(
        "adapter.obo_graphs_import",
        checked,
        tuple(issues),
        requested=1,
        complete=checked == 1,
        regime="obographs-selected-class-axiom-meaning",
    )
    return OboGraphsDocument(
        payload, selected_id, source if report.verified else None, report
    )
