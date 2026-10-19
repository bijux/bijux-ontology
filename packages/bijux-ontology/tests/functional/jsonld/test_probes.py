"""Finite context qualification catches declared branch mutations through PyLD."""

from collections.abc import Iterator
from copy import deepcopy

import pytest
from rdflib import Literal
from rdflib.namespace import XSD

from bijux_ontology.declarations import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.errors import InputError
from bijux_ontology.limits import Limits
from bijux_ontology.models.context_probes import (
    ContextProbe,
    ProbeSample,
    probe_context,
)

EX = "https://example.org/"


def contract() -> tuple[dict[str, object], ContextProbe]:
    """Exercise separate ordinary, coerced, reverse and type-scoped branches."""
    context: dict[str, object] = {
        "title": EX + "title",
        "link": {"@id": EX + "link", "@type": "@id"},
        "parent": {"@reverse": EX + "child", "@type": "@id"},
        "kind": {
            "@id": EX + "Record",
            "@context": {"measure": {"@id": EX + "measure", "@type": str(XSD.integer)}},
        },
    }
    model = ModelDeclaration(
        "record",
        (EX + "Record",),
        (
            FieldDeclaration(
                "title", EX + "title", (ValueDeclaration("literal", str(XSD.string)),)
            ),
            FieldDeclaration("link", EX + "link", (ValueDeclaration("iri"),)),
            FieldDeclaration(
                "parent", EX + "child", (ValueDeclaration("iri"),), reverse=True
            ),
            FieldDeclaration(
                "measure",
                EX + "measure",
                (ValueDeclaration("literal", str(XSD.integer)),),
            ),
        ),
    )
    return context, ContextProbe(model, type_values=("kind",))


@pytest.mark.parametrize("mutation", ["title", "link", "parent", "measure", "type"])
def test_every_mapping_branch_and_selected_type_scope_is_observed(
    mutation: str,
) -> None:
    context, probe = contract()
    before = deepcopy(context)
    good = probe_context(context, [probe])
    assert good.verified, good.to_dict()
    assert good.checked == good.requested == 4
    assert len(good.children) == 4
    assert all("neutral" in child.scope for child in good.children)
    changed = deepcopy(context)
    if mutation == "measure":
        changed["kind"] = {
            "@id": EX + "Record",
            "@context": {"measure": {"@id": EX + "wrong", "@type": str(XSD.integer)}},
        }
    elif mutation == "type":
        changed["kind"] = {
            "@id": EX + "Wrong",
            "@context": {"measure": {"@id": EX + "measure", "@type": str(XSD.integer)}},
        }
    elif mutation == "link":
        changed[mutation] = {"@id": EX + "link"}
    elif mutation == "parent":
        changed[mutation] = {"@id": EX + "child", "@type": "@id"}
    else:
        changed[mutation] = EX + "wrong"
    bad = probe_context(changed, [probe])
    assert not bad.verified
    assert any(issue.kind == "finding" for issue in bad.issues), bad.to_dict()
    assert context == before


@pytest.mark.parametrize("datatype", [XSD.integer, XSD.date])
def test_literal_probe_uses_untyped_lexical_input_to_exercise_context_coercion(
    datatype: object,
) -> None:
    model = ModelDeclaration(
        "record",
        (),
        (
            FieldDeclaration(
                "value", EX + "value", (ValueDeclaration("literal", str(datatype)),)
            ),
        ),
    )
    probe = ContextProbe(model)
    assert probe_context(
        {"value": {"@id": EX + "value", "@type": str(datatype)}}, [probe]
    ).verified
    bad = probe_context({"value": EX + "value"}, [probe])
    assert "context.datatype_mismatch" in {issue.code for issue in bad.issues}


@pytest.mark.parametrize("container", ["@list", "@set"])
def test_container_representatives_retain_real_source_shapes(container: str) -> None:
    branch = ValueDeclaration("literal", str(XSD.string), container=container)
    raw: object = ["one", "two"]
    definition: dict[str, object] = {"@id": EX + "value", "@container": container}
    model = ModelDeclaration(
        "record", (), (FieldDeclaration("value", EX + "value", (branch,)),)
    )
    sample = ProbeSample("value", 0, raw)
    probe = ContextProbe(model, samples=(sample,))
    good = probe_context({"value": definition}, [probe])
    assert good.verified, good.to_dict()
    assert ("object" if isinstance(raw, dict) else "array") in good.children[0].scope
    bad = probe_context(
        {"value": {"@id": EX + "wrong", "@container": container}}, [probe]
    )
    assert not bad.verified
    assert "context.mapping_mismatch" in {issue.code for issue in bad.issues}


@pytest.mark.parametrize("container", ["@language", "@index", "@id", "@type"])
def test_map_container_kind_is_unverified_without_observed_key_meaning(
    container: str,
) -> None:
    model = ModelDeclaration(
        "record",
        (),
        (
            FieldDeclaration(
                "value",
                EX + "value",
                (ValueDeclaration("literal", container=container),),
            ),
        ),
    )
    probe = ContextProbe(model, samples=(ProbeSample("value", 0, {"en": "label"}),))
    for actual_container in ("@language", "@index"):
        report = probe_context(
            {"value": {"@id": EX + "value", "@container": actual_container}}, [probe]
        )
        assert not report.verified and not report.complete
        assert report.requested == 1 and len(report.children) == 1
        assert {issue.code for issue in report.issues} == {"context.probe_unverified"}


def test_fieldless_selected_probes_remain_required_beside_valid_siblings() -> None:
    valid = ContextProbe(
        ModelDeclaration(
            "valid",
            (),
            (FieldDeclaration("value", EX + "value", (ValueDeclaration("literal"),)),),
        )
    )
    empty = ModelDeclaration("empty", (EX + "Expected",))
    context = {
        "expected": EX + "Expected",
        "wrong": EX + "Wrong",
        "value": EX + "value",
    }
    good = probe_context(
        context, [ContextProbe(empty, type_values=("expected",)), valid]
    )
    assert good.verified and good.checked == good.requested == 2
    assert len(good.children) == 2
    wrong = probe_context(context, [ContextProbe(empty, type_values=("wrong",)), valid])
    assert not wrong.verified and wrong.requested == 2
    assert "context.types_mismatch" in {issue.code for issue in wrong.issues}
    unknown = probe_context(
        context, [ContextProbe(ModelDeclaration("unscoped", ())), valid]
    )
    assert not unknown.verified and not unknown.complete
    assert unknown.checked == 1 and unknown.requested == 2
    assert len(unknown.children) == 2
    assert {issue.code for issue in unknown.issues} == {"context.probe_unverified"}


@pytest.mark.parametrize("predicate", ["@id", "@type"])
@pytest.mark.parametrize("typed", [False, True])
def test_metadata_alias_fields_are_unverified_without_invented_collisions(
    predicate: str, typed: bool
) -> None:
    model = ModelDeclaration(
        "record",
        (EX + "Record",),
        (
            FieldDeclaration(
                "metadata", predicate, (ValueDeclaration("iri"),), key="alias"
            ),
        ),
    )
    probe = ContextProbe(model, type_values=("kind",) if typed else ())
    report = probe_context({"alias": predicate, "kind": EX + "Record"}, [probe])
    assert not report.verified and not report.complete
    assert report.requested == 1 and len(report.children) == 1
    assert {issue.code for issue in report.issues} == {"context.probe_unverified"}


def test_unknown_unrepresented_and_unsupported_branches_never_claim_coverage() -> None:
    model = ModelDeclaration(
        "record",
        (),
        (
            FieldDeclaration("unknown", EX + "unknown", (ValueDeclaration(),)),
            FieldDeclaration("object", EX + "object", (ValueDeclaration("object"),)),
            FieldDeclaration(
                "custom",
                EX + "custom",
                (ValueDeclaration("literal", "urn:custom:datatype"),),
            ),
            FieldDeclaration(
                "map", EX + "map", (ValueDeclaration("literal", container="@index"),)
            ),
            FieldDeclaration(
                "graph", EX + "graph", (ValueDeclaration("object", container="@graph"),)
            ),
        ),
    )
    report = probe_context({}, [ContextProbe(model)])
    assert not report.verified and not report.complete
    assert report.checked == 0 and report.requested == 5
    assert len(report.children) == 5
    assert {issue.code for issue in report.issues} == {"context.probe_unverified"}


def test_owned_json_and_authored_lexical_samples_exercise_each_representative() -> None:
    raw = {"@id": EX + "child", EX + "label": "sample"}
    sample = ProbeSample("child", 0, raw)
    raw[EX + "label"] = "changed"
    returned = sample.value
    assert isinstance(returned, dict)
    returned.clear()
    assert sample.value == {"@id": EX + "child", EX + "label": "sample"}
    model = ModelDeclaration(
        "record",
        (),
        (
            FieldDeclaration("child", EX + "child", (ValueDeclaration("object"),)),
            FieldDeclaration(
                "label",
                EX + "label",
                (
                    ValueDeclaration(
                        "literal",
                        str(XSD.string),
                        samples=(Literal("one"), Literal("two")),
                    ),
                ),
            ),
        ),
    )
    report = probe_context(
        {"child": EX + "child", "label": EX + "label"},
        [ContextProbe(model, samples=(sample,))],
    )
    assert report.verified and report.checked == report.requested == 3
    assert len(report.children) == 3
    with pytest.raises(InputError):
        ContextProbe(model, samples=(ProbeSample("missing", 0, 1),))


def test_offline_resources_are_shared_and_limits_never_erase_prior_findings() -> None:
    context, probe = contract()
    calls = []

    def loader(url: str, options: dict[str, object]) -> dict[str, object]:
        calls.append(url)
        return {
            "documentUrl": url,
            "contextUrl": None,
            "document": {"@context": context},
        }

    assert probe_context(EX + "context", [probe], document_loader=loader).verified
    assert calls == [EX + "context"]
    context["title"] = EX + "wrong"
    report = probe_context(context, [probe], limits=Limits(max_work=500))
    assert not report.verified and not report.complete
    assert any(issue.kind == "exhausted" for issue in report.issues), report.to_dict()
    assert any(issue.code == "context.mapping_mismatch" for issue in report.issues), (
        report.to_dict()
    )
    states = probe_context(context, [probe], limits=Limits(max_states=2))
    assert states.outcome == "exhausted" and not states.verified


def test_metadata_and_sample_bounds_apply_before_probe_processing() -> None:
    model = ModelDeclaration(
        "record",
        (),
        (FieldDeclaration("value", EX + "value", (ValueDeclaration("literal"),)),),
    )
    sample = ProbeSample("value", 0, "large " * 100)
    probe = ContextProbe(model, samples=(sample,))
    bounded = probe_context({}, [probe], limits=Limits(max_bytes=100))
    assert bounded.outcome == "exhausted" and not bounded.children
    metadata = ContextProbe(ModelDeclaration("large " * 100, (), model.fields))
    bounded = probe_context({}, [metadata], limits=Limits(max_bytes=100))
    assert bounded.outcome == "exhausted" and not bounded.children
    bounded = probe_context({}, [probe], limits=Limits(max_work=1))
    assert bounded.outcome == "exhausted" and not bounded.children


def test_type_set_qualification_owns_the_selected_probes() -> None:
    model = ModelDeclaration(
        "record",
        (EX + "First", EX + "Second"),
        (FieldDeclaration("value", EX + "value", (ValueDeclaration("literal"),)),),
    )
    selected = [ContextProbe(model, type_values=("second", "first"))]

    def loader(url: str, options: dict[str, object]) -> dict[str, object]:
        selected.clear()
        return {
            "documentUrl": url,
            "contextUrl": None,
            "document": {
                "@context": {
                    "first": EX + "First",
                    "second": EX + "Second",
                    "value": EX + "value",
                }
            },
        }

    report = probe_context(EX + "context", selected, document_loader=loader)
    assert report.verified and report.checked == 1
    assert not selected

    class IndexedProbes(list[ContextProbe]):
        def __iter__(self) -> Iterator[ContextProbe]:
            raise AssertionError("Capture must use the bounded indexed selection")

    assert probe_context(
        {"first": EX + "First", "second": EX + "Second", "value": EX + "value"},
        IndexedProbes([ContextProbe(model, type_values=("first", "second"))]),
    ).verified


@pytest.mark.parametrize(
    "value", [{"@id": EX + "child"}, {"@id": "_:child"}, {EX + "label": "child"}]
)
def test_object_representatives_support_references_and_descriptions(
    value: object,
) -> None:
    model = ModelDeclaration(
        "record",
        (),
        (FieldDeclaration("child", EX + "child", (ValueDeclaration("object"),)),),
    )
    probe = ContextProbe(model, samples=(ProbeSample("child", 0, value),))
    good = probe_context({"child": EX + "child"}, [probe])
    assert good.verified, good.to_dict()
    wrong = probe_context({"child": EX + "wrong"}, [probe])
    assert "context.mapping_mismatch" in {issue.code for issue in wrong.issues}
    literal = ContextProbe(
        model, samples=(ProbeSample("child", 0, {"@value": "literal"}),)
    )
    rejected = probe_context({"child": EX + "child"}, [literal])
    assert "context.value_kind_mismatch" in {issue.code for issue in rejected.issues}
