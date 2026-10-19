"""Fresh checks from inert persisted model preparation, never saved conformance."""

import hashlib
import json
import sys
import tracemalloc
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import FrameType
from typing import NotRequired, TypedDict, cast

import pytest
from rdflib import BNode, Literal, URIRef
from rdflib.namespace import OWL, RDFS, XSD

from bijux_ontology.absence import ABSENT
from bijux_ontology.declarations.codecs import models_from_data
from bijux_ontology.declarations.types import ModelDeclaration, ValueDeclaration
from bijux_ontology.declarations.value_constraints import ValueConstraints
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.models.emission.types import ModelUsage
from bijux_ontology.models.persistence import (
    PreparationIdentity,
    compile_model_artifact,
    load_model_artifact,
    model_preparation_identity,
)
from bijux_ontology.rdf.literals import Regime
from bijux_ontology.rdf.sources import RdfSource

E = "https://example.org/contracts/"
CONTEXT = E + "context"


class Options(TypedDict):
    """Exactly the closed public keyword arguments used by these native fixtures."""

    imports: dict[str, RdfSource]
    context: object
    resources: dict[str, object]
    base: str
    regime: NotRequired[Regime]


def inputs() -> tuple[tuple[ModelDeclaration, ...], RdfSource, Options]:
    """Return independent resources with recursive nested model obligations."""
    models = models_from_data(
        [
            {
                "identifier": "node",
                "types": [E + "Node"],
                "fields": [
                    {
                        "name": "label",
                        "predicate": E + "label",
                        "emission": "guaranteed",
                        "values": [{"kind": "literal", "datatype": str(XSD.string)}],
                    },
                    {
                        "name": "child",
                        "predicate": E + "child",
                        "emission": "optional",
                        "values": [
                            {
                                "kind": "object",
                                "model": "node",
                                "role": {
                                    "kind": "instance",
                                    "types": [E + "Node"],
                                    "type_mode": "named",
                                },
                            }
                        ],
                    },
                ],
            }
        ]
    )
    header = (
        f"@prefix ex: <{E}> . @prefix owl: <{OWL}> . "
        f"@prefix rdfs: <{RDFS}> . @prefix xsd: <{XSD}> .\n"
    )
    source = RdfSource(
        header + 'ex:ontology owl:imports ex:imported . _:same rdfs:label "local" .',
        format="turtle",
    )
    imported = RdfSource(
        header + "ex:Node a owl:Class . "
        "ex:label a owl:DatatypeProperty ; rdfs:domain ex:Node ; "
        "rdfs:range xsd:string . "
        "ex:child a owl:ObjectProperty ; rdfs:domain ex:Node ; rdfs:range ex:Node . "
        '_:same rdfs:label "imported" .',
        format="turtle",
    )
    options: Options = {
        "imports": {E + "imported": imported},
        "context": CONTEXT,
        "resources": {CONTEXT: {"@context": {"@vocab": E}}},
        "base": E + "root",
    }
    return models, source, options


def emission(bad: bool = False) -> dict[str, object]:
    """Keep one nested counterexample between two actual supported outputs."""
    return {
        "@id": E + "root",
        "@type": "Node",
        "label": "Root",
        "child": {"@id": E + "child", "@type": "Node", "label": 7 if bad else "Child"},
    }


def compiled() -> tuple[
    bytes, str, PreparationIdentity, tuple[ModelDeclaration, ...], RdfSource, Options
]:
    """Require actual native preparation before inspecting its public artifact."""
    models, source, options = inputs()
    result = compile_model_artifact(models, source, **options)
    assert result.content is not None, result.report.to_dict()
    assert result.report.verified, result.report.to_dict()
    assert result.sha256 == hashlib.sha256(result.content).hexdigest()
    assert result.sha256 is not None and result.identity is not None
    return result.content, result.sha256, result.identity, models, source, options


def test_native_reuse_keeps_fresh_nested_checks_and_ownership() -> None:
    content, sha256, identity, models, source, options = compiled()
    expected = model_preparation_identity(models, source, **options)
    assert identity == expected
    calls: dict[str, int] = {}

    def profile(frame: FrameType, event: str, arg: object) -> None:
        if event == "call":
            key = frame.f_globals.get("__name__", "") + "." + frame.f_code.co_name
            if key.endswith((".parse_dataset", ".view._index", ".check_models")):
                calls[key] = calls.get(key, 0) + 1

    sys.setprofile(profile)
    try:
        loaded = load_model_artifact(content, expected=expected, trusted_sha256=sha256)
    finally:
        sys.setprofile(None)
    assert loaded.prepared is not None, loaded.report.to_dict()
    assert loaded.report.verified
    assert not any(key.endswith((".parse_dataset", ".view._index")) for key in calls)
    assert calls["bijux_ontology.models.contracts.checking.check_models"] == 1
    assert loaded.reused.sources == ("local", "import[0]")
    assert loaded.reused.inspections
    plan = loaded.prepared
    results = [
        plan.check(emission(bad), (ModelUsage("node"),)) for bad in (False, True, False)
    ]
    assert [report.verified for report in results] == [True, False, True]
    assert any(issue.code == "emission.value_mismatch" for issue in results[1])
    exported = plan._vocabulary.local
    exported.remove((None, None, None))
    assert plan.check(emission(), (ModelUsage("node"),)).verified
    blanks = {
        edge.subject
        for index in plan._vocabulary._indexes.values()
        for edge in index.edges
        if isinstance(edge.subject, BNode)
    }
    assert len(blanks) == 2


def test_current_inputs_invalidate_and_absence_is_distinct() -> None:
    content, sha256, identity, models, source, options = compiled()
    original = source.content
    assert isinstance(original, str)
    variations: list[tuple[tuple[ModelDeclaration, ...], RdfSource, Options]] = [
        (models, replace(source, content=original + "\n# revision"), options),
        (models, source, {**options, "context": {"@vocab": E}}),
        (models, source, {**options, "context": None}),
        (models, source, {**options, "context": ABSENT}),
        (models, source, {**options, "regime": "rdf"}),
        (models, replace(source, document_iri=E + "source"), options),
        ((replace(models[0], location="changed"),), source, options),
        (
            models,
            source,
            {
                **options,
                "imports": {
                    **options["imports"],
                    E + "unused": RdfSource("", format="turtle"),
                },
            },
        ),
    ]
    identities = []
    for changed_models, changed_source, changed_options in variations:
        expected = model_preparation_identity(
            changed_models, changed_source, **changed_options
        )
        identities.append(expected)
        loaded = load_model_artifact(content, expected=expected, trusted_sha256=sha256)
        assert loaded.prepared is None
        assert any(
            issue.code == "models.artifact_incompatible" for issue in loaded.report
        )
        assert not loaded.reused.sources
    assert len(set(identities)) == len(identities)
    changed_source = replace(
        source,
        content=original + "\n<" + E + "Fresh> a <" + str(OWL.Class) + "> .",
    )
    changed = compile_model_artifact(models, changed_source, **options)
    assert changed.content is not None and changed.identity is not None
    assert changed.sha256 is not None
    assert changed.identity == model_preparation_identity(
        models, changed_source, **options
    )
    restored = load_model_artifact(
        changed.content, expected=changed.identity, trusted_sha256=changed.sha256
    )
    assert restored.prepared is not None
    assert (URIRef(E + "Fresh"), None, None) in restored.prepared._vocabulary.local


def test_unknown_format_extension_corruption_and_external_pin_reject() -> None:
    content, sha256, identity, *_ = compiled()
    raw = json.loads(content)
    cases = [
        dict(raw, format_version=2),
        dict(raw, extensions=[{"callable": "os.system"}]),
        dict(raw, unknown="ignored"),
        dict(raw, terms=[["python", "object"]]),
    ]
    for changed in cases:
        changed_content = json.dumps(changed).encode()
        result = load_model_artifact(
            changed_content,
            expected=identity,
            trusted_sha256=hashlib.sha256(changed_content).hexdigest(),
        )
        assert result.prepared is None and not result.report.verified
    wrong_pin = load_model_artifact(content, expected=identity, trusted_sha256="0" * 64)
    assert wrong_pin.prepared is None
    changed_engine = PreparationIdentity(identity.inputs_sha256, "0" * 64)
    assert (
        load_model_artifact(
            content, expected=changed_engine, trusted_sha256=sha256
        ).prepared
        is None
    )


def test_declaration_wire_preserves_exact_terms_absence_and_decimal() -> None:
    models, source, options = inputs()
    branch = ValueDeclaration(
        "literal",
        datatype=str(XSD.string),
        language=ABSENT,
        direction=ABSENT,
        samples=(Literal("hello", normalize=False),),
        constraints=ValueConstraints(
            enumeration=(Literal("hello", normalize=False),),
            minimum=Decimal("0.00000000000000000000000001"),
        ),
    )
    models = (
        replace(models[0], fields=(replace(models[0].fields[0], values=(branch,)),)),
    )
    artifact = compile_model_artifact(models, source, **options)
    assert artifact.content is not None, artifact.report.to_dict()
    assert artifact.identity is not None and artifact.sha256 is not None
    loaded = load_model_artifact(
        artifact.content, expected=artifact.identity, trusted_sha256=artifact.sha256
    )
    assert loaded.prepared is not None
    restored = loaded.prepared.models[0].fields[0].values[0]
    assert restored == branch
    assert restored.language is ABSENT and restored.direction is ABSENT


def test_dataset_physical_scope_and_empty_contexts_survive() -> None:
    models, source, options = inputs()
    graph_name = E + "graph"
    original = source.content
    assert isinstance(original, str)
    content = original.replace("ex:ontology owl:imports ex:imported .", "")
    source = RdfSource(
        f"<{graph_name}> {{ {content} }} <{E}empty> {{ }}",
        format="trig",
        graph_name=graph_name,
    )
    # Prefix directives remain outside the TriG graph body.
    header, body = content.rsplit("\n", 1)
    source = replace(
        source, content=f"{header}\n<{graph_name}> {{ {body} }} <{E}empty> {{ }}"
    )
    artifact = compile_model_artifact(models, source, **options)
    # Import map is supplied, but only actual required imports are native loaded.
    assert artifact.content is not None, artifact.report.to_dict()
    assert artifact.identity is not None and artifact.sha256 is not None
    loaded = load_model_artifact(
        artifact.content, expected=artifact.identity, trusted_sha256=artifact.sha256
    )
    assert loaded.prepared is not None, loaded.report.to_dict()
    actual = loaded.prepared._vocabulary._source_handles[0][1].prepared
    assert actual.graph_name == graph_name
    assert E + "empty" in {str(graph.identifier) for graph in actual.dataset().graphs()}
    changed = model_preparation_identity(
        models, replace(source, graph_name=E + "empty"), **options
    )
    assert changed != artifact.identity
    assert (
        load_model_artifact(
            artifact.content, expected=changed, trusted_sha256=artifact.sha256
        ).prepared
        is None
    )


def test_caps_and_non_data_inputs_publish_no_partial_preparation() -> None:
    content, sha256, identity, models, source, options = compiled()
    for limits in (
        Limits(max_work=10),
        Limits(max_states=10),
        Limits(max_total_bytes=1024),
    ):
        budget = Budget(limits)
        loaded = load_model_artifact(
            content,
            expected=identity,
            trusted_sha256=sha256,
            budget=budget,
        )
        assert loaded.prepared is None and loaded.report.outcome == "exhausted"
        assert not loaded.reused.sources
        for name, count in budget._counts.items():
            cap = (
                limits.max_total_bytes
                if name == "bytes"
                else getattr(limits, "max_" + name)
            )
            assert count <= cap
    original = source.content
    assert isinstance(original, str)
    source_text: str = original
    reads: list[str] = []

    class ChangingSource(RdfSource):
        def __getattribute__(self, name: str) -> object:
            if name == "content":
                reads.append(name)
                return source_text if len(reads) <= 2 else source_text + "\n# different"
            return cast(object, super().__getattribute__(name))

    changing = ChangingSource(source_text, format="turtle")
    reads.clear()
    rejected = compile_model_artifact(models, changing, **options)
    assert rejected.content is None and rejected.report.outcome == "input_error"
    assert not reads
    with pytest.raises(InputError):
        model_preparation_identity(models, changing, **options)
    result = compile_model_artifact(
        models, replace(source, content=Path("source.ttl")), **options
    )
    assert result.content is None and result.report.outcome == "input_error"
    with pytest.raises(InputError):
        load_model_artifact(
            content,
            expected=identity,
            trusted_sha256=sha256,
            budget=cast(Budget, "bad"),
        )


def test_decimal_conversion_prepays_native_digit_workspace() -> None:
    models, source, options = inputs()
    branch = replace(
        models[0].fields[0].values[0],
        constraints=ValueConstraints(minimum=Decimal("1" * 100_000)),
    )
    large = (
        replace(models[0], fields=(replace(models[0].fields[0], values=(branch,)),)),
    )
    budget = Budget(Limits(max_total_bytes=100_000))
    tracemalloc.start()
    try:
        result = compile_model_artifact(large, source, **options, budget=budget)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.content is None and result.report.outcome == "exhausted"
    assert budget._counts["bytes"] <= budget.limits.max_total_bytes
    assert peak < budget.limits.max_total_bytes
