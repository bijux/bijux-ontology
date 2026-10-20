"""Sample ordinary Pydantic JSON output using one semantic mapping source."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Literal, cast, get_args

from rdflib import Graph

from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    OutputRequirement,
    ValueDeclaration,
)
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report, combine_reports
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    retain_report,
)
from bijux_ontology.interfaces.adapters.contracts import ModelExtraction
from bijux_ontology.json.paths import SourcePath, at_path, location
from bijux_ontology.json.values import own_json
from bijux_ontology.jsonld.evidence.entities import object_key
from bijux_ontology.jsonld.evidence.occurrences import ObservedOccurrence
from bijux_ontology.jsonld.processing.conversion import DirectionPolicy
from bijux_ontology.jsonld.processing.resources import DocumentLoader, context_resources
from bijux_ontology.jsonld.usages.checking import PreparedUsages
from bijux_ontology.models.emission.bindings import (
    candidate_paths,
    source_entity,
    source_node_traits,
)
from bijux_ontology.models.emission.counts import check_occurrences
from bijux_ontology.models.emission.samples import SampledOutput, serialization_failure
from bijux_ontology.models.emission.serialization import InstanceCheck
from bijux_ontology.models.emission.types import ModelUsage
from bijux_ontology.rdf.datatype_evaluators.registry import DatatypeRegistry
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary

if TYPE_CHECKING:
    from pydantic import BaseModel
    from pydantic.fields import FieldInfo


def _identity(model: type["BaseModel"]) -> str:
    return f"{model.__module__}.{model.__qualname__}"


def _key(
    model: type["BaseModel"],
    name: str,
    field: "FieldInfo",
    options: Mapping[str, object],
) -> str:
    by_alias = options.get("by_alias")
    if by_alias is None:
        by_alias = model.model_config.get("serialize_by_alias", False)
    return (field.serialization_alias or field.alias or name) if by_alias else name


def _children(annotation: object, budget: Budget) -> tuple[type["BaseModel"], ...]:
    """Use Python types only to propose model ownership, never RDF value kinds."""
    from pydantic import BaseModel

    budget.consume("work")
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return (annotation,)
    return tuple(
        dict.fromkeys(
            child
            for argument in get_args(annotation)
            for child in _children(argument, budget)
        )
    )


def _branch(item: ObservedOccurrence, model: str | None) -> ValueDeclaration:
    traits = item.traits
    container = "@list" if item.container == "list" else None
    if traits.kind == "literal":
        return ValueDeclaration(
            "literal",
            datatype=traits.datatype if traits.direction is None else None,
            language=traits.language,
            direction=cast(Literal["ltr", "rtl"] | None, traits.direction),
            container=container,
        )
    if traits.kind in {"iri", "blank", "node"}:
        return ValueDeclaration(
            "object" if model or traits.kind != "iri" else "iri",
            types=traits.types,
            model=model,
            container=container,
        )
    return ValueDeclaration(container=container)


class _Sample:
    """One owned processor snapshot and finite class-associated sample inventory."""

    def __init__(
        self,
        document: object,
        prepared: PreparedUsages,
        options: Mapping[str, object],
        requirements: Mapping[tuple[type["BaseModel"], str], OutputRequirement],
        budget: Budget,
    ) -> None:
        self.document, self.prepared = document, prepared
        self.options, self.requirements, self.budget = options, requirements, budget
        self.issues = IssueCollector(budget)
        self.fields: dict[type[BaseModel], dict[str, FieldDeclaration]] = {}
        self.types: dict[type[BaseModel], set[str]] = {}
        self.conflicts: set[tuple[type[BaseModel], str]] = set()
        self.exclusions: list[str] = []
        self.checked = 0

    def walk(self, root: type["BaseModel"]) -> None:
        pending: list[tuple[type[BaseModel], SourcePath]] = [(root, ())]
        seen: set[SourcePath] = set()
        while pending:
            model, path = pending.pop()
            self.budget.consume("work")
            self.budget.consume("states")
            if path in seen:
                continue
            seen.add(path)
            node = at_path(self.document, path)
            if not isinstance(node, dict):
                self.unknown("Model candidate is not an object", path)
                continue
            traits = source_node_traits(node, path, self.prepared, self.budget)
            if len(traits) != 1 or traits[0].kind not in {"node", "iri", "blank"}:
                self.unknown("Actual model node correspondence is unresolved", path)
                continue
            self.checked += 1
            observed_types = set(traits[0].types)
            if model in self.types and self.types[model] != observed_types:
                self.unknown("One Python model has differing sampled RDF types", path)
                self.types[model].intersection_update(observed_types)
            else:
                self.types.setdefault(model, observed_types)
            fields = self.fields.setdefault(model, {})
            keys = {"@context"}
            for name, field in model.model_fields.items():
                self.budget.consume("work")
                key = _key(model, name, field, self.options)
                keys.add(key)
                selected = (*path, key)
                requirement = self.requirements.get((model, name))
                if key == "@context":
                    continue
                self.checked += 1
                if key not in node:
                    self.issues.extend(
                        check_occurrences(
                            requirement,
                            (),
                            where=location(selected),
                            budget=self.budget,
                            native_literals={},
                            present=False,
                        )
                    )
                    self.exclusions.append(
                        f"not serialized in sample: {location(selected)}"
                    )
                    continue
                if key in {"@id", "@type"}:
                    if requirement is not None:
                        self.unknown(
                            "Header requirements need an independent inventory",
                            selected,
                        )
                    continue
                observed = self.prepared.observe(selected)
                if observed.limitation:
                    self.unknown(observed.limitation, selected)
                    continue
                if not observed.occurrences:
                    self.issues.extend(
                        check_occurrences(
                            requirement,
                            (),
                            where=location(selected),
                            budget=self.budget,
                            native_literals={},
                            present=True,
                            raw=node[key],
                        )
                    )
                    if node[key] not in (None, [], {}):
                        self.unknown(
                            "Serialized field has no established RDF mapping", selected
                        )
                    else:
                        self.exclusions.append(
                            f"no RDF value in sample: {location(selected)}"
                        )
                    continue
                depth = min(item.depth for item in observed.occurrences)
                own = tuple(
                    item for item in observed.occurrences if item.depth == depth
                )
                mappings = {(item.predicate, item.reverse) for item in own}
                if len(mappings) != 1:
                    self.unknown(
                        "Field does not have one established predicate", selected
                    )
                    continue
                predicate, reverse = next(iter(mappings))
                if predicate in {"@id", "@type"}:
                    continue
                if predicate.startswith("@") and predicate != "@id":
                    self.unknown(
                        "Structural output is outside the ordinary model profile",
                        selected,
                    )
                    continue
                targets = _children(field.annotation, self.budget)
                child_model = None
                resources = {
                    object_key(item)
                    for item in own
                    if item.traits.kind in {"node", "iri", "blank"}
                }
                resources.discard(None)
                if targets and any(
                    item.traits.kind not in {"node", "iri", "blank"} for item in own
                ):
                    self.unknown(
                        "Nested model output is not an established RDF node", selected
                    )
                if targets and resources:
                    if len(targets) != 1:
                        self.unknown(
                            "Nested Python model selection is ambiguous", selected
                        )
                    else:
                        target = targets[0]
                        for candidate in candidate_paths(
                            node[key], selected, self.budget
                        ):
                            raw = at_path(self.document, candidate)
                            if not isinstance(raw, dict):
                                continue
                            entity = source_entity(
                                raw, candidate, self.prepared, self.budget
                            )
                            if entity is None or entity not in resources:
                                continue
                            description = self.prepared.entities.get(entity)
                            if description is None or not description.described:
                                continue
                            pending.append((target, candidate))
                            child_model = _identity(target)
                    if child_model is None and len(targets) == 1:
                        self.unknown(
                            "Nested model has no established local description",
                            selected,
                        )
                values = tuple(
                    dict.fromkeys(_branch(item, child_model) for item in own)
                )
                declaration = FieldDeclaration(
                    name,
                    predicate,
                    values,
                    key=key,
                    reverse=reverse,
                    emission="optional",
                    output=requirement
                    or OutputRequirement(allow_null=True, allow_empty=True),
                )
                if (model, name) in self.conflicts:
                    continue
                if name in fields:
                    previous = fields[name]
                    if (previous.predicate, previous.reverse, previous.key) != (
                        predicate,
                        reverse,
                        key,
                    ):
                        self.unknown(
                            "One field has differing sampled semantic mappings",
                            selected,
                        )
                        self.conflicts.add((model, name))
                        fields.pop(name)
                        continue
                    declaration = replace(
                        previous,
                        values=tuple(dict.fromkeys((*previous.values, *values))),
                    )
                fields[name] = declaration
            for key in node:
                if key not in keys:
                    self.unknown(
                        "Output key has no supported declared field owner", (*path, key)
                    )
            if model.model_computed_fields:
                self.unknown(
                    "Computed output requires an independent serializer inventory", path
                )
        for model, name in self.requirements:
            if model not in self.fields:
                self.issues.append(
                    Issue(
                        "adapter.output_unverified",
                        "Required model scope was not established in the sample",
                        f"{_identity(model)}.{name}",
                        kind="unverified",
                    )
                )

    def unknown(self, message: str, path: SourcePath) -> None:
        self.issues.append(
            Issue(
                "adapter.output_unverified", message, location(path), kind="unverified"
            )
        )

    def extraction(self) -> ModelExtraction:
        known = {_identity(model) for model in self.fields}
        declarations = []
        for model, fields in self.fields.items():
            retained = tuple(
                replace(
                    field,
                    values=tuple(
                        replace(value, model=None)
                        if value.model is not None and value.model not in known
                        else value
                        for value in field.values
                    ),
                )
                for field in fields.values()
            )
            declarations.append(
                ModelDeclaration(
                    _identity(model), tuple(sorted(self.types[model])), retained
                )
            )
        return ModelExtraction(
            tuple(declarations),
            Report(
                "pydantic.sample",
                self.checked,
                tuple(self.issues),
                requested=max(1, self.checked),
                families=("sampled-serializer",),
                exclusions=tuple(dict.fromkeys(self.exclusions)),
            ),
        )


def check_instance(
    instance: "BaseModel",
    *,
    vocabulary: Vocabulary | Graph,
    options: Mapping[str, object],
    context: object | Absent = ABSENT,
    inventory: Sequence[ModelDeclaration] | ModelExtraction | None = None,
    requirements: Mapping[tuple[type["BaseModel"], str], OutputRequirement]
    | None = None,
    model: str | None = None,
    base: str | None = None,
    document_loader: DocumentLoader | None = None,
    rdf_direction: DirectionPolicy | None = None,
    limits: Limits | None = None,
    datatype_registry: DatatypeRegistry | None = None,
) -> InstanceCheck:
    """Dump once, check one supported JSON sample, and return its owned output.

    ``mode`` (json or JSON-compatible python), by_alias, exclude_none,
    exclude_unset and exclude_defaults are passed exactly to model_dump. Other
    options require the generic caller-owned serializer API. An authoritative
    context supplies RDF meanings; actual maintained expansion supplies sampled
    literal/resource kinds and datatypes, including Annotated serializers.
    Python annotations propose nested model ownership only. A supplied independent
    inventory is retained unchanged and can detect a context disagreement.
    ``requirements`` adds explicit source/count requirements to context-derived
    samples; independent inventories carry their own OutputRequirement values.
    Types must be asserted by actual output. Arbitrary transformed/computed model
    shapes and unresolved correspondence remain unverified. Success describes
    this actual sample and its options, never universal serializer behavior.
    Limits cover owned semantic work; caller serializer code runs in-process.
    """
    if rdf_direction not in (None, "i18n-datatype", "compound-literal"):
        raise InputError("Unsupported RDF direction policy")
    try:
        from pydantic import BaseModel
    except ImportError as error:
        raise ResourceError("Install bijux-ontology[pydantic]") from error
    if not isinstance(instance, BaseModel):
        raise InputError("Pydantic profile requires a BaseModel instance")
    if set(options) - {
        "mode",
        "by_alias",
        "exclude_none",
        "exclude_unset",
        "exclude_defaults",
    }:
        raise InputError("Unsupported Pydantic profile serialization option")
    if options.get("mode", "python") not in ("json", "python") or any(
        type(value) is not bool and not (name == "by_alias" and value is None)
        for name, value in options.items()
        if name != "mode"
    ):
        raise InputError("Invalid Pydantic profile serialization options")
    if inventory is not None and requirements:
        raise InputError(
            "Independent inventory requirements belong on its field declarations"
        )
    loader = context_resources(document_loader, limits=limits)
    budget = loader.budget
    selected = budget.limits
    supplied = own_json(
        dict(options), max_bytes=selected.max_bytes, max_depth=selected.max_depth
    )
    assert isinstance(supplied, dict)
    required = dict(requirements or {})
    if inventory is not None and not isinstance(inventory, ModelExtraction):
        inventory = tuple(inventory)
    for binding, requirement in required.items():
        if (
            not isinstance(binding, tuple)
            or len(binding) != 2
            or not isinstance(binding[0], type)
            or not issubclass(binding[0], BaseModel)
            or binding[1] not in binding[0].model_fields
            or not isinstance(requirement, OutputRequirement)
        ):
            raise InputError("Requirements select declared Pydantic model fields")
        if (
            _key(binding[0], binding[1], binding[0].model_fields[binding[1]], options)
            == "@context"
        ):
            raise InputError("Context definitions are not output property requirements")
    output_sample = SampledOutput(budget)
    reports: list[Report] = []
    sample: _Sample | None = None
    try:
        if isinstance(inventory, ModelExtraction):
            retained = IssueCollector(budget)
            try:
                retain_report(inventory.report, budget, retained)
            except LimitExceeded:
                retained.exhausted("Pydantic inventory handoff exhausted its budget")
                reports.append(
                    Report(
                        "pydantic.inventory",
                        issues=tuple(retained),
                        requested=1,
                        complete=False,
                    )
                )
                raise
            reports.append(inventory.report)
        output_sample.capture_context(context)
        raw = instance.model_dump(**supplied)
        output = output_sample.capture_output(raw)
        sources = (
            vocabulary
            if isinstance(vocabulary, Vocabulary)
            else load_vocabulary(vocabulary, budget=budget)
        )
        if context is not ABSENT and not isinstance(output, dict):
            raise InputError("Ordinary Pydantic profile requires object output")
        output_sample.apply_context()
        prepared = output_sample.prepare(base=base, document_loader=loader)
        document = output_sample.document
        if inventory is None:
            if model is not None:
                raise InputError(
                    "Explicit model selection requires an independent inventory"
                )
            sample = _Sample(document, prepared, supplied, required, budget)
            sample.walk(type(instance))
            extraction = sample.extraction()
            models = extraction.declarations
            reports.append(extraction.report)
            root = _identity(type(instance))
        else:
            models = (
                inventory.declarations
                if isinstance(inventory, ModelExtraction)
                else tuple(inventory)
            )
            roots = (
                [
                    item.identifier
                    for item in models
                    if item.types
                    and set(item.types) <= set(prepared.root_nodes[0].types)
                ]
                if len(prepared.root_nodes) == 1
                else []
            )
            root = model or (roots[0] if len(roots) == 1 else "")
            if not root or root not in {item.identifier for item in models}:
                raise InputError(
                    "Independent inventory requires one selected root model"
                )
        report = output_sample.check(
            models,
            [ModelUsage(root)],
            vocabulary=sources,
            declaration_report=combine_reports("pydantic.sample", *reports)
            if reports
            else None,
            base=base,
            document_loader=loader,
            rdf_direction=rdf_direction,
            datatype_registry=datatype_registry,
        )
        return InstanceCheck(
            output_sample.output_json, output_sample.document_json, report
        )
    except Exception as error:
        if sample is not None and not reports:
            reports.append(replace(sample.extraction().report, complete=False))
        failure = serialization_failure(error, budget)
        retained = IssueCollector(budget)
        try:
            retain_report(failure, budget, retained)
        except LimitExceeded:
            retained.exhausted("Pydantic sample failed within its resource limits")
            failure = Report(
                "pydantic.sample", issues=tuple(retained), requested=1, complete=False
            )
        reports.append(failure)
        return InstanceCheck(
            output_sample.output_json,
            None,
            combine_reports("pydantic.sample", *reports),
        )
