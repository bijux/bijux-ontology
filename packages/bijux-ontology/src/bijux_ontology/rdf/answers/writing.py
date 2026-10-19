"""Bounded maintained result serialization with explicitly lossy CSV output."""

import json
from threading import Event
from typing import cast

from rdflib import BNode, Literal, URIRef

from bijux_ontology._json import object_value
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.reports import Issue, Report

from .reading import _dispatch
from .types import AnswerArtifact, AnswerFormat, AnswerSerialization, AnswerUnavailable


def write_answers(
    artifact: AnswerArtifact,
    *,
    format: AnswerFormat,
    allow_lossy: bool = False,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 10,
    cancel: Event | None = None,
) -> AnswerSerialization:
    """Serialize exact input without upgrading external execution evidence.

    CSV is available only by explicit opt-in. Its output retains an unverified
    identity-loss diagnostic and cannot be read back as an exact answer proof.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    losses: tuple[str, ...] = ()
    payload = None
    try:
        if type(artifact) is not AnswerArtifact or not artifact.report.verified:
            raise InputError("Serialization requires a complete owned answer artifact")
        if not isinstance(cast(object, format), str) or format not in {
            "json",
            "xml",
            "tsv",
            "csv",
        }:
            raise InputError("Unsupported answer serialization format")
        if type(allow_lossy) is not bool:
            raise InputError("allow_lossy requires a boolean")
        if artifact.kind == "ASK" and format in {"csv", "tsv"}:
            raise InputError("Tabular answer formats support SELECT only")
        if format == "csv":
            if not allow_lossy:
                raise InputError("CSV requires explicit allow_lossy=True")
            losses = ("rdf-term-kinds", "literal-metadata", "unbound-versus-empty")
            issues.append(
                Issue(
                    "rdf.answer_identity_lost",
                    "CSV omits RDF term identity and unbound distinctions",
                    kind="unverified",
                )
            )
        root: dict[str, object] = {"head": {}}
        if artifact.kind == "ASK":
            root["boolean"] = artifact.answer
        else:
            variables = artifact.variables
            for variable in variables:
                bound_text(variable, operation)
            root["head"] = {"vars": variables}
            rows = []
            blanks: dict[str, str] = {}
            for row in artifact.rows:
                operation.consume("states", len(row) + 1)
                operation.consume("bytes", 128 * (len(row) + 1))
                cells: dict[str, object] = {}
                for variable, term in zip(variables, row, strict=True):
                    operation.consume("work")
                    if term is None:
                        continue
                    bound_node(term, operation)
                    if isinstance(term, URIRef):
                        value = {"type": "uri", "value": str(term)}
                    elif isinstance(term, BNode):
                        label = blanks.setdefault(str(term), "b" + str(len(blanks)))
                        value = {"type": "bnode", "value": label}
                    elif isinstance(term, Literal):
                        value = {"type": "literal", "value": str(term)}
                        if term.language:
                            value["xml:lang"] = term.language
                        elif term.datatype:
                            value["datatype"] = str(term.datatype)
                    else:
                        raise InputError("Unsupported answer term")
                    cells[variable] = value
                rows.append(cells)
            root["results"] = {"bindings": rows}
        size = json_bound(root, operation)
        operation.consume("bytes", size * 2)
        text = json.dumps(root, ensure_ascii=True, separators=(",", ":"))
        data = object_value(
            _dispatch(text, format, operation, mode="write"), location="serialization"
        )
        if set(data) != {"serialized"} or not isinstance(data["serialized"], str):
            raise InputError("Malformed answer serialization response")
        bound_text(data["serialized"], operation)
        operation.consume("bytes", len(data["serialized"]) * 4)
        payload = data["serialized"].encode("utf-8")
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(
                Issue(
                    "rdf.answer_output_unverified"
                    if isinstance(error, AnswerUnavailable)
                    else "rdf.answer_input",
                    str(error),
                    kind="unverified"
                    if isinstance(error, AnswerUnavailable)
                    else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return AnswerSerialization(
        format,
        payload,
        losses,
        Report(
            "rdf.answers.write",
            int(payload is not None),
            tuple(issues),
            requested=1,
            complete=payload is not None and not losses,
        ),
    )
