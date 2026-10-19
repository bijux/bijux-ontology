"""Closed local declarations never replace actual data or mandatory baselines."""

import importlib.util
import json
from dataclasses import replace
from pathlib import Path
from typing import Literal as Policy
from typing import cast

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF

from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.workflows.projects.profiles.composition import compose_profile
from bijux_ontology.workflows.projects.profiles.loading import load_profile
from bijux_ontology.workflows.projects.routing import (
    ProfileJobBinding,
    ProfilePlan,
    check_profile_routes,
)

EX = Namespace("https://example.invalid/routing/")


def _plan(
    directory: Path,
    name: str,
    predicate: URIRef,
    *,
    minimum: int = 1,
    body: str = "",
    unsupported: bool = False,
    invalid_static: bool = False,
) -> ProfilePlan:
    """Build an ordinary local profile whose bundled fixture deliberately passes."""
    directory.mkdir()
    iri = str(EX[name])
    shape = URIRef(iri + "/Shape")
    (directory / "terms.ttl").write_text(
        "<urn:Term> a <http://www.w3.org/2002/07/owl#Class>."
    )
    (directory / "terms.json").write_text(
        '["urn:Missing"]' if invalid_static else '["urn:Term"]'
    )
    (directory / "data.ttl").write_text(f'<urn:fixture> <{predicate}> "value".')
    (directory / "shapes.ttl").write_text(
        "@prefix sh:<http://www.w3.org/ns/shacl#>. "
        f"<{shape}> a sh:NodeShape;sh:targetNode <urn:fixture>; "
        f"sh:property [sh:path <{predicate}>;sh:minCount {minimum}]." + body
    )
    jobs: list[dict[str, object]] = [
        {
            "name": "terms",
            "kind": "vocabulary",
            "source": "terms",
            "inventory": "terms.json",
        },
        {
            "name": "shape",
            "kind": "shacl",
            "document": "data",
            "shapes": "shapes",
            "use_shapes": [str(shape)],
        },
    ]
    if invalid_static:
        jobs[1]["depends_on"] = ["terms"]
    if unsupported:
        (directory / "document.json").write_text("{}")
        (directory / "checks.json").write_text("[]")
        jobs.append(
            {
                "name": "context",
                "kind": "context",
                "document": "document.json",
                "expectations": "checks.json",
            }
        )
    (directory / "project.json").write_text(
        json.dumps(
            {
                "version": 2 if invalid_static else 1,
                "rdf": {
                    key: {"path": key + ".ttl", "iri": iri + "/" + key}
                    for key in ("terms", "data", "shapes")
                },
                "checks": jobs,
            }
        )
    )
    (directory / "profile.json").write_text(
        json.dumps(
            {
                "version": 1,
                "id": iri,
                "shapes": [{"source": "shapes", "roots": [str(shape)]}],
            }
        )
    )
    profile = compose_profile(load_profile(directory), {})
    assert profile.report.verified, profile.report.to_dict()
    return ProfilePlan(profile, (ProfileJobBinding(iri, "shape", "data", (shape,)),))


def _data(*, missing_label: bool = False) -> Graph:
    """Independent actual records select two different plans and share one baseline."""
    graph = Graph()
    for record, kind, profile, predicate in (
        (EX.a, EX.TypeA, EX.domainA, EX.aValue),
        (EX.b, EX.TypeB, EX.domainB, EX.bValue),
    ):
        graph.add((record, RDF.type, kind))
        graph.add((record, EX.profile, profile))
        graph.add((record, predicate, Literal("actual")))
        if not missing_label or record != EX.a:
            graph.add((record, EX.label, Literal("name")))
    return graph


def test_actual_records_route_distinct_plans_plus_baseline_not_bundle_fixtures(
    tmp_path: Path,
) -> None:
    """Actual supplied failure survives even though the owned fixture passes."""
    pytest.importorskip("pyshacl")
    baseline = _plan(tmp_path / "baseline", "baseline", EX.label)
    first = _plan(tmp_path / "a", "domainA", EX.aValue)
    second = _plan(tmp_path / "b", "domainB", EX.bValue)
    assert baseline.profile.check(tmp_path / "fixture").report.verified
    routes = check_profile_routes(
        prepare_rdf(_data()),
        (EX.a, EX.b),
        predicates=(EX.profile,),
        registry={str(EX.domainA): first, str(EX.domainB): second},
        baseline=(baseline,),
        destination=tmp_path / "routed",
    )
    assert routes.report.verified and not routes.pending and len(routes.records) == 2
    assert all(len(record.plans) == 2 for record in routes.records)
    assert routes.records[0].plans[1].profile == str(EX.domainA)
    assert routes.records[1].plans[1].profile == str(EX.domainB)
    failing = check_profile_routes(
        prepare_rdf(_data(missing_label=True)),
        (EX.a, EX.b),
        predicates=(EX.profile,),
        registry={str(EX.domainA): first, str(EX.domainB): second},
        baseline=(baseline,),
        destination=tmp_path / "failing",
    )
    assert not failing.report.verified
    assert failing.records[0].plans[0].report.outcome == "disagreement"
    assert failing.records[0].plans[1].report.verified
    assert failing.records[1].report.verified


def test_unknown_missing_weaker_and_multiple_declarations_cannot_skip_baselines(
    tmp_path: Path,
) -> None:
    """Closed registry applicability and mandatory data obligations stay independent."""
    pytest.importorskip("pyshacl")
    baseline = _plan(tmp_path / "base", "baseline", EX.label)
    weak = _plan(tmp_path / "weak", "weak", EX.label, minimum=0)
    graph = (
        Graph()
        .add((EX.a, EX.profile, EX.weak))
        .add((EX.b, EX.profile, URIRef("https://unregistered.invalid/profile")))
        .add((EX.c, RDF.type, EX.Record))
    )
    routed = check_profile_routes(
        prepare_rdf(graph),
        (EX.a, EX.b, EX.c),
        predicates=(EX.profile,),
        registry={str(EX.weak): weak},
        baseline=(baseline,),
        destination=tmp_path / "routes",
    )
    assert len(routed.records) == 3 and all(
        record.plans[0].baseline for record in routed.records
    )
    assert all(
        record.plans[0].report.outcome == "disagreement" for record in routed.records
    )
    assert routed.records[0].plans[1].report.verified
    assert {issue.code for issue in routed.records[1].applicability.issues} == {
        "profile.declaration_unknown"
    }
    assert {issue.code for issue in routed.records[2].applicability.issues} == {
        "profile.declaration_missing"
    }
    malformed = replace(weak, profile=replace(weak.profile, iri="relative"))
    retained = check_profile_routes(
        prepare_rdf(graph),
        (EX.a,),
        predicates=(EX.profile,),
        registry={},
        baseline=(baseline, malformed),
        require_declaration=False,
        destination=tmp_path / "malformed-later-plan",
    )
    assert len(retained.records) == 1 and len(retained.records[0].plans) == 2
    assert retained.records[0].plans[0].report.outcome == "disagreement"
    assert retained.records[0].plans[0].result is not None
    assert retained.records[0].plans[1].report.outcome == "unverified"
    assert retained.records[0].plans[1].result is None
    assert not retained.report.verified and not retained.pending
    graph.add((EX.a, EX.profile, EX.baseline)).add((EX.a, EX.label, Literal("value")))
    selected = prepare_rdf(graph)
    for policy, expected in (("reject", 1), ("all", 3)):
        result = check_profile_routes(
            selected,
            (EX.a,),
            predicates=(EX.profile,),
            registry={str(EX.weak): weak, str(EX.baseline): baseline},
            baseline=(baseline,),
            multiple=cast(Policy["reject", "all"], policy),
            destination=tmp_path / policy,
        )
        assert len(result.records[0].plans) == expected
        assert result.report.verified is (policy == "all")


def test_graph_scope_shared_children_and_owned_blank_coverage_are_truthful(
    tmp_path: Path,
) -> None:
    """Routing retains the actual selected graph rather than a guessed record slice."""
    pytest.importorskip("pyshacl")
    plan = _plan(
        tmp_path / "base",
        "baseline",
        EX.child,
        body=f"<{EX.baseline}/Shape> <http://www.w3.org/ns/shacl#property> "
        f"[<http://www.w3.org/ns/shacl#path> <{EX.child}>; "
        f"<http://www.w3.org/ns/shacl#node> [<http://www.w3.org/ns/shacl#property> "
        f"[<http://www.w3.org/ns/shacl#path> <{EX.label}>;"
        "<http://www.w3.org/ns/shacl#minCount> 1]]].",
    )
    dataset = Dataset()
    child = BNode()
    for record in (EX.a, EX.b):
        dataset.graph(EX.g).add((record, EX.child, child))
    dataset.graph(EX.g).add((child, EX.label, Literal("shared")))
    dataset.graph(EX.h).add((EX.hidden, EX.label, Literal("hidden")))
    source = prepare_rdf(RdfSource(dataset, graph_name=str(EX.g)))
    good = check_profile_routes(
        source,
        (EX.a, EX.b),
        predicates=(EX.profile,),
        registry={},
        baseline=(plan,),
        require_declaration=False,
        destination=tmp_path / "good",
    )
    assert good.report.verified
    hidden = check_profile_routes(
        source,
        (EX.hidden,),
        predicates=(EX.profile,),
        registry={},
        baseline=(plan,),
        destination=tmp_path / "hidden",
    )
    assert (
        hidden.report.outcome == "unverified"
        and hidden.records[0].plans[0].result is None
    )
    owned = next(
        node
        for node in source.graph().objects(EX.a, EX.child)
        if isinstance(node, BNode)
    )
    for index, blank_record in enumerate((owned, BNode(str(owned)))):
        result = check_profile_routes(
            source,
            (blank_record,),
            predicates=(EX.profile,),
            registry={},
            baseline=(plan,),
            require_declaration=False,
            destination=tmp_path / f"blank-{index}",
        )
        assert not result.report.verified and result.records[0].plans[0].result is None
        assert bool(result.records[0].applicability.issues) is bool(index)


def test_plan_binding_and_actual_execution_coverage_cannot_be_weakened(
    tmp_path: Path,
) -> None:
    """Wrong roots/aliases, unsupported jobs and deactivation remain required."""
    pytest.importorskip("pyshacl")
    parent = _plan(tmp_path / "parent", "parent", EX.label, invalid_static=True)
    child = _plan(tmp_path / "child", "child", EX.label, minimum=0)
    manifest_path = tmp_path / "child/profile.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["parents"] = [{"id": str(EX.parent)}]
    manifest_path.write_text(json.dumps(manifest))
    inherited = compose_profile(
        load_profile(tmp_path / "child"),
        {str(EX.parent): load_profile(tmp_path / "parent")},
    )
    required = ProfilePlan(inherited, (*parent.bindings, *child.bindings))
    inherited_result = check_profile_routes(
        prepare_rdf(_data()),
        (EX.a,),
        predicates=(EX.profile,),
        registry={},
        baseline=(required,),
        require_declaration=False,
        destination=tmp_path / "inherited",
    )
    assert not inherited_result.report.verified
    assert any(
        issue.code == "vocabulary.term_missing"
        for issue in inherited_result.report.issues
    )
    assert any(
        issue.code == "project.dependency_unverified"
        for issue in inherited_result.report.issues
    )
    inherited_raw = inherited_result.records[0].plans[0].result
    assert inherited_raw is not None and inherited_raw.raw_reports[-1][1].verified
    bounded_prefix = check_profile_routes(
        prepare_rdf(Graph().add((EX.a, EX.label, Literal("name")))),
        (EX.a, EX.b),
        predicates=(EX.profile,),
        registry={},
        baseline=(required,),
        require_declaration=False,
        destination=tmp_path / "finding-prefix",
        limits=Limits(max_issues=1),
    )
    assert bounded_prefix.report.outcome == "exhausted" and bounded_prefix.pending == 1
    assert any(
        issue.code == "vocabulary.term_missing"
        for issue in bounded_prefix.report.issues
    )
    assert bounded_prefix.records[0].plans[0].result is not None
    plan = _plan(tmp_path / "base", "baseline", EX.label)
    graph = prepare_rdf(Graph().add((EX.a, EX.label, Literal("value"))))
    wrong = ProfilePlan(plan.profile, (replace(plan.bindings[0], shapes=(EX.Weaker,)),))
    missing = ProfilePlan(plan.profile, ())
    other = ProfilePlan(
        plan.profile, (replace(plan.bindings[0], data_resource="other"),)
    )
    unsupported = _plan(
        tmp_path / "unsupported", "unsupported", EX.label, unsupported=True
    )
    inactive = _plan(
        tmp_path / "inactive",
        "inactive",
        EX.label,
        body=f"<{EX.inactive}/Shape> <http://www.w3.org/ns/shacl#deactivated> true.",
    )
    for index, selected in enumerate((wrong, missing, other, unsupported, inactive)):
        result = check_profile_routes(
            graph,
            (EX.a,),
            predicates=(EX.profile,),
            registry={},
            baseline=(selected,),
            require_declaration=False,
            destination=tmp_path / f"route-{index}",
        )
        assert not result.report.verified
        assert (
            result.records[0].plans[0].result is None
            if index < 4
            else result.records[0].plans[0].result is not None
        )

    # A resource inventory is the union of its jobs; each job retains its own roots.
    directory = tmp_path / "base"
    config = json.loads((directory / "project.json").read_text())
    extra_shape = URIRef(str(EX.baseline) + "/OtherShape")
    with (directory / "shapes.ttl").open("a") as handle:
        handle.write(
            f"<{extra_shape}> a sh:NodeShape; sh:property "
            f"[sh:path <{EX.label}>;sh:minCount 1]."
        )
    config["checks"].append(
        {
            "name": "other-shape",
            "kind": "shacl",
            "document": "data",
            "shapes": "shapes",
            "use_shapes": [str(extra_shape)],
        }
    )
    (directory / "project.json").write_text(json.dumps(config))
    manifest = json.loads((directory / "profile.json").read_text())
    manifest["shapes"][0]["roots"].append(str(extra_shape))
    (directory / "profile.json").write_text(json.dumps(manifest))
    combined = compose_profile(load_profile(directory), {})
    two_jobs = ProfilePlan(
        combined,
        (
            plan.bindings[0],
            replace(plan.bindings[0], job="other-shape", shapes=(extra_shape,)),
        ),
    )
    result = check_profile_routes(
        graph,
        (EX.a,),
        predicates=(EX.profile,),
        registry={},
        baseline=(two_jobs,),
        require_declaration=False,
        destination=tmp_path / "two-jobs",
    )
    assert result.report.verified and result.records[0].plans[0].result is not None


def test_empty_required_scope_conversion_evidence_and_bounded_remaining_records(
    tmp_path: Path,
) -> None:
    """No input limitation, exhausted inventory or missing backend becomes green."""
    source = prepare_rdf(_data())
    empty = check_profile_routes(
        source,
        (),
        predicates=(EX.profile,),
        registry={},
        baseline=(),
        destination=tmp_path / "empty",
    )
    assert not empty.report.verified
    bounded = check_profile_routes(
        source,
        (EX.a, EX.b),
        predicates=(EX.profile,),
        registry={},
        baseline=(),
        destination=tmp_path / "bounded",
        limits=Limits(max_work=1),
    )
    assert bounded.report.outcome == "exhausted" and bounded.pending == 2
    evidence = Report(
        "jsonld-conversion",
        issues=(
            Issue("input.invalid", "Actual conversion incomplete", kind="input_error"),
        ),
        requested=1,
    )
    result = check_profile_routes(
        source,
        (EX.a,),
        predicates=(EX.profile,),
        registry={},
        baseline=(),
        destination=tmp_path / "conversion",
        input_report=evidence,
    )
    assert not result.report.verified and any(
        issue.code == "input.invalid" for issue in result.report.issues
    )
    plan = _plan(tmp_path / "base", "baseline", EX.label)
    if importlib.util.find_spec("pyld") is not None:
        from bijux_ontology.jsonld.preparation import prepare_jsonld

        converted = prepare_jsonld(
            {"@id": str(EX.a), str(EX.label): "name", "unmapped": "lost"}
        )
        assert converted.source is None and not converted.report.verified
        valid = prepare_jsonld({"@id": str(EX.a), str(EX.label): "name"})
        assert valid.source is not None and valid.report.verified
        # A replacement valid snapshot cannot hide a failed required conversion.
        actual = check_profile_routes(
            valid.source,
            (EX.a,),
            predicates=(EX.profile,),
            registry={},
            baseline=(plan,),
            require_declaration=False,
            input_report=converted.report,
            destination=tmp_path / "actual-jsonld",
        )
        assert not actual.report.verified
        assert actual.records[0].plans[0].report.verified
    if importlib.util.find_spec("pyshacl") is None:
        unavailable = check_profile_routes(
            source,
            (EX.a,),
            predicates=(EX.profile,),
            registry={},
            baseline=(plan,),
            require_declaration=False,
            destination=tmp_path / "missing-backend",
        )
        assert (
            not unavailable.report.verified
            and unavailable.records[0].plans[0].result is not None
        )
    spent = Budget(Limits(max_issues=1))
    spent.consume("issues")
    stopped = check_profile_routes(
        source,
        (EX.a, EX.b),
        predicates=(EX.profile,),
        registry={},
        baseline=(plan,),
        destination=tmp_path / "spent",
        budget=spent,
    )
    assert stopped.report.outcome == "exhausted" and not stopped.report.verified
