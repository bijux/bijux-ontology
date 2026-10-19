"""Actual finite public checks inside the existing owned-process lifetime."""

import base64
import json
import sys
from typing import cast

from rdflib import BNode, Graph, URIRef

from bijux_ontology.declarations.codecs import models_from_data
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import ControlBudget
from bijux_ontology.execution.limits import IssueCollector, Limits, retain_report
from bijux_ontology.json.paths import at_path
from bijux_ontology.json.snapshots import serialized, snapshot
from bijux_ontology.json.values import decode_json, object_value
from bijux_ontology.jsonld.contracts.codecs import expectations_from_data
from bijux_ontology.jsonld.processing.resources import (
    OfflineDocument,
    OfflineDocumentLoader,
)
from bijux_ontology.jsonld.usages.checking import check_context, prepare_usages
from bijux_ontology.models.emission.checking import check_emission
from bijux_ontology.models.emission.codecs import usages_from_data
from bijux_ontology.rdf.records.codecs import read_term, read_witness
from bijux_ontology.rdf.records.materialization import materialize
from bijux_ontology.rdf.records.types import PatchBlank
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.sources.types import RdfSource
from bijux_ontology.shacl.checking import validate_shapes
from bijux_ontology.skos.checking import check_skos

from .preparation import reserve
from .types import ReproductionUnavailable, SourceObligation


def _text(value: object) -> str:
    if type(value) is not str:
        raise InputError("Malformed reproduction text field")
    return value


def _array(value: object) -> list[object]:
    if type(value) is not list:
        raise InputError("Malformed reproduction inventory")
    return value


def _source(
    value: object, budget: ControlBudget
) -> tuple[PreparedSource, dict[str, BNode]]:
    witness = read_witness(value, budget)
    original = prepare_rdf(Graph(), require_complete_graphs=True, budget=budget)
    bindings: dict[str, BNode] = {}
    result = materialize(witness, original, budget, bindings=bindings)
    return result, bindings


def _loader(value: object, budget: ControlBudget) -> OfflineDocumentLoader:
    documents = {}
    for raw in _array(value):
        resource = object_value(raw, location="JSON response")
        if set(resource) != {"iri", "document_json", "effective_url", "context_url"}:
            raise InputError("Malformed owned JSON response")
        reserve(budget)
        content = decode_json(
            _text(resource["document_json"]),
            max_bytes=budget.limits.max_bytes,
            max_depth=budget.limits.max_depth,
        )
        documents[_text(resource["iri"])] = OfflineDocument(
            content,
            None
            if resource["effective_url"] is None
            else _text(resource["effective_url"]),
            None if resource["context_url"] is None else _text(resource["context_url"]),
        )
    return OfflineDocumentLoader(documents, limits=budget.limits)


def _source_obligations(
    document: object,
    paths: list[tuple[str | int, ...]],
    budget: ControlBudget,
    loader: OfflineDocumentLoader,
    base: str | None,
) -> tuple[SourceObligation, ...]:
    prepared = prepare_usages(
        document, document_loader=loader, base=base, budget=budget
    )
    result = []
    for path in paths:
        observed = prepared.observe(path)
        reserve(budget, len(observed.occurrences) * 2 + 1)
        subjects = tuple(
            sorted(
                {
                    item.subject
                    for item in observed.occurrences
                    if item.subject is not None
                }
            )
        )
        graphs = tuple(
            sorted(
                {item.graph for item in observed.occurrences},
                key=lambda value: value or "",
            )
        )
        _, raw = snapshot(at_path(document, path), budget)
        named = bool(subjects) and all(
            item.subject is not None
            and not item.subject.startswith("_:")
            and item.subject_identity is None
            and not item.anonymous_graph
            for item in observed.occurrences
        )
        result.append(SourceObligation(path, subjects, graphs, raw, named))
    return tuple(result)


def _json_operation(
    request: dict[str, object], budget: ControlBudget
) -> tuple[Report, tuple[SourceObligation, ...]]:
    kind = request["kind"]
    keys = {"kind", "document", "base", "resources"} | (
        {"expectations"} if kind == "context" else {"models", "usages"}
    )
    if set(request) != keys:
        raise InputError("Malformed public JSON operation fields")
    document = decode_json(
        _text(request["document"]),
        max_bytes=budget.limits.max_bytes,
        max_depth=budget.limits.max_depth,
    )
    loader = _loader(request["resources"], budget)
    base = request["base"]
    if base is not None:
        base = _text(base)
    paths: list[tuple[str | int, ...]] = []
    if kind == "context":
        expectations = expectations_from_data(
            decode_json(
                _text(request["expectations"]),
                max_bytes=budget.limits.max_bytes,
                max_depth=budget.limits.max_depth,
            )
        )
        report = check_context(
            document, expectations, document_loader=loader, base=base, budget=budget
        )
        paths.extend(expectation.path for expectation in expectations)
        return report, _source_obligations(
            document, paths, budget, loader, base
        ) if report.complete else ()
    models = models_from_data(
        decode_json(
            _text(request["models"]),
            max_bytes=budget.limits.max_bytes,
            max_depth=budget.limits.max_depth,
        )
    )
    usages = usages_from_data(
        decode_json(
            _text(request["usages"]),
            max_bytes=budget.limits.max_bytes,
            max_depth=budget.limits.max_depth,
        )
    )
    report = check_emission(
        document, models, usages, document_loader=loader, base=base, budget=budget
    )
    if not report.complete:
        return report, ()
    prepared = prepare_usages(
        document, document_loader=loader, base=base, budget=budget
    )
    registry = {model.identifier: model for model in models}
    obligations = []
    for usage in usages:
        raw = at_path(document, usage.path)
        if not isinstance(raw, dict):
            raise InputError("Model source binding must select an object")
        if usage.path:
            occurrences = prepared.observe(usage.path).occurrences
            identifiers = tuple(
                sorted(
                    {
                        item.traits.identifier
                        for item in occurrences
                        if item.traits.identifier is not None
                    }
                )
            )
            graphs = tuple(
                sorted(
                    {item.graph for item in occurrences}, key=lambda value: value or ""
                )
            )
        else:
            identifiers = tuple(
                sorted(
                    {
                        trait.identifier
                        for trait in prepared.root_nodes
                        if trait.identifier is not None
                    }
                )
            )
            graphs = (None,)
        reserve(budget, len(identifiers) + 1)
        obligations.append(
            SourceObligation(
                usage.path,
                identifiers,
                graphs,
                None,
                bool(identifiers)
                and all(not name.startswith("_:") for name in identifiers),
            )
        )
        for field in registry[usage.model].fields:
            key = field.key or field.name
            if key in raw:
                paths.append((*usage.path, key))
    obligations.extend(_source_obligations(document, paths, budget, loader, base))
    return report, tuple(obligations)


def _roots(
    value: object, bindings: dict[str, BNode], budget: ControlBudget
) -> tuple[URIRef | BNode, ...]:
    result: list[URIRef | BNode] = []
    for raw in _array(value):
        term = read_term(raw, budget)
        if isinstance(term, PatchBlank):
            if term.token not in bindings:
                raise InputError("Required owned blank selector is absent")
            result.append(bindings[term.token])
        elif isinstance(term, URIRef):
            result.append(term)
        else:
            raise InputError(
                "Required roots need explicit named or owned blank resources"
            )
    return tuple(result)


def _finding(value: object, aliases: dict[str, str], budget: ControlBudget) -> object:
    """Discard generated report IDs, retaining owned authored term correspondences."""
    reserve(budget)
    if isinstance(value, dict):
        if value.get("kind") == "blank" and isinstance(value.get("value"), str):
            token = aliases.get(value["value"])
            if token is None:
                raise ReproductionUnavailable(
                    "SHACL anonymous finding identity lacks an owned correspondence"
                )
            return {"kind": "blank", "value": token}
        return {
            key: _finding(item, aliases, budget)
            for key, item in value.items()
            if key not in {"result", "details", "detail_nodes", "messages", "locations"}
        }
    if isinstance(value, list):
        return [_finding(item, aliases, budget) for item in value]
    return value


def _rdf_operation(
    request: dict[str, object], budget: ControlBudget
) -> tuple[Report, str | None]:
    if request["kind"] == "skos":
        if set(request) != {"kind", "source", "concepts", "rules"}:
            raise InputError("Malformed SKOS reproduction request")
        source, _ = _source(request["source"], budget)
        concepts = tuple(map(_text, _array(request["concepts"])))
        rules = tuple(map(_text, _array(request["rules"])))
        return check_skos(
            source.graph(), concepts=concepts, rules=rules, budget=budget
        ), None
    if set(request) != {"kind", "data", "shapes", "focus", "roots", "imports"}:
        raise InputError("Malformed SHACL reproduction request")
    data, data_blanks = _source(request["data"], budget)
    shapes, shape_blanks = _source(request["shapes"], budget)
    focus, roots = (
        _roots(request["focus"], data_blanks, budget),
        _roots(request["roots"], shape_blanks, budget),
    )
    imports = {}
    for raw in _array(request["imports"]):
        item = object_value(raw, location="RDF response")
        if set(item) != {"iri", "content", "format", "document_iri"}:
            raise InputError("Malformed owned RDF response")
        content = base64.b64decode(_text(item["content"]), validate=True)
        budget.consume("bytes", len(content))
        imports[_text(item["iri"])] = RdfSource(
            content,
            format=_text(item["format"]),
            document_iri=cast(str | None, item["document_iri"]),
        )
    result = validate_shapes(
        data,
        shapes,
        mode="pairs",
        focus_nodes=focus,
        use_shapes=roots,
        imports=imports,
        budget=budget,
        _owned_worker=True,
    )
    aliases = {
        str(node): role + ":" + token
        for role, bindings in (("data", data_blanks), ("shapes", shape_blanks))
        for token, node in bindings.items()
    }
    try:
        normalized = []
        if result.report.complete:
            for finding in result.findings:
                if (
                    finding.path_expression is not None
                    and finding.path_expression.kind != "predicate"
                ):
                    raise ReproductionUnavailable(
                        "SHACL reduction requires direct named predicate paths"
                    )
                normalized.append(
                    snapshot(_finding(finding.to_dict(), aliases, budget), budget)[1]
                )
        budget.consume(
            "work",
            (sum(map(len, normalized)) + 1) * max(1, len(normalized).bit_length()),
        )
        reserve(budget, len(normalized))
        budget.consume("bytes", 4 * sum(map(len, normalized)) + 2 + len(normalized))
        finding_json = "[" + ",".join(sorted(normalized)) + "]"
        return result.report, finding_json
    except ReproductionUnavailable as error:
        issues = IssueCollector(budget)
        retain_report(result.report, budget, issues)
        issues.append(
            Issue(
                "reproduction.shacl_identity_unverified", str(error), kind="unverified"
            )
        )
        return Report(
            "reproduction.shacl_identity",
            checked=result.report.checked,
            requested=result.report.requested,
            complete=False,
            children=(result.report,),
            issues=tuple(issues),
        ), None


def execute(
    request: dict[str, object], budget: ControlBudget
) -> tuple[Report, tuple[SourceObligation, ...], str | None]:
    """Invoke public native checks; unexpected failures retain their traceback."""
    if request.get("kind") in ("context", "emission"):
        report, obligations = _json_operation(request, budget)
        return report, obligations, None
    if request.get("kind") in ("skos", "shacl"):
        report, findings = _rdf_operation(request, budget)
        return report, (), findings
    raise InputError("Unsupported reproduction operation")


def main() -> None:
    """Read a finite request, account native controls and return raw evidence."""
    capacity = int(sys.argv[1])
    raw = sys.stdin.buffer.read(capacity + 1)
    if len(raw) > capacity:
        raise LimitExceeded("Reproduction input exceeds protocol capacity")
    envelope = object_value(
        decode_json(raw, max_bytes=capacity), location="reproduction envelope"
    )
    if set(envelope) != {"request", "limits", "allowances"}:
        raise InputError("Malformed reproduction envelope")
    limits = Limits(
        **cast(dict[str, int], object_value(envelope["limits"], location="limits"))
    )
    budget = ControlBudget(limits, envelope["allowances"])
    issues = IssueCollector(budget)
    report = None
    obligations: tuple[SourceObligation, ...] = ()
    findings = None
    try:
        report, obligations, findings = execute(
            object_value(envelope["request"], location="request"), budget
        )
        retain_report(report, budget, issues)
        reserve(budget, len(obligations) * 8 + 1)
        pending = [report]
        while pending:
            current = pending.pop()
            reserve(
                budget,
                32
                + 32 * len(current.issues)
                + len(current.children)
                + len(current.exclusions)
                + len(current.families),
            )
            pending.extend(current.children)
            for issue in current.issues:
                if issue.evidence_json is not None:
                    reserve(budget, 2 * len(issue.evidence_json))
                if issue.details is not None:
                    reserve(budget, 8 + len(issue.details.path))
        for obligation in obligations:
            reserve(
                budget,
                len(obligation.path)
                + len(obligation.subjects)
                + len(obligation.graphs),
            )
    except LimitExceeded as error:
        if report is not None:
            issues.retain_completed(report)
        issues.exhausted(str(error))
        report = Report(
            "reproduction.execution", requested=1, complete=False, issues=tuple(issues)
        )
        obligations, findings = (), None
    except InputError as error:
        try:
            issues.append(Issue("reproduction.input", str(error), kind="input_error"))
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
        report = Report(
            "reproduction.execution", requested=1, complete=False, issues=tuple(issues)
        )
        obligations, findings = (), None
    try:
        payload = {
            "report": report.to_dict(),
            "obligations": [
                {
                    "path": list(item.path),
                    "subjects": list(item.subjects),
                    "graphs": list(item.graphs),
                    "raw_json": item.raw_json,
                    "named": item.named,
                }
                for item in obligations
            ],
            "findings": findings,
            "consumed": budget.allowances,
        }
        # Allowance counters bound the final receipt's decimal widths. Prepay a
        # second encoding before substituting the actual completed counters.
        text = serialized(payload, budget)
        budget.consume("bytes", 4 * len(text))
        budget.consume("work", len(text))
        payload["consumed"] = budget.consumed()
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    except LimitExceeded as error:
        issues.retain_completed(report)
        issues.exhausted(str(error))
        terminal = Report(
            "reproduction.execution", requested=1, complete=False, issues=tuple(issues)
        )
        content = json.dumps(
            {
                "report": terminal.to_dict(),
                "obligations": [],
                "findings": None,
                "consumed": budget.consumed(),
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
    if len(content) > capacity:
        raise LimitExceeded("Reproduction output exceeds protocol capacity")
    sys.stdout.buffer.write(content)


if __name__ == "__main__":
    main()
