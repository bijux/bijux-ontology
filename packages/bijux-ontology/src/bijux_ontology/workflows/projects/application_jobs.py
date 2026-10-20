"""Concrete file-backed emission, context corpus and application upgrade jobs."""

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast

from bijux_ontology.absence import ABSENT
from bijux_ontology.declarations.codecs import models_from_data, terms_from_data
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.json.values import (
    array_value,
    at_input_location,
    input_location,
    known_keys,
    object_value,
    optional_string_value,
    own_json,
    text_value,
)
from bijux_ontology.jsonld.contracts.codecs import expectations_from_data
from bijux_ontology.jsonld.processing.conversion import DirectionPolicy
from bijux_ontology.jsonld.processing.resources import OfflineDocumentLoader
from bijux_ontology.models.emission.checking import check_emission
from bijux_ontology.models.emission.codecs import rdf_usages_from_data, usages_from_data
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.workflows.projects.jobs.decoding import (
    GraphSelection,
    VocabularySelection,
    strings,
)
from bijux_ontology.workflows.projects.resources import ProjectResources
from bijux_ontology.workflows.upgrades.applications.comparison import (
    compare_applications,
)
from bijux_ontology.workflows.upgrades.applications.types import (
    ApplicationField,
    ApplicationOutput,
    ApplicationVersion,
)
from bijux_ontology.workflows.upgrades.corpus import (
    ContextCorpusDocument,
    CorpusBranch,
    CorpusWitness,
    assess_context_corpus,
)


class _Inputs:
    """Bound actual input acquisition before existing concrete operation stages."""

    def __init__(
        self, resources: ProjectResources, budget: Budget | None = None
    ) -> None:
        self.resources = resources
        self.config = resources.configuration
        self.budget = budget if budget is not None else Budget(self.config.limits)

    def read(self, value: object) -> tuple[object, Path]:
        """Read one selected file with cumulative preparation byte/resource bounds."""
        path = self.config.path(value, location="project input")
        return self.resources.read_json(path, budget=self.budget), path

    def array(self, value: object) -> list[object]:
        """Reserve finite owned inventory references before typed construction."""
        result = array_value(value)
        self.budget.consume("states", len(result))
        self.budget.consume("work", len(result))
        return result

    def mapping(self, value: object) -> dict[str, object]:
        """Qualify named metadata before constructing domain records."""
        result = object_value(value, location="project input")
        self.budget.consume("states", len(result))
        for key in result:
            bound_text(key, self.budget)
        return result

    def contexts(self, value: object) -> OfflineDocumentLoader:
        """Own separate explicitly supplied old/new offline context documents."""
        documents = {}
        for iri, filename in self.mapping(value).items():
            document, _ = self.read(filename)
            documents[iri] = document
        return OfflineDocumentLoader(documents, limits=self.config.limits)


def _direction(job: Mapping[str, object]) -> DirectionPolicy | None:
    value = optional_string_value(job.get("rdf_direction"))
    if value not in (None, "i18n-datatype", "compound-literal"):
        raise InputError("Unknown RDF direction policy")
    return cast(DirectionPolicy | None, value)


def _application(value: object, inputs: _Inputs) -> ApplicationVersion:
    """Decode only finite data-only releases through existing declaration codecs."""
    data, path = inputs.read(value)
    entry = inputs.mapping(data)
    known_keys(
        entry,
        {
            "version",
            "terms",
            "models",
            "fields",
            "outputs",
            "context",
            "resources",
            "base",
            "rdf_direction",
        },
        location=str(path),
    )
    if type(entry.get("version")) is not int or entry["version"] != 1:
        raise InputError("Application input version must be 1", location=str(path))
    terms = {}
    for identifier, raw in inputs.mapping(entry.get("terms", {})).items():
        where = input_location(str(path), ("terms", identifier))
        with at_input_location(str(path), ("terms", identifier)):
            term = terms_from_data([raw], budget=inputs.budget)[0]
        terms[identifier] = replace(term, location=term.location or where)
    models = {}
    for identifier, raw in inputs.mapping(entry.get("models", {})).items():
        declaration = object_value(raw, location=str(path))
        fields_raw = inputs.array(declaration.get("fields", []))
        for field in fields_raw:
            inputs.array(object_value(field, location=str(path)).get("values"))
        with at_input_location(str(path), ("models", identifier)):
            model = models_from_data([raw], budget=inputs.budget)[0]
        models[identifier] = replace(
            model,
            location=model.location
            or input_location(str(path), ("models", identifier)),
            fields=tuple(
                replace(
                    field,
                    location=field.location
                    or input_location(
                        str(path), ("models", identifier, "fields", index)
                    ),
                )
                for index, field in enumerate(model.fields)
            ),
        )
    fields = {}
    for identifier, raw in inputs.mapping(entry.get("fields", {})).items():
        field = object_value(raw, location=str(path))
        known_keys(field, {"model", "field", "samples"}, location=str(path))
        samples = {}
        for sample, source_path in inputs.mapping(field.get("samples", {})).items():
            samples[sample] = tuple(cast(list[str | int], inputs.array(source_path)))
        fields[identifier] = ApplicationField(
            text_value(field.get("model"), location="model"),
            text_value(field.get("field"), location="field"),
            samples,
        )
    outputs = {}
    for identifier, raw in inputs.mapping(entry.get("outputs", {})).items():
        output = object_value(raw, location=str(path))
        known_keys(output, {"document", "usages"}, location=str(path))
        if "document" not in output:
            raise InputError(
                "Application outputs require an actual document", location=str(path)
            )
        inputs.array(output.get("usages"))
        outputs[identifier] = ApplicationOutput(
            output["document"], usages_from_data(output["usages"])
        )
    return ApplicationVersion(
        terms=terms,
        models=models,
        fields=fields,
        outputs=outputs,
        context=entry.get("context", ABSENT),
        resources=inputs.mapping(entry.get("resources", {})),
        base=optional_string_value(entry.get("base")),
        rdf_direction=_direction(entry),
    )


def _corpus(job: Mapping[str, object], inputs: _Inputs) -> Report:
    """Delegate actual old/new outputs and branch witnesses to the corpus runner."""
    before = inputs.contexts(job.get("before_contexts", {}))
    after = inputs.contexts(job.get("after_contexts", {}))
    documents = []
    raw_documents = inputs.array(job.get("documents"))
    if not raw_documents:
        raise InputError("Context corpus documents must be nonempty")
    for raw in raw_documents:
        member = object_value(raw, location="documents")
        known_keys(
            member,
            {"name", "before", "after", "expectations", "base"},
            location="documents",
        )
        document, _ = inputs.read(member.get("before"))
        candidate: object = ABSENT
        if "after" in member:
            candidate, _ = inputs.read(member["after"])
        expectations, path = inputs.read(member.get("expectations"))
        documents.append(
            ContextCorpusDocument(
                text_value(member.get("name"), location="name"),
                document,
                expectations_from_data(expectations, source=str(path)),
                candidate,
                optional_string_value(member.get("base")),
            )
        )
    branches = []
    for raw in inputs.array(job.get("branches", [])):
        branch = object_value(raw, location="branches")
        known_keys(branch, {"name", "witnesses", "minimum"}, location="branches")
        witnesses = []
        for item in inputs.array(branch.get("witnesses")):
            witness = object_value(item, location="witnesses")
            known_keys(witness, {"document", "path"}, location="witnesses")
            witnesses.append(
                CorpusWitness(
                    text_value(witness.get("document"), location="document"),
                    tuple(cast(list[str | int], inputs.array(witness.get("path")))),
                )
            )
        branches.append(
            CorpusBranch(
                text_value(branch.get("name"), location="name"),
                tuple(witnesses),
                cast(int, branch.get("minimum", 1)),
            )
        )
    return assess_context_corpus(
        documents,
        before_loader=before,
        after_loader=after,
        branches=branches,
        direction=_direction(job),
        limits=inputs.config.limits,
    ).report


def run_application_job(
    kind: str,
    job: Mapping[str, object],
    resources: ProjectResources,
    *,
    budget: Budget | None = None,
) -> Report:
    """Reuse concrete checkers; configuration never imports serializer callbacks."""
    config = resources.configuration
    inputs = _Inputs(resources, budget)
    # Configurations own JSON already; this also qualifies a job under its smaller
    # selected operation metadata bounds before constructing typed records.
    selected = cast(
        dict[str, object],
        own_json(
            dict(job),
            max_bytes=config.limits.max_bytes,
            max_depth=config.limits.max_depth,
            budget=inputs.budget,
        ),
    )
    if kind == "context-corpus":
        return _corpus(selected, inputs)
    if kind == "rdf-emission":
        declarations, path = inputs.read(selected.get("declarations"))
        usages, _ = inputs.read(selected.get("usages"))
        graph = GraphSelection.from_job(
            selected, name_key="data_graph", union_key="data_union"
        )
        vocabulary = None
        if "source" in selected:
            scope = VocabularySelection.from_job(selected)
            vocabulary = resources.vocabulary(
                scope.source,
                lookup=scope.lookup,
                graph_name=scope.graph.name,
                union=scope.graph.union,
            )
        elif set(selected) & {"lookup", "graph_name", "union"}:
            raise InputError("Vocabulary options require a selected source")
        return check_rdf_emission(
            resources.source(text_value(selected.get("document"), location="document")),
            models_from_data(declarations, source=str(path), budget=inputs.budget),
            rdf_usages_from_data(usages, budget=inputs.budget),
            vocabulary=vocabulary,
            graph_name=graph.name,
            union=graph.union,
            rdf_direction=_direction(selected),
            budget=inputs.budget,
        )
    if kind == "emission":
        scope = VocabularySelection.from_job(selected)
        vocabulary = resources.vocabulary(
            scope.source,
            lookup=scope.lookup,
            graph_name=scope.graph.name,
            union=scope.graph.union,
        )
        document, _ = inputs.read(selected.get("document"))
        declarations, path = inputs.read(selected.get("declarations"))
        usages, _ = inputs.read(selected.get("usages"))
        inputs.array(declarations)
        inputs.array(usages)
        return check_emission(
            document,
            models_from_data(declarations, source=str(path), budget=inputs.budget),
            usages_from_data(usages, budget=inputs.budget),
            vocabulary=vocabulary,
            base=optional_string_value(selected.get("base")),
            document_loader=resources.context_loader(inputs.budget),
            allowed_drops=frozenset(strings(selected.get("allowed_drops", []))),
            budget=inputs.budget,
            rdf_direction=_direction(selected),
        )
    if kind == "upgrade":
        versions = []
        vocabularies = []
        for side in ("before", "after"):
            raw = object_value(selected.get(side), location=side)
            known_keys(
                raw,
                {"source", "lookup", "graph_name", "union", "application"},
                location=side,
            )
            scope = VocabularySelection.from_job(raw)
            versions.append(_application(raw.get("application"), inputs))
            vocabularies.append(
                resources.vocabulary(
                    scope.source,
                    lookup=scope.lookup,
                    graph_name=scope.graph.name,
                    union=scope.graph.union,
                )
            )
        return compare_applications(
            vocabularies[0],
            vocabularies[1],
            before=versions[0],
            after=versions[1],
            scopes=strings(selected.get("scopes", ["local"])),
            limits=config.limits,
        ).report
    raise InputError("Unsupported application project job")
