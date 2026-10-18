"""Dispatch validated project jobs into concrete ontology-checking operations."""

from collections.abc import Mapping
from typing import Literal, cast

from bijux_ontology._json import optional_string_value, read_json, text_value
from bijux_ontology.declarations.codecs import (
    models_from_data,
    terms_from_data,
)
from bijux_ontology.errors import InputError
from bijux_ontology.jsonld.checking import check_context
from bijux_ontology.jsonld.codecs import expectations_from_data
from bijux_ontology.jsonld.conversion import DirectionPolicy
from bijux_ontology.models.checking import check_models
from bijux_ontology.rdf.literals import Regime as ModelRegime
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.builtins import Regime
from bijux_ontology.vocabulary.checking import check_vocabulary
from bijux_ontology.workflows.projects.jobs import (
    CheckKind,
    GraphSelection,
    VocabularySelection,
    boolean,
    strings,
)
from bijux_ontology.workflows.projects.resources import ProjectResources


def run_check(
    kind: CheckKind, job: Mapping[str, object], resources: ProjectResources
) -> Report:
    """Execute one supported check using its operation-owned explicit inputs."""
    config = resources.configuration

    def snapshot(key: str) -> object:
        """Read one explicitly selected bounded JSON input."""
        return read_json(config.path(job.get(key), location=key), config.limits)

    if kind == "context":
        base = job.get("base")
        if base is not None:
            base = text_value(base, location="base")
        return check_context(
            snapshot("document"),
            expectations_from_data(
                snapshot("expectations"),
                source=str(config.path(job.get("expectations"))),
            ),
            base=base,
            document_loader=resources.document_loader,
            allowed_drops=frozenset(strings(job.get("allowed_drops", []))),
            limits=config.limits,
        ).with_source(str(config.path(job.get("document"))))
    if kind == "shacl":
        from bijux_ontology.shacl.checking import validate_shapes

        supporting = job.get("ontology")
        selected = GraphSelection.from_job(
            job, name_key="data_graph", union_key="data_union"
        )
        return validate_shapes(
            resources.source(text_value(job.get("document"), location="document")),
            resources.source(text_value(job.get("shapes"), location="shapes")),
            ontology=resources.source(text_value(supporting, location="ontology"))
            if supporting is not None
            else None,
            graph_name=selected.name,
            union=selected.union,
            imports=resources.imports,
            trusted_sparql=boolean(job.get("trusted_sparql", False)),
            focus_nodes=strings(job.get("focus_nodes", [])),
            use_shapes=strings(job.get("use_shapes", [])),
            mode=text_value(job.get("mode", "targets"), location="mode"),
            preflight=boolean(job.get("preflight", False)),
            timeout=cast(float | None, job.get("timeout", 30.0)),
            limits=config.limits,
            allow_empty=boolean(job.get("allow_empty", False)),
        ).report
    if kind == "query-rules":
        from bijux_ontology.data.query_rules.checking import run_query_rules
        from bijux_ontology.data.query_rules.loading import load_query_rules

        return run_query_rules(
            resources.source(text_value(job.get("source"), location="source")),
            load_query_rules(
                config.path(job.get("rules"), location="rules"), limits=config.limits
            ),
            limits=config.limits,
            max_rows=cast(int, job.get("max_rows", 1000)),
            timeout=cast(float, job.get("timeout", 30.0)),
            allow_empty=boolean(job.get("allow_empty", False)),
        ).report
    selection = VocabularySelection.from_job(job)
    vocabulary = resources.vocabulary(
        selection.source,
        lookup=selection.lookup,
        graph_name=selection.graph.name,
        union=selection.graph.union,
    )
    if kind == "skos":
        from bijux_ontology.skos.checking import check_skos

        kwargs = {
            name: strings(job[name])
            for name in (
                "scopes",
                "concepts",
                "schemes",
                "rules",
                "languages",
                "language_fallback",
                "definition_predicates",
                "notation_collections",
                "replacement_predicates",
            )
            if name in job
        }
        return check_skos(
            vocabulary,
            profile=cast(
                Literal["core-integrity", "mapping-integrity"] | None,
                job.get("profile"),
            ),
            hierarchy_basis=cast(
                Literal["asserted", "effective"],
                job.get("hierarchy_basis", "effective"),
            ),
            **kwargs,
            closed_targets=boolean(job.get("closed_targets", False)),
            limits=config.limits,
        )
    if kind == "models":
        return check_models(
            models_from_data(
                snapshot("declarations"),
                source=str(config.path(job.get("declarations"))),
            ),
            vocabulary,
            regime=cast(ModelRegime, job.get("regime", "application")),
            rdf_direction=cast(
                DirectionPolicy | None, optional_string_value(job.get("rdf_direction"))
            ),
            limits=config.limits,
        )
    if kind == "vocabulary":
        return check_vocabulary(
            vocabulary,
            terms_from_data(
                snapshot("inventory"), source=str(config.path(job.get("inventory")))
            ),
            local_namespaces=strings(job.get("namespaces", [])),
            exclude=strings(job.get("exclude", [])),
            require_coverage=boolean(job.get("coverage", False)),
            coverage_terms=strings(job.get("coverage_terms", [])),
            coverage_kinds=strings(job.get("coverage_kinds", [])),
            coverage_scope=cast(str, job.get("coverage_scope", "local")),
            regime=cast(Regime, job.get("regime", "rdf")),
            check_references=boolean(job.get("references", False)),
            lint=boolean(job.get("lint", False)),
            allow_empty=boolean(job.get("allow_empty", False)),
            limits=config.limits,
        )
    if kind == "ontology":
        from bijux_ontology.workflows.ontology.checking import check_ontology

        return check_ontology(
            vocabulary,
            regime=cast(Regime, job.get("regime", "rdf")),
            families=strings(
                job.get(
                    "families",
                    [
                        "references",
                        "declarations",
                        "headers",
                        "expressions",
                        "annotations",
                    ],
                )
            ),
            include_imports=boolean(job.get("include_imports", True)),
            include_lookup=boolean(job.get("include_lookup", False)),
            required_annotations=strings(job.get("required_annotations", [])),
            languages=strings(job.get("languages", [])),
            used_terms=strings(job.get("used_terms", [])),
            replacement_predicates=strings(job.get("replacement_predicates", [])),
            limits=config.limits,
        )
    if kind == "data":
        from bijux_ontology.data.usage import check_data_usage

        selected = GraphSelection.from_job(
            job, name_key="data_graph", union_key="data_union"
        )
        return check_data_usage(
            resources.source(text_value(job.get("document"), location="document")),
            vocabulary,
            graph_name=selected.name,
            union=selected.union,
            exclude=strings(job.get("exclude", [])),
            check_types=boolean(job.get("check_types", True)),
            check_literals=boolean(job.get("check_literals", True)),
            limits=config.limits,
        )
    raise InputError("Unsupported project check kind", location=kind)
