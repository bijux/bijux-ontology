"""Check real CKAN/DCAT serializer output with an explicit offline profile."""

import argparse
import json
from copy import deepcopy
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path

from ckan.cli import load_config
from ckan.common import config, config_declaration
from ckanext.dcat.processors import RDFSerializer
from rdflib import BNode, Graph, Namespace, URIRef
from rdflib.namespace import XSD

import bijux_ontology
from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.documents.compaction import export_jsonld
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.limits import Budget
from bijux_ontology.models.contracts.checking import check_models
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.models.persistence.codecs import declaration_data
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.sources.types import RdfSource
from bijux_ontology.shacl.checking import validate_shapes
from bijux_ontology.shacl.targets.coverage import ShaclCoverageContract
from bijux_ontology.workflows.projects.checking import check_project
from bijux_ontology.workflows.projects.configuration import ProjectConfig

DCAT = Namespace("http://www.w3.org/ns/dcat#")
DCT = Namespace("http://purl.org/dc/terms/")
DATASET = "11111111-1111-1111-1111-111111111111"
SUBJECT = URIRef("https://catalogue.example/datasets/microscopy")


def require(condition, message):
    """Refuse incomplete qualification rather than producing a success summary."""
    if not condition:
        raise RuntimeError(message)


def dataset():
    """Supply an ordinary package_show-style dictionary with complete identities."""
    return {
        "id": DATASET,
        "name": "microscopy-dataset",
        "title": "Microscopy collection",
        "notes": "Dataset description",
        "metadata_created": "2026-01-01T00:00:00",
        "metadata_modified": "2026-01-02T00:00:00",
        "extras": [
            {"key": "uri", "value": str(SUBJECT)},
            {"key": "temporal_start", "value": "2020-01-01"},
            {"key": "temporal_end", "value": "2020-12-31"},
        ],
        "resources": [
            {
                "id": "22222222-2222-2222-2222-222222222222",
                "package_id": DATASET,
                "name": "Data table",
                "url": "https://data.example/data.csv",
                "download_url": "https://data.example/data.csv",
                "format": "CSV",
                "mimetype": "text/csv",
            }
        ],
        "tags": [],
        "groups": [],
    }


def declarations():
    """Keep required RDF occurrences independent of source JSON presence."""

    def field(name, predicate, value):
        return FieldDeclaration(name, str(predicate), (value,), emission="guaranteed")

    return (
        ModelDeclaration(
            "dataset",
            (str(DCAT.Dataset),),
            (
                field(
                    "title",
                    DCT.title,
                    ValueDeclaration("literal", datatype=str(XSD.string)),
                ),
                field(
                    "distribution",
                    DCAT.distribution,
                    ValueDeclaration("iri", model="distribution"),
                ),
                field(
                    "temporal",
                    DCT.temporal,
                    ValueDeclaration("object", model="temporal"),
                ),
            ),
        ),
        ModelDeclaration(
            "distribution",
            (str(DCAT.Distribution),),
            (field("download_url", DCAT.downloadURL, ValueDeclaration("iri")),),
        ),
        ModelDeclaration(
            "temporal",
            (str(DCT.PeriodOfTime),),
            tuple(
                field(
                    name, predicate, ValueDeclaration("literal", datatype=str(XSD.date))
                )
                for name, predicate in (
                    ("start", DCAT.startDate),
                    ("end", DCAT.endDate),
                )
            ),
        ),
    )


def main():
    """Exercise installed serializers and public checks without database services."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ckan-ini", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    config.update(load_config(str(args.ckan_ini.resolve())))
    config_declaration.load_core_declaration()
    config_declaration.load_plugin("dcat")
    config_declaration.make_safe(config)
    config_declaration.normalize(config)
    profile = Graph().parse(Path(__file__).with_name("profile.ttl"), format="turtle")
    models = declarations()
    reports = {}

    def retain(name, report, verified=True):
        reports[name] = report.to_dict()
        require(report.verified is verified, f"{name}: {report.to_dict()}")

    retain("declarations", check_models(models, profile))
    native = RDFSerializer(profiles=["euro_dcat_ap_2"])
    native.graph_from_dataset(dataset())
    source = prepare_source(RdfSource(native.g, union=True))
    temporal = tuple(source.graph().objects(SUBJECT, DCT.temporal))
    require(
        len(temporal) == 1 and isinstance(temporal[0], BNode),
        "The real serializer must emit the temporal blank node",
    )
    bindings = [RdfModelUsage("dataset", SUBJECT)]
    coverage = ShaclCoverageContract(minimum_focus=2, focus_nodes=(str(SUBJECT),))

    def check(name, prepared):
        retain(name + ".emission", check_rdf_emission(prepared, models, bindings))
        retain(
            name + ".shapes",
            validate_shapes(prepared, profile, coverage=coverage).report,
        )
        retain(name + ".rdf", compare_rdf(source.graph(), prepared.graph()))

    check("native", source)

    def project_output(name, graph):
        """The ordinary portable job must agree with the native Python checker."""
        (args.output / "models.json").write_text(
            json.dumps(declaration_data(models, Budget())), encoding="utf-8"
        )
        (args.output / "bindings.json").write_text(
            json.dumps([{"model": "dataset", "subject": str(SUBJECT), "location": ""}]),
            encoding="utf-8",
        )
        (args.output / "native-output.ttl").write_text(
            graph.serialize(format="turtle"), encoding="utf-8"
        )
        configured = ProjectConfig(
            {
                "version": 2,
                "rdf": {"output": {"path": "native-output.ttl"}},
                "checks": [
                    {
                        "name": "native-output",
                        "kind": "rdf-emission",
                        "document": "output",
                        "declarations": "models.json",
                        "usages": "bindings.json",
                    }
                ],
            },
            directory=args.output,
        )
        ordinary = check_project(configured)
        direct = check_rdf_emission(prepare_source(graph), models, bindings)
        require(
            [(i.code, i.location) for i in ordinary.issues]
            == [(i.code, i.location) for i in direct.issues],
            "Project/native RDF emission findings differ",
        )
        retain(name, ordinary, direct.verified)

    project_output("project.native", source.graph())
    missing_title = source.graph()
    missing_title.remove((SUBJECT, DCT.title, None))
    project_output("project.missing_title", missing_title)
    for syntax, suffix in (("turtle", "ttl"), ("json-ld", "jsonld")):
        # Fresh producers prevent repeat calls from accumulating new blank nodes.
        output = RDFSerializer(profiles=["euro_dcat_ap_2"]).serialize_dataset(
            dataset(), _format=syntax
        )
        (args.output / ("dataset." + suffix)).write_text(output, encoding="utf-8")
        check(syntax, prepare_source(RdfSource(output, format=syntax, union=True)))

    compact = export_jsonld(
        source, context={"dcat": str(DCAT), "dct": str(DCT)}, union=True
    )
    retain("compact", compact.report)
    require(
        compact.content is not None, "Verified compaction must return checked output"
    )
    (args.output / "compacted.jsonld").write_bytes(compact.content)

    wrong = replace(
        models[0],
        fields=(
            replace(models[0].fields[0], predicate="urn:wrong:title"),
            *models[0].fields[1:],
        ),
    )
    retain(
        "changed_mapping.declarations",
        check_models((wrong, *models[1:]), profile),
        False,
    )
    retain(
        "changed_mapping.emission",
        check_rdf_emission(source, (wrong, *models[1:]), bindings),
        False,
    )
    missing = deepcopy(dataset())
    del missing["resources"][0]["download_url"]
    changed = RDFSerializer(profiles=["euro_dcat_ap_2"])
    changed.graph_from_dataset(missing)
    changed_source = prepare_source(RdfSource(changed.g, union=True))
    retain(
        "missing_download.emission",
        check_rdf_emission(changed_source, models, bindings),
        False,
    )
    retain(
        "missing_download.shapes",
        validate_shapes(changed_source, profile, coverage=coverage).report,
        False,
    )
    summary = {
        "module": bijux_ontology.__file__,
        "versions": {
            name: version(name)
            for name in (
                "bijux-ontology",
                "ckan",
                "ckanext-dcat",
                "rdflib",
                "pyld",
                "pyshacl",
            )
        },
        "profile": "euro_dcat_ap_2",
        "ckan_config": str(args.ckan_ini.resolve()),
        "triples": len(source.graph()),
        "temporal_blank_nodes": len(temporal),
        "reports": reports,
    }
    (args.output / "qualification.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {key: value for key, value in summary.items() if key != "reports"}, indent=2
        )
    )


if __name__ == "__main__":
    main()
