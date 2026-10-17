"""Explicit representative JSON-LD output compared with model declarations."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from rdflib import Literal

from bijux_ontology._json import own_json
from bijux_ontology.declarations.types import ModelDeclaration, ValueDeclaration
from bijux_ontology.jsonld.checking import prepare_usages
from bijux_ontology.jsonld.expectations import ContextExpectation
from bijux_ontology.jsonld.observations import (
    ObservedOccurrence,
    SourcePath,
    at_path,
    location,
)
from bijux_ontology.jsonld.resources import DocumentLoader, OfflineDocumentLoader
from bijux_ontology.jsonld.runtime import processor
from bijux_ontology.jsonld.values import ValueTraits
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.identifiers import absolute_iri, language_tag
from bijux_ontology.rdf.literals import check_literal
from bijux_ontology.rdf.terms import literal_term

from ..errors import InputError, LimitExceeded
from ..reports import Issue, Report
from .registry import index_models


@dataclass(frozen=True)
class ModelUsage:
    """Associate a compact document node with one explicit model identity."""

    model: str
    path: SourcePath = ()

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise InputError("Model usage requires a model identifier")
        path = tuple(self.path)
        if isinstance(cast(object, self.path), (str, bytes)) or any(
            type(part) not in (str, int) or isinstance(part, int) and part < 0
            for part in path
        ):
            raise InputError("Model usage requires a key/index path")
        object.__setattr__(self, "path", path)


def _matches(
    value: ValueDeclaration,
    occurrence: ObservedOccurrence,
    registry: dict[str, ModelDeclaration],
) -> bool | None:
    if value.kind == "unknown" or value.container not in (None, "@set", "@list"):
        return None
    container = "list" if value.container == "@list" else "unordered"
    if occurrence.container != container:
        return False
    if occurrence.empty:
        return True if container == "list" else None
    traits = occurrence.traits
    if value.kind == "literal":
        if traits.kind != "literal":
            return False
        if value.datatype is not None and value.datatype != traits.datatype:
            return False
        return not (
            value.language is not None
            and value.language.lower() != (traits.language or "").lower()
        )
    if value.kind == "iri" and traits.kind != "iri":
        return False
    if value.kind == "object" and traits.kind not in {"node", "iri", "blank"}:
        return False
    required = set(value.types)
    if value.model is not None:
        if value.model not in registry:
            return None
        required.update(registry[value.model].types)
    if required - set(traits.types):
        return None if traits.kind in {"iri", "blank"} and not traits.types else False
    return True


def _literal_result(
    occurrence: ObservedOccurrence,
    where: str,
    budget: Budget,
    native: dict[str, Literal],
) -> Report:
    """Validate actual lexical values, obtaining native spelling from PyLD itself."""
    raw = occurrence.value
    if not isinstance(raw, dict) or "@value" not in raw:
        raise RuntimeError("Observed literal lacks its expanded value")
    value = raw["@value"]
    datatype, language = occurrence.traits.datatype, occurrence.traits.language
    if datatype is not None:
        absolute_iri(datatype)
    if language is not None:
        language_tag(language)
    if raw.get("@type") == "@json":
        return Report(
            "literal",
            1,
            (
                Issue(
                    "literal.datatype_unverified",
                    "JSON literal lexical validation is unsupported",
                    where,
                    kind="unverified",
                    actual=datatype,
                ),
            ),
        )
    try:
        if isinstance(value, str):
            term = literal_term(value, datatype=datatype, language=language)
        elif type(value) in (int, float, bool):
            key = occurrence.value_json
            if key not in native:
                budget.consume("states")
                loader = OfflineDocumentLoader({})
                jsonld = processor()
                dataset = jsonld.to_rdf(
                    [{"urn:bijux:observed-literal": [raw]}],
                    {
                        "documentLoader": loader,
                        "contextResolver": jsonld.ContextResolver({}, loader),
                    },
                )
                # This wrapper supplies only an already-expanded literal. Its
                # canonical native lexical form comes from the public processor.
                statements = dataset.get("@default", [])
                if len(statements) != 1:
                    raise RuntimeError("Native literal did not produce one RDF term")
                actual = statements[0]["object"]
                if actual.get("type") != "literal":
                    raise RuntimeError("Native value did not produce an RDF literal")
                native[key] = literal_term(
                    actual["value"],
                    datatype=actual.get("datatype"),
                    language=actual.get("language"),
                )
            term = native[key]
        else:
            raise RuntimeError("Unexpected expanded literal value")
    except InputError as error:
        return Report(
            "literal",
            1,
            (
                Issue(
                    "literal.lexical_unverified",
                    str(error),
                    where,
                    kind="unverified",
                    actual=datatype,
                ),
            ),
        )
    return check_literal(term, location=where)


def check_emission(
    document: object,
    models: Sequence[ModelDeclaration],
    usages: Sequence[ModelUsage],
    *,
    base: str | None = None,
    document_loader: DocumentLoader | None = None,
    allowed_drops: frozenset[str] = frozenset(),
    limits: Limits | None = None,
) -> Report:
    """Check selected actual outputs, without claiming all hypothetical instances.

    Each occurrence must match one alternative branch. Nested model nodes need
    explicit compact-path bindings; external reference typing stays unverified.
    Predicates, reversal, collection membership and literal traits come from the
    shared standards-processed usage snapshot, never a second expansion engine.
    Type checks compare explicit emitted types, without ontology entailment.
    """
    selected = limits if limits is not None else Limits()
    budget = Budget(selected)
    issues = IssueCollector(budget)
    checked = 0
    complete = True
    native_literals: dict[str, Literal] = {}
    try:
        registry = index_models(models, budget, issues)
        for declaration in registry.values():
            for field in declaration.fields:
                for index, branch in enumerate(field.values):
                    budget.consume("work")
                    if branch.model is not None and branch.model not in registry:
                        issues.append(
                            Issue(
                                "input.model_reference_missing",
                                "Value branch references an unknown model",
                                f"{declaration.identifier}.{field.name}[{index}]",
                                kind="input_error",
                                actual=branch.model,
                            )
                        )
        snapshot = own_json(
            document, max_bytes=selected.max_bytes, max_depth=selected.max_depth
        )
        prepared = prepare_usages(
            snapshot,
            base=base,
            document_loader=document_loader,
            allowed_drops=allowed_drops,
            limits=selected,
            budget=budget,
        )
        bindings = tuple(usages)
        for binding in bindings:
            budget.consume("work")
            if not isinstance(binding, ModelUsage):
                raise InputError("Expected typed model usage bindings")
            if binding.model not in registry:
                issues.append(
                    Issue(
                        "input.model_reference_missing",
                        "Usage references an unknown model",
                        location(binding.path),
                        kind="input_error",
                        actual=binding.model,
                    )
                )
        expectations = []
        for binding in bindings:
            budget.consume("work")
            model = registry.get(binding.model)
            if model is None:
                continue
            try:
                node = at_path(snapshot, binding.path)
            except (KeyError, IndexError) as error:
                raise InputError(
                    "No supplied model usage", location=location(binding.path)
                ) from error
            if not isinstance(node, dict):
                raise InputError(
                    "Model bindings select compact node objects",
                    location=location(binding.path),
                )
            checked += 1
            node_traits: tuple[ValueTraits, ...]
            if not binding.path:
                node_traits = prepared.root_nodes
            else:
                observed = prepared.observe(binding.path)
                if observed.limitation:
                    issues.append(
                        Issue(
                            "emission.node_unverified",
                            observed.limitation,
                            location(binding.path),
                            kind="unverified",
                        )
                    )
                depth = min((item.depth for item in observed.occurrences), default=0)
                node_traits = tuple(
                    item.traits for item in observed.occurrences if item.depth == depth
                )
            if len(node_traits) != 1:
                issues.append(
                    Issue(
                        "emission.node_unverified",
                        "Selected compact node cannot be associated "
                        "with exactly one expanded node",
                        location(binding.path),
                        kind="unverified",
                    )
                )
            elif node_traits[0].kind not in {"node", "iri", "blank"}:
                issues.append(
                    Issue(
                        "emission.node_kind_mismatch",
                        "Selected output is not an RDF node",
                        location(binding.path),
                        actual=node_traits[0].kind,
                    )
                )
            elif set(model.types) - set(node_traits[0].types):
                issues.append(
                    Issue(
                        "emission.types_mismatch",
                        "Emitted node does not assert the declared RDF types",
                        location(binding.path),
                        expected=", ".join(model.types),
                        actual=", ".join(node_traits[0].types),
                    )
                )
            for field in model.fields:
                budget.consume("work")
                path = binding.path + (field.key or field.name,)
                checked += 1
                if path[-1] not in node:
                    if field.emission == "guaranteed":
                        issues.append(
                            Issue(
                                "emission.required_occurrence_missing",
                                "Guaranteed field was omitted from "
                                "the representative output",
                                location(path),
                            )
                        )
                    continue
                expectations.append(
                    ContextExpectation(path, field.predicate, reverse=field.reverse)
                )
                observed = prepared.observe(path)
                if observed.limitation:
                    issues.append(
                        Issue(
                            "emission.value_unverified",
                            observed.limitation,
                            location(path),
                            kind="unverified",
                        )
                    )
                    continue
                if not observed.occurrences:
                    if field.emission == "guaranteed":
                        issues.append(
                            Issue(
                                "emission.required_occurrence_missing",
                                "Guaranteed field emitted no RDF occurrence",
                                location(path),
                            )
                        )
                    else:
                        issues.append(
                            Issue(
                                "emission.value_unverified",
                                "No value survived the selected representative usage",
                                location(path),
                                kind="unverified",
                            )
                        )
                    continue
                minimum_depth = min(item.depth for item in observed.occurrences)
                for index, occurrence in enumerate(observed.occurrences):
                    budget.consume("work")
                    # Nested contributions are checked by their own explicit binding.
                    if occurrence.depth != minimum_depth:
                        continue
                    if not occurrence.empty and occurrence.traits.kind == "literal":
                        result = _literal_result(
                            occurrence,
                            f"{location(path)}[{index}]",
                            budget,
                            native_literals,
                        )
                        checked += result.checked
                        issues.extend(result.issues)
                    matches = tuple(
                        _matches(value, occurrence, registry) for value in field.values
                    )
                    if True not in matches:
                        issues.append(
                            Issue(
                                "emission.value_unverified"
                                if None in matches
                                else "emission.value_mismatch",
                                "Observed value cannot be verified "
                                "against a supported branch"
                                if None in matches
                                else "Observed value matches none of "
                                "the declared alternatives",
                                f"{location(path)}[{index}]",
                                kind="unverified" if None in matches else "finding",
                                actual=occurrence.traits.kind,
                            )
                        )
                targets = {
                    value.model for value in field.values if value.model is not None
                }
                if targets:
                    raw = cast(dict[str, object], node)[str(path[-1])]
                    child_paths = (
                        [
                            path + (index,)
                            for index, item in enumerate(raw)
                            if isinstance(item, dict)
                        ]
                        if isinstance(raw, list)
                        else [path]
                        if isinstance(raw, dict)
                        else []
                    )
                    for child_path in child_paths:
                        if not any(
                            item.path == child_path and item.model in targets
                            for item in bindings
                        ):
                            issues.append(
                                Issue(
                                    "emission.nested_scope_unverified",
                                    "Nested model output needs an explicit "
                                    "model usage binding",
                                    location(child_path),
                                    kind="unverified",
                                )
                            )
        mapping = prepared.check(expectations, allow_empty=True)
        checked += mapping.checked
        issues.extend(mapping.issues)
        complete = mapping.complete
    except LimitExceeded as error:
        complete = False
        issues.exhausted(str(error))
    return Report(
        "model-emission",
        checked,
        tuple(issues),
        complete=complete,
        families=("representative-serialization", "literal-values"),
        requested=max(checked, len(usages)),
    )
