"""Supplied typed constraints retain identity, opaque RDF and actual behavior."""

import threading

import pytest
from rdflib import BNode, Namespace
from rdflib.namespace import SH

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.documents.export import export_rdf
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.paths import RdfPath
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.shacl.authoring import (
    ShaclCount,
    ShaclLogical,
    ShaclNodeShape,
    ShaclPropertyShape,
    ShaclShapeRef,
)
from bijux_ontology.shacl.checking import validate_shapes
from bijux_ontology.shacl.inspection import (
    ShapeCountEdit,
    ShapeInspection,
    edit_shape_counts,
    inspect_shapes,
)

E = Namespace("urn:shape-inspection:")
PREFIXES = """@prefix e:<urn:shape-inspection:>.
@prefix sh:<http://www.w3.org/ns/shacl#>.
@prefix xsd:<http://www.w3.org/2001/XMLSchema#>.
@prefix rdfs:<http://www.w3.org/2000/01/rdf-schema#>."""


def _shapes(extra: str = "") -> PreparedSource:
    """Supply independent anonymous shapes, logical references and metadata."""
    return prepare_source(
        PREFIXES
        + """
e:Root a sh:NodeShape; sh:targetNode e:record;
    sh:property _:amount; sh:or (_:left _:right).
_:amount a sh:PropertyShape; sh:path e:amount; sh:minCount 2;
    sh:datatype xsd:integer; rdfs:label "quantity".
_:left a sh:NodeShape; sh:hasValue e:record.
_:right a sh:NodeShape; sh:nodeKind sh:IRI.
"""
        + extra,
        format="turtle",
    )


def _data() -> PreparedSource:
    """Use real RDF data with one exact integer value."""
    return prepare_source(
        PREFIXES + 'e:record e:amount "01"^^xsd:integer.', format="turtle"
    )


def _property(view: ShapeInspection) -> ShaclPropertyShape:
    """Select the inspected property by its established typed path."""
    return next(
        d
        for d in view.definitions
        if isinstance(d, ShaclPropertyShape)
        and isinstance(d.path, RdfPath)
        and d.path.node == E.amount
    )


def test_nested_logical_shapes_edit_bound_and_export_real_behavior() -> None:
    source = _shapes()
    baseline = validate_shapes(_data(), source, use_shapes=(E.Root,), preflight=True)
    assert baseline.conforms is False and baseline.report.outcome == "disagreement"
    view = inspect_shapes(source, (E.Root,))
    assert view.report.verified
    definition = _property(view)
    assert isinstance(definition.identifier, BNode)
    assert ShaclCount(2, None) in definition.constraints
    assert any(
        isinstance(c, ShaclLogical) for d in view.definitions for c in d.constraints
    )
    edited = edit_shape_counts(
        view, (ShapeCountEdit(definition.identifier, ShaclCount(1, 3)),), data=_data()
    )
    assert edited.report.verified and edited.source is not None
    assert edited.validation is not None and edited.validation.conforms is True
    graph = edited.source._read()
    assert graph.value(E.Root, SH.property) is definition.identifier
    assert str(graph.value(definition.identifier, SH.minCount)) == "1"
    assert str(source._read().value(definition.identifier, SH.minCount)) == "2"
    exported = export_rdf(edited.source, format="turtle")
    assert exported.report.verified and exported.content is not None
    reloaded = prepare_source(exported.content, format="turtle")
    assert compare_rdf(edited.source, reloaded).verified
    assert (
        validate_shapes(
            _data(), reloaded, use_shapes=(E.Root,), preflight=True
        ).conforms
        is True
    )


def test_opaque_extensions_require_explicit_preservation_and_survive_export() -> None:
    source = _shapes(
        'e:Root e:extension _:config. _:config e:setting "preserved"; '
        "e:next _:tail. _:tail e:back _:config."
    )
    view = inspect_shapes(source, (E.Root,))
    assert not view.report.verified and view.remainder
    definition = _property(view)
    changes = (ShapeCountEdit(definition.identifier, ShaclCount(1, 3)),)
    assert edit_shape_counts(view, changes, data=_data()).source is None
    edited = edit_shape_counts(view, changes, data=_data(), allow_opaque=True)
    assert edited.source is not None and not edited.report.verified
    graph = edited.source._read()
    config = graph.value(E.Root, E.extension)
    assert graph.value(config, E.setting) == source._read().value(config, E.setting)
    assert graph.value(graph.value(config, E.next), E.back) == config
    exported = export_rdf(edited.source, format="turtle")
    assert exported.content is not None and exported.report.verified
    assert compare_rdf(
        edited.source, prepare_source(exported.content, format="turtle")
    ).verified


def test_recursive_references_and_distinct_blank_shapes_remain_records() -> None:
    source = _shapes(
        "e:Root sh:node _:recursive. _:recursive a sh:NodeShape; sh:node _:recursive."
    )
    view = inspect_shapes(source, (E.Root,))
    assert view.report.verified
    nodes = [d for d in view.definitions if isinstance(d, ShaclNodeShape)]
    recursive = next(d for d in nodes if ShaclShapeRef(d.identifier) in d.constraints)
    assert isinstance(recursive.identifier, BNode)
    assert len({d.identifier for d in nodes}) == 4
    assert len(view.definitions) == 5
    assert all(isinstance(d.identifier, BNode) for d in nodes if d.identifier != E.Root)


def test_foreign_blank_selectors_bounds_cancellation_and_fresh_recovery() -> None:
    view = inspect_shapes(_shapes(), (E.Root,))
    definition = _property(view)
    with pytest.raises(InputError, match="actual inspected"):
        edit_shape_counts(
            view,
            (ShapeCountEdit(BNode(str(definition.identifier)), ShaclCount(1, 3)),),
            data=_data(),
        )
    limited = inspect_shapes(_shapes(), (E.Root,), limits=Limits(max_work=1))
    assert limited.report.outcome == "exhausted"
    allowance = Budget()
    assert inspect_shapes(_shapes(), (E.Root,), budget=allowance).report.verified
    consumed = allowance.limits.max_work - allowance.remaining("work")
    projection_cutoff = inspect_shapes(
        _shapes(), (E.Root,), limits=Limits(max_work=consumed - 1)
    )
    assert projection_cutoff.profile.structural_report.verified
    assert projection_cutoff.report.outcome == "exhausted"
    changes = (ShapeCountEdit(definition.identifier, ShaclCount(1, 3)),)
    stopped = threading.Event()
    stopped.set()
    result = edit_shape_counts(view, changes, data=_data(), cancel=stopped)
    assert result.source is None and result.report.outcome == "exhausted"
    small = edit_shape_counts(view, changes, data=_data(), limits=Limits(max_bytes=1))
    assert small.source is None and small.report.outcome == "exhausted"
    assert edit_shape_counts(view, changes, data=_data()).report.verified


def test_selected_graph_and_required_root_coverage_remain_explicit() -> None:
    source = prepare_source(
        RdfSource(
            PREFIXES
            + """
e:chosen {e:Root a sh:NodeShape; sh:targetNode e:record; sh:property e:Amount.
    e:Amount a sh:PropertyShape; sh:path e:amount; sh:minCount 2.}
e:hidden {e:Root a sh:NodeShape; sh:targetNode e:record; sh:class e:Impossible.}
""",
            format="trig",
            graph_name=str(E.chosen),
        )
    )
    view = inspect_shapes(source, (E.Root,))
    assert view.report.verified
    result = edit_shape_counts(
        view, (ShapeCountEdit(E.Amount, ShaclCount(1)),), data=_data()
    )
    assert result.report.verified and result.source is not None
    assert E.hidden not in {g.identifier for g in result.source.dataset().graphs()}
    source = _shapes("e:Unexercised a sh:NodeShape; sh:nodeKind sh:IRI.")
    view = inspect_shapes(source, (E.Root, E.Unexercised))
    assert view.report.verified
    result = edit_shape_counts(
        view, (ShapeCountEdit(_property(view).identifier, ShaclCount(1)),), data=_data()
    )
    assert result.source is not None and not result.report.verified
    assert any(i.code == "shacl.shape_edit_unverified" for i in result.report.issues)
