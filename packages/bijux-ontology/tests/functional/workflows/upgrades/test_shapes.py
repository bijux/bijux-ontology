"""Actual shape releases retain raw failures and distinguish target loss."""

import importlib.util
import threading
from typing import Any, cast

import pytest
from rdflib import BNode, Dataset, Namespace, URIRef
from rdflib.namespace import SH

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.limits import Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.shacl.profiles import ShapeProfile, extract_profile
from bijux_ontology.workflows.upgrades.shapes import (
    ShapeCorrespondence,
    ShapeSample,
    assess_shape_upgrade,
)

E = Namespace("urn:shape-release:")
PREFIX = """@prefix e:<urn:shape-release:>.
@prefix sh:<http://www.w3.org/ns/shacl#>.
@prefix xsd:<http://www.w3.org/2001/XMLSchema#>.
"""


def profile(text: str, root: URIRef = E.Root) -> ShapeProfile:
    """Prepare a real explicit selected shape closure without invented case truth."""
    return extract_profile(prepare_rdf(PREFIX + text, format="turtle"), (root,))


def backend() -> None:
    """Keep genuine core missing-capability controls independent of pySHACL."""
    if importlib.util.find_spec("pyshacl") is None:
        pytest.skip("Actual SHACL validation requires the optional backend")


def test_named_tightening_and_selective_constraint_removal() -> None:
    """Actual minCount/datatype failures are introduced and removed independently."""
    backend()
    first = profile("""e:Root a sh:NodeShape;sh:targetNode e:r,e:q;
        sh:property e:Name,e:Code.
        e:Name sh:path e:p;sh:minCount 0;sh:datatype xsd:string.
        e:Code sh:path e:code;sh:minCount 0.""")
    tightened = profile("""e:Root a sh:NodeShape;sh:targetNode e:r,e:q;
        sh:property e:Name,e:Code.
        e:Name sh:path e:p;sh:minCount 1;sh:datatype xsd:integer.
        e:Code sh:path e:code;sh:minCount 1.""")
    data = prepare_rdf(PREFIX + 'e:q e:p "wrong".', format="turtle")
    changed = assess_shape_upgrade(first, tightened, (ShapeSample("records", data),))
    assert changed.verdict == "demonstrated-regression", changed.report.to_dict()
    introduced = [x for x in changed.samples[0].findings if x.status == "introduced"]
    assert {f.constraint_component for x in introduced for f in x.after} == {
        SH.MinCountConstraintComponent,
        SH.DatatypeConstraintComponent,
    }
    assert changed.samples[0].before is not None
    assert changed.samples[0].after is not None
    assert changed.samples[0].before.conforms is True
    assert changed.samples[0].after.conforms is False
    removed = profile("""e:Root a sh:NodeShape;sh:targetNode e:r,e:q;
        sh:property e:Name,e:Code.
        e:Name sh:path e:p;sh:minCount 1.
        e:Code sh:path e:code;sh:minCount 1.""")
    resolved = assess_shape_upgrade(tightened, removed, (ShapeSample("records", data),))
    assert resolved.report.verified, resolved.report.to_dict()
    assert {
        f.constraint_component
        for x in resolved.samples[0].findings
        if x.status == "resolved"
        for f in x.before
    } == {SH.DatatypeConstraintComponent}
    assert any(x.status == "persistent" for x in resolved.samples[0].findings)
    assert resolved.samples[0].before is not None
    assert resolved.samples[0].after is not None
    assert resolved.samples[0].before.report_graph()
    assert resolved.samples[0].after.report_graph()
    # The first actual regression remains classified if a later selected
    # sample's own metadata cannot fit the same operation allowance.
    prefix = assess_shape_upgrade(
        first,
        tightened,
        (ShapeSample("first", data), ShapeSample("x" * 100001, data)),
        limits=Limits(max_bytes=50000),
    )
    assert prefix.report.outcome == "exhausted", prefix.report.to_dict()
    assert prefix.verdict == "demonstrated-regression"
    assert prefix.samples and any(
        item.status == "introduced" for item in prefix.samples[0].findings
    )
    dataset = Dataset()
    dataset.graph(E.selected).add((E.r, E.other, E.value))
    dataset.graph(E.hidden).add((E.r, E.p, E.value))
    pinned = prepare_rdf(RdfSource(dataset, graph_name=str(E.selected)))
    scoped = assess_shape_upgrade(first, tightened, (ShapeSample("pinned", pinned),))
    assert scoped.verdict == "demonstrated-regression"
    assert any(
        finding.focus_node == E.r
        and finding.constraint_component == SH.MinCountConstraintComponent
        for change in scoped.samples[0].findings
        if change.status == "introduced"
        for finding in change.after
    )


def test_anonymous_shape_and_path_churn_and_edited_unknown() -> None:
    """Joint RDF topology qualifies pure churn without binding edited blank shapes."""
    backend()
    text = """e:Root a sh:NodeShape;sh:targetNode e:r;
        sh:property [sh:path (e:p e:q);sh:minCount 1]."""
    before, after = profile(text), profile(text)
    data = prepare_rdf(PREFIX + "e:r e:other e:v.", format="turtle")
    same = assess_shape_upgrade(before, after, (ShapeSample("record", data),))
    assert same.report.verified, same.report.to_dict()
    assert same.samples[0].before is not None
    assert same.samples[0].after is not None
    assert same.samples[0].before.conforms is False
    assert same.samples[0].after.conforms is False
    assert same.samples[0].findings[0].status == "persistent"
    assert same.samples[0].findings[0].identity == ("joint-anonymous-results",)
    edited = profile(text.replace("minCount 1", "minCount 2"))
    unknown = assess_shape_upgrade(before, edited, (ShapeSample("record", data),))
    assert unknown.verdict == "unverified" and not unknown.report.verified
    assert any(x.status == "uncertain" for x in unknown.samples[0].findings)


def test_target_drop_is_not_constraint_resolution() -> None:
    """A release stopping evaluation cannot gain compatibility through vacuity."""
    backend()
    before = profile("""e:Root a sh:NodeShape;sh:targetNode e:r;
        sh:property e:Name. e:Name sh:path e:p;sh:minCount 1.""")
    after = profile("""e:Root a sh:NodeShape;
        sh:property e:Name. e:Name sh:path e:p;sh:minCount 1.""")
    data = prepare_rdf(PREFIX + "e:r e:other e:v.", format="turtle")
    result = assess_shape_upgrade(before, after, (ShapeSample("record", data),))
    assert result.verdict == "demonstrated-regression" and not result.report.verified
    assert result.samples[0].before is not None
    assert result.samples[0].after is not None
    assert result.samples[0].before.conforms is False
    assert result.samples[0].after.conforms is True
    assert any(x.status == "removed" for x in result.samples[0].targets)
    assert not any(x.status == "resolved" for x in result.samples[0].findings)
    assert result.samples[0].before.coverage is not None
    assert result.samples[0].after.coverage is not None
    assert result.samples[0].before.coverage.targets
    assert not result.samples[0].after.coverage.targets


def test_changed_violating_value_and_severity_are_not_persistent() -> None:
    """Equal counts cannot hide different failing values or changed severity."""
    backend()
    text = """e:Root a sh:NodeShape;sh:targetNode e:r;sh:property e:Allowed.
        e:Allowed sh:path e:p;sh:in ("A" "B")."""
    before = profile(text)
    after = profile(text.replace('"B"', '"C"'))
    data = prepare_rdf(PREFIX + 'e:r e:p "B", "C".', format="turtle")
    result = assess_shape_upgrade(before, after, (ShapeSample("values", data),))
    assert result.verdict == "demonstrated-regression"
    assert not result.report.verified
    changes = result.samples[0].findings
    assert {
        str(f.value) for x in changes if x.status == "resolved" for f in x.before
    } == {"C"}
    assert {
        str(f.value) for x in changes if x.status == "introduced" for f in x.after
    } == {"B"}
    assert not any(x.status == "persistent" for x in changes)
    warning = profile(
        text.replace('sh:in ("A" "B")', 'sh:in ("A");sh:severity sh:Warning')
    )
    violation = profile(
        text.replace('sh:in ("A" "B")', 'sh:in ("A");sh:severity sh:Violation')
    )
    changed_severity = assess_shape_upgrade(
        warning, violation, (ShapeSample("severity", data),)
    )
    assert changed_severity.verdict == "demonstrated-regression"
    changes = changed_severity.samples[0].findings
    assert {
        f.severity for x in changes if x.status == "resolved" for f in x.before
    } == {SH.Warning}
    assert {
        f.severity for x in changes if x.status == "introduced" for f in x.after
    } == {SH.Violation}
    assert not any(x.status == "persistent" for x in changes)


def test_named_correspondence_and_required_empty_samples() -> None:
    """Caller-established names survive renaming; absent actual cases stay unknown."""
    backend()
    before = profile("""e:Root a sh:NodeShape;sh:targetNode e:r;
        sh:property e:Old. e:Old sh:path e:p;sh:minCount 1.""")
    after = profile("""e:Root a sh:NodeShape;sh:targetNode e:r;
        sh:property e:New. e:New sh:path e:p;sh:minCount 1.""")
    data = prepare_rdf(PREFIX + "e:r e:other e:v.", format="turtle")
    result = assess_shape_upgrade(
        before,
        after,
        (ShapeSample("record", data),),
        correspondence=(ShapeCorrespondence("name", E.Old, E.New),),
    )
    assert result.report.verified, result.report.to_dict()
    assert all(x.status == "persistent" for x in result.samples[0].findings)
    assert not assess_shape_upgrade(before, after, ()).report.verified


def test_missing_capability_cancellation_and_metadata_bounds() -> None:
    """Core absence and incomplete work cannot authorize a comparison."""
    selected = profile("e:Root a sh:NodeShape;sh:targetNode e:r;sh:closed true.")
    data = prepare_rdf(PREFIX + "e:r e:other e:v.", format="turtle")
    sample = ShapeSample("record", data)
    if importlib.util.find_spec("pyshacl") is None:
        unavailable = assess_shape_upgrade(selected, selected, (sample,))
        assert not unavailable.report.verified and unavailable.verdict == "unverified"
    cancel = threading.Event()
    cancel.set()
    stopped = assess_shape_upgrade(selected, selected, (sample,), cancel=cancel)
    assert stopped.report.outcome == "exhausted"
    bounded = assess_shape_upgrade(
        selected,
        selected,
        (ShapeSample("x" * 100000, data),),
        limits=Limits(max_bytes=1000),
    )
    assert bounded.report.outcome == "exhausted"
    assert len(str(bounded.report.to_dict())) < 100000
    with pytest.raises(InputError):
        ShapeCorrespondence("foreign", cast(Any, BNode("same")), E.New)
