"""Qualify pinned native BIA output through explicit installed Bijux contracts."""

import argparse
import json
import random
import subprocess
from copy import deepcopy
from dataclasses import asdict, replace
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path

import bia_ro_crate
from bia_ro_crate.core.bia_ro_crate_metadata import BIAROCrateMetadata
from bia_ro_crate.core.parser.ro_crate_parser import ROCrateParser
from bia_ro_crate.models.linked_data.ontology_terms import BIA
from bia_ro_crate.models.linked_data.pydantic_ld.FieldContext import FieldContext
from bia_ro_crate.models.model_registry import MODEL_REGISTRY
from bia_ro_crate.models.ro_crate_models import Contributor, Dataset, FileList, Study
from rdflib import BNode, Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD

import bijux_ontology
from bijux_ontology.declarations import ValueDeclaration
from bijux_ontology.interfaces.adapters.contracts import RdfField
from bijux_ontology.interfaces.adapters.namespaces import namespace_inventory
from bijux_ontology.interfaces.adapters.pydantic import model_declarations
from bijux_ontology.interfaces.execution.executor import WorkerExecutor
from bijux_ontology.models.checking import assess_models, check_models
from bijux_ontology.owl.obligations import OntologyObligation
from bijux_ontology.workflows.projects.contracts import (
    ContractObligation,
    check_contract,
)

BIA_REVISION = "17e0a80ac02ef1f4878aff54cd415c040129d0d5"


def save(root, name, value):
    (root / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def parse(crate):
    parser = ROCrateParser(crate)
    try:
        parser.parse()
        result = parser.result
    except ExceptionGroup:
        return None, [issue.to_dict() for issue in parser.issues]
    return result, [issue.to_dict() for issue in parser.issues]


def resolve(metadata):
    """Read real FieldContext traits; unknown scalar/output guarantees stay unknown."""
    return RdfField(
        str(metadata.uri),
        (
            ValueDeclaration(
                "iri" if metadata.is_id_field else "unknown",
                container=metadata.container,
            ),
        ),
        reverse=metadata.is_reverse_field,
        output_contract=True,
    )


def extraction_inventory(extraction):
    return [
        {"model": model.identifier, **asdict(field)}
        for model in extraction.declarations
        for field in model.fields
    ]


def select_entities(executor, document, base, types=()):
    result = executor.run(
        "selection",
        {
            "document": document,
            "base": base,
            "selector": {
                "kind": "entity",
                "types": list(types),
                "graph": None,
                "coverage_unit": "rdf_entity",
                "minimum": 1,
            },
        },
    )
    require(
        result.report.verified,
        f"Required entity selection is incomplete: {result.report.to_dict()}",
    )
    require(
        isinstance(result.data, dict) and result.data["coverage_unit"] == "rdf_entity",
        "Worker did not retain RDF entity coverage",
    )
    entities = result.data["entities"]
    require(
        result.data["count"] == len(entities),
        "Worker selection count does not match its entity keys",
    )
    named = [
        key
        for key in entities
        if isinstance(key["identifier"], str)
        and not key["identifier"].startswith("_:")
        and key["anonymous"] is None
        and key["graph"] is None
        and not key["anonymous_graph"]
    ]
    return result, named


def selected_contracts(extraction, native_graph, executor, document, base):
    """Consumer policy applies only to these named, asserted native entities."""
    definitions = {model.identifier: model for model in extraction.declarations}
    models, usages, selections = [], [], []
    for cls in (Study, Contributor, Dataset):
        identifier = f"{cls.__module__}.{cls.__qualname__}"
        selected = []
        for field in definitions[identifier].fields:
            if field.name == "name":
                values = (ValueDeclaration("literal", datatype=str(XSD.string)),)
            elif cls is Study and field.name == "author":
                values = (
                    ValueDeclaration(
                        "iri", types=(str(BIA.Contributor),), container="@set"
                    ),
                )
            else:
                continue
            selected.append(replace(field, values=values, emission="guaranteed"))
        models.append(replace(definitions[identifier], fields=tuple(selected)))
        selection, keys = select_entities(
            executor, document, base, (str(cls.get_model_type()),)
        )
        subjects = [key["identifier"] for key in keys]
        native_subjects = {
            str(node)
            for node in native_graph.subjects(RDF.type, cls.get_model_type())
            if isinstance(node, URIRef)
        }
        require(
            set(subjects) == native_subjects,
            "Worker-selected identities differ from native RDF output",
        )
        require(
            bool(subjects),
            f"Required native type has no named entities: {cls.__name__}",
        )
        selections.append((cls, selection, keys))
        usages.extend(
            {
                "model": identifier,
                "reference": str(subject),
                "graph": None,
                "entity": True,
                "closed": True,
                "subject": {"expected_iri": str(subject), "allow_blank": False},
            }
            for subject in subjects
        )
    return models, usages, selections


def qualify(source, worker, output):
    output.mkdir(parents=True, exist_ok=True)
    save(output, "evidence.json", {"status": "incomplete"})
    revision = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    require(
        revision == BIA_REVISION,
        "The BIA source revision is outside the qualified target",
    )
    subprocess.run(
        ["git", "-C", str(source), "diff", "--quiet", "HEAD", "--", "src", "test"],
        check=True,
    )
    installed = Path(bia_ro_crate.__file__).parent
    native_source = source / "src/bia_ro_crate"
    for path in native_source.rglob("*"):
        if path.suffix in {".py", ".ttl", ".jsonld"}:
            require(
                (installed / path.relative_to(native_source)).read_bytes()
                == path.read_bytes(),
                f"Installed BIA differs from pinned source: {path.name}",
            )
    fixtures = source / "test/validator/input_ro_crate"
    native, native_issues = parse(fixtures / "test_typical_ro_crate")
    require(
        native is not None, f"Native BIA parser rejected typical crate: {native_issues}"
    )
    metadata = native.metadata
    document = metadata.to_dict()
    save(output, "document.json", document)
    base = (metadata.get_base_path() / metadata.DEFAULT_RO_CRATE_FILENAME).as_uri()
    graph = metadata.to_graph()
    registry = {model: (str(iri),) for iri, model in MODEL_REGISTRY.items()}
    extracted = model_declarations(registry, metadata_resolvers={FieldContext: resolve})
    inventory = extraction_inventory(extracted)
    require(
        len(registry) == 25 and len(inventory) == 100,
        "Registered BIA inventory changed",
    )
    save(output, "mapping-inventory.json", inventory)
    save(output, "extraction-report.json", extracted.report.to_dict())
    ontology_text = (
        files("bia_ro_crate")
        .joinpath("models/linked_data/bia_ontology.ttl")
        .read_text()
    )
    (output / "bia-ontology.ttl").write_text(ontology_text, encoding="utf-8")
    ontology = Graph().parse(data=ontology_text, format="turtle")
    full_model_report = check_models(extracted.declarations, ontology)
    save(output, "full-model-report.json", full_model_report.to_dict())
    namespace = namespace_inventory({"BIA": BIA})
    require(len(namespace.declarations) == 79, "BIA namespace inventory changed")
    terms = tuple(term.iri for term in namespace.declarations)
    namespace_report = namespace.check(
        ontology,
        owned_namespaces=("http://bia/",),
        coverage_terms=terms,
        require_coverage=True,
    )
    executor = WorkerExecutor(python=worker, artifacts=output / "runs")
    entity_inventory, inventory_named = select_entities(executor, document, base)
    require(
        entity_inventory.data["count"] == len(document["@graph"]),
        "Native fixture entity coverage changed",
    )
    save(
        output,
        "entity-inventory.json",
        {
            "report": entity_inventory.report.to_dict(),
            "selection": entity_inventory.data,
        },
    )
    models, usages, selections = selected_contracts(
        extracted, graph, executor, document, base
    )
    save(
        output,
        "selection-coverage.json",
        {
            cls.__qualname__: {
                "report": selection.report.to_dict(),
                "selection": selection.data,
                "named_bindings": len(keys),
            }
            for cls, selection, keys in selections
        },
    )
    models_data = json.loads(json.dumps([asdict(model) for model in models]))
    save(output, "selected-models.json", models_data)
    save(output, "selected-usages.json", usages)
    parameters = {
        "document": document,
        "base": base,
        "models": models_data,
        "usages": usages,
    }

    def emission(candidate):
        return executor.run("emission", {**parameters, "document": candidate}).report

    positive = emission(document)
    named = OntologyObligation(
        "subsumption", (str(BIA.Study),), "http://schema.org/CreativeWork"
    )
    selected_proof = assess_models((), ontology, obligations=(named,), allow_empty=True)
    selection_obligations = tuple(
        ContractObligation(
            f"{cls.__qualname__}-selection",
            lambda report=selection.report: report,
            bindings=len(keys),
            minimum_bindings=1,
        )
        for cls, selection, keys in selections
    )
    contract = check_contract(
        (
            *selection_obligations,
            ContractObligation(
                "namespace", lambda: namespace_report, bindings=79, minimum_bindings=79
            ),
            ContractObligation(
                "emission", lambda: positive, bindings=len(usages), minimum_bindings=1
            ),
            ContractObligation("named-study-relation", lambda: selected_proof.report),
        )
    )
    contract.require_verified()
    save(output, "required-contract.json", contract.report.to_dict())
    shuffled = deepcopy(document)
    random.Random(1729).shuffle(shuffled["@graph"])
    shuffled_selections = {}
    for cls, selection, keys in selections:
        repeated_selection, repeated_keys = select_entities(
            executor, shuffled, base, (str(cls.get_model_type()),)
        )
        require(
            repeated_keys == keys
            and repeated_selection.data["count"] == selection.data["count"],
            "Graph array order changed selected entity identities or coverage",
        )
        shuffled_selections[cls.__qualname__] = {
            "report": repeated_selection.report.to_dict(),
            "selection": repeated_selection.data,
        }
    save(output, "shuffled-selection-coverage.json", shuffled_selections)
    repeated = emission(shuffled)
    require(
        repeated.to_dict() == positive.to_dict(),
        "Graph array order changed selected entity results",
    )
    equivalent = executor.run(
        "serialization",
        {
            "before": document,
            "after": shuffled,
            "before_base": base,
            "after_base": base,
        },
    ).report
    require(equivalent.verified, "Graph shuffle changed native RDF meaning")
    controls = {
        "graph_shuffle": repeated.to_dict(),
        "serialization": equivalent.to_dict(),
    }
    for name, context in (
        ("mapping", {"@id": "urn:incorrect-name"}),
        ("datatype", {"@id": "http://schema.org/name", "@type": str(XSD.integer)}),
        ("coercion", {"@id": "http://schema.org/name", "@type": "@id"}),
    ):
        changed = deepcopy(shuffled)
        changed["@context"]["name"] = context
        report = emission(changed)
        require(
            report.outcome == "disagreement",
            f"{name} mutation was not diagnosed: {report.to_dict()}",
        )
        controls[name] = report.to_dict()
    changed = deepcopy(shuffled)
    changed["@context"]["author"] = {
        "@reverse": "http://schema.org/author",
        "@container": "@set",
    }
    reverse = emission(changed)
    require(
        reverse.outcome == "disagreement", "Reversed author mapping was not diagnosed"
    )
    controls["reverse"] = reverse.to_dict()
    contributor = next(
        item for item in usages if item["model"].endswith(".Contributor")
    )["reference"]
    changed = deepcopy(shuffled)
    next(node for node in changed["@graph"] if node["@id"] == contributor).pop("name")
    missing = emission(changed)
    require(
        missing.outcome == "disagreement"
        and len(missing.issues) == 1
        and contributor in missing.issues[0].location,
        "Missing field did not fail at its selected entity",
    )
    controls["emission"] = missing.to_dict()
    missing_namespace = Graph()
    missing_namespace += ontology
    missing_namespace.remove((BIA.Study, RDF.type, None))
    absent = namespace.check(missing_namespace, owned_namespaces=("http://bia/",))
    require(not absent.verified, "Missing owned namespace declaration was hidden")
    controls["namespace"] = absent.to_dict()
    restricted = Graph()
    restricted += ontology
    restriction = BNode()
    restricted.add((BIA.Study, RDFS.subClassOf, restriction))
    restricted.add((restriction, RDF.type, OWL.Restriction))
    restricted.add((restriction, OWL.onProperty, BIA.acknowledgement))
    restricted.add((restriction, OWL.someValuesFrom, RDF.langString))
    narrow = assess_models((), restricted, obligations=(named,), allow_empty=True)
    unresolved = assess_models(
        (),
        restricted,
        obligations=(named, OntologyObligation("restrictions", (str(BIA.Study),))),
        allow_empty=True,
    )
    require(
        narrow.report.verified and not unresolved.report.verified,
        "A named proof erased an independent unresolved restriction",
    )
    required_unknown = check_contract(
        (ContractObligation("ontology", lambda: unresolved.report),)
    )
    require(
        not required_unknown.accepted,
        "A required unknown ontology obligation was accepted",
    )
    controls["selected_named_with_restriction"] = narrow.report.to_dict()
    controls["required_unknown_restriction"] = required_unknown.report.to_dict()
    save(
        output,
        "ontology-obligations.json",
        {
            name: [
                {
                    **asdict(answer),
                    "witness": [
                        [node.n3() for node in edge] for edge in answer.witness
                    ],
                }
                for answer in assessment.obligations
            ]
            for name, assessment in (
                ("native", selected_proof),
                ("restricted_named", narrow),
                ("required_restrictions", unresolved),
            )
        },
    )
    without_files = {
        key: value
        for key, value in metadata.get_object_lookup().items()
        if not isinstance(value, FileList)
    }
    try:
        BIAROCrateMetadata(
            without_files, metadata.get_context(), metadata.get_base_path()
        )
    except ValueError as error:
        controls["native_missing_file_list_model"] = str(error)
    else:
        raise RuntimeError("Native metadata producer accepted a missing FileList")
    for fixture in ("test_no_file_list", "test_invalid_objects"):
        result, issues = parse(fixtures / fixture)
        require(
            result is None and issues, f"Native business failure was hidden: {fixture}"
        )
        controls[fixture] = issues
    save(output, "controls.json", controls)
    coverage = {}
    for entity in metadata.get_objects():
        coverage.setdefault(type(entity).__qualname__, []).append(entity.id)
    records = {node["@id"]: node for node in document["@graph"]}
    serializer_coverage = [
        {
            "model": field["model"],
            "field": field["name"],
            "node": entity.id,
            "native_kind": type(records[entity.id].get(field["key"])).__name__,
            "nonempty": records[entity.id].get(field["key"]) not in (None, []),
        }
        for field in inventory
        for entity in metadata.get_objects()
        if field["model"] == f"{type(entity).__module__}.{type(entity).__qualname__}"
    ]
    save(output, "serializer-coverage.json", serializer_coverage)
    summary = {
        "status": "completed",
        "revision": revision,
        "imports": {"bia": bia_ro_crate.__file__, "bijux": bijux_ontology.__file__},
        "versions": {
            name: version(name)
            for name in ("bia-ro-crate-validator", "bijux-ontology", "PyLD", "pydantic")
        },
        "producer": "BIAROCrateMetadata.to_dict",
        "records": len(document["@graph"]),
        "native_models": coverage,
        "file_list_rows": len(native.file_list.data),
        "registered_models": 25,
        "mapped_fields": 100,
        "namespace_terms": 79,
        "mapped_fields_on_exercised_models": len(
            {(row["model"], row["field"]) for row in serializer_coverage}
        ),
        "nonempty_mapped_fields": len(
            {
                (row["model"], row["field"])
                for row in serializer_coverage
                if row["nonempty"]
            }
        ),
        "coverage_unit": "named RDF entities in the default graph",
        "inventory_entities": entity_inventory.data["count"],
        "inventory_named_entities": len(inventory_named),
        "unselected_local_entities": [
            key
            for key in entity_inventory.data["entities"]
            if key not in inventory_named
        ],
        "unselected_named_entities": [
            key
            for key in inventory_named
            if key["identifier"] not in {usage["reference"] for usage in usages}
        ],
        "selected_named_entities": len(usages),
        "selected_models": [model.identifier for model in models],
        "selected_fields": [
            (model.identifier, field.name) for model in models for field in model.fields
        ],
        "inventory_unknown_emission_guarantees": sum(
            field["emission"] == "unknown" for field in inventory
        ),
        "unselected_model_report_verified": full_model_report.verified,
        "selected_contract_verified": contract.report.verified,
        "native_issues": native_issues,
    }
    save(output, "evidence.json", summary)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bia-source", type=Path, required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/bia-integration")
    )
    options = parser.parse_args()
    qualify(
        options.bia_source.resolve(),
        options.worker.absolute(),
        options.output.resolve(),
    )
