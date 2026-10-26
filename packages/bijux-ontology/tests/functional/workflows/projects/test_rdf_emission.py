"""Portable RDF emission uses named roots and the native model occurrence checker."""

import json
from pathlib import Path

from rdflib import URIRef

from bijux_ontology.declarations.codecs import models_from_data
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.workflows.projects.checking import check_project
from bijux_ontology.workflows.projects.configuration import ProjectConfig
from bijux_ontology.workflows.projects.resources import ProjectResources


def test_named_rdf_roots_preserve_graph_selection_required_fields_and_locations(
    tmp_path: Path,
) -> None:
    models = [
        {
            "identifier": "Record",
            "types": ["urn:Record"],
            "fields": [
                {
                    "name": "title",
                    "predicate": "urn:title",
                    "emission": "guaranteed",
                    "values": [
                        {
                            "kind": "literal",
                            "datatype": "http://www.w3.org/2001/XMLSchema#string",
                        }
                    ],
                }
            ],
        }
    ]
    (tmp_path / "models.json").write_text(json.dumps(models))
    bindings = tmp_path / "bindings.json"
    bindings.write_text(
        json.dumps(
            [
                {
                    "model": "Record",
                    "subject": "urn:record",
                    "location": "producer.record",
                }
            ]
        )
    )
    (tmp_path / "output.trig").write_text(
        "<urn:record> a <urn:Record>. <urn:visible> {"
        '<urn:record> a <urn:Record>; <urn:title> "Observed". }'
    )
    job: dict[str, object] = {
        "name": "output",
        "kind": "rdf-emission",
        "document": "output",
        "declarations": "models.json",
        "usages": "bindings.json",
        "data_graph": "@default",
    }
    config: dict[str, object] = {
        "version": 2,
        "rdf": {"output": {"path": "output.trig"}},
        "checks": [job],
    }
    project = ProjectConfig(config, directory=tmp_path)
    direct = check_rdf_emission(
        ProjectResources(project).source("output"),
        models_from_data(models),
        (RdfModelUsage("Record", URIRef("urn:record"), location="producer.record"),),
        graph_name="@default",
    )
    actual = check_project(project).children[0]
    assert [(i.code, i.location) for i in actual.issues] == [
        (i.code, i.location) for i in direct.issues
    ]
    assert not actual.verified and actual.issues[0].location == "producer.record.title"
    job["data_graph"] = "urn:visible"
    assert check_project(ProjectConfig(config, directory=tmp_path)).verified
    job.pop("data_graph")
    job["data_union"] = True
    assert check_project(ProjectConfig(config, directory=tmp_path)).verified
    bindings.write_text('[{"model":"Record","subject":"_:forged"}]')
    rejected = check_project(ProjectConfig(config, directory=tmp_path))
    assert rejected.outcome == "input_error" and not rejected.verified
