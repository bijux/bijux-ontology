"""Caller values yield checked examples against original shape/focus pairs."""

import importlib.util
import threading

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace
from rdflib.namespace import RDF, SH, XSD

from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.sources.types import RdfSource
from bijux_ontology.shacl.examples import MissingValueExample, generate_examples

EX = Namespace("https://example.invalid/examples/")
_PREFIX = f"@prefix ex:<{EX}>. @prefix sh:<{SH}>. @prefix xsd:<{XSD}>. "


def _shapes(body: str) -> PreparedSource:
    """Read the original unchanged named shape definitions."""
    return prepare_rdf(Graph().parse(data=_PREFIX + body, format="turtle"))


def _seed() -> PreparedSource:
    """Keep an explicit caller-owned focus type and unrelated compact facts."""
    graph = Graph()
    graph.add((EX.focus, RDF.type, EX.Record))
    graph.add((EX.focus, EX.anchor, Literal("retained")))
    return prepare_rdf(graph)


def _status() -> PreparedSource:
    """Select status and label as individually named original obligations."""
    return _shapes(
        "ex:R a sh:NodeShape; sh:targetClass ex:Record; "
        "sh:property ex:Status,ex:Label. "
        "ex:Status sh:path ex:status; sh:minCount 1; sh:maxCount 1; "
        "sh:in (ex:ready). "
        "ex:Label sh:path ex:label; sh:minCount 1; sh:maxCount 1; "
        "sh:datatype xsd:string; sh:nodeKind sh:Literal."
    )


def test_caller_values_positive_and_named_missing_status_keep_original_binding() -> (
    None
):
    """A negative example retains raw failure and its checked preserved label."""
    pytest.importorskip("pyshacl")
    seed = _seed()
    result = generate_examples(
        _status(),
        EX.R,
        EX.focus,
        seed=seed,
        values=(EX.ready, Literal("sample")),
        negative=MissingValueExample(EX.Status, (EX.Label,)),
    )
    assert result.status == "found" and result.report.complete
    assert result.positive is not None and result.negative is not None
    positive, negative = result.positive, result.negative
    assert positive.validation.report.verified and positive.validation.conforms is True
    assert negative.validation.conforms is False
    assert result.report.outcome == "disagreement"
    assert all(
        item.conforms is True and item.report.verified for item in negative.preserved
    )
    assert any(
        item.focus_node == EX.focus
        and item.source_shape == EX.Status
        and item.constraint_component == SH.MinCountConstraintComponent
        for item in negative.validation.findings
    )
    for example in (positive, negative):
        graph = example.source.graph()
        assert set(seed.graph()) <= set(graph)
        assert (EX.focus, EX.label, Literal("sample")) in graph
        assert example.validation.shapes == (str(EX.R),)
        assert example.validation.focus_nodes == (str(EX.focus),)
    assert not tuple(negative.source.graph().objects(EX.focus, EX.status))


def test_contradictory_or_alternative_does_not_poison_valid_nested_branch() -> None:
    """Finite branch construction reuses original refs and explicit child IRIs."""
    pytest.importorskip("pyshacl")
    shapes = _shapes(
        "ex:R a sh:NodeShape; sh:or (ex:Bad ex:Good). "
        "ex:Bad sh:property ex:Impossible. ex:Impossible sh:path ex:value; "
        "sh:minCount 2; sh:maxCount 1. "
        "ex:Good sh:property ex:Child. ex:Child sh:path ex:child; sh:minCount 1; "
        "sh:maxCount 1; sh:nodeKind sh:IRI; sh:node ex:Nested. "
        "ex:Nested sh:property ex:Name. ex:Name sh:path ex:name; sh:minCount 1; "
        "sh:maxCount 1; sh:datatype xsd:string; sh:nodeKind sh:Literal."
    )
    result = generate_examples(
        shapes,
        EX.R,
        EX.focus,
        seed=_seed(),
        values=(EX.child, Literal("sample")),
    )
    assert result.status == "found" and result.positive is not None
    assert result.report.verified
    graph = result.positive.source.graph()
    assert (EX.focus, EX.child, EX.child) in graph
    assert (EX.child, EX.name, Literal("sample")) in graph
    assert result.positive.validation.conforms is True


def test_explicit_pool_failure_and_trial_window_do_not_invent_values() -> None:
    """No candidate from an incomplete caller pool is presented as scientific truth."""
    pytest.importorskip("pyshacl")
    missing = generate_examples(
        _status(), EX.R, EX.focus, seed=_seed(), values=(Literal("label"),)
    )
    assert missing.status == "search_exhausted" and missing.positive is None
    assert not missing.report.verified
    shapes = _shapes(
        "ex:R a sh:NodeShape; sh:property ex:P. ex:P sh:path ex:value; "
        "sh:minCount 1; sh:maxCount 1; sh:datatype xsd:integer; sh:minInclusive 2."
    )
    capped = generate_examples(
        shapes,
        EX.R,
        EX.focus,
        seed=_seed(),
        values=(Literal("bad"), Literal(3)),
        max_trials=1,
    )
    assert capped.status == "search_exhausted" and capped.positive is None
    assert len(capped.trials) == 1 and capped.trials[0].conforms is False
    assert not capped.report.verified
    found = generate_examples(
        shapes, EX.R, EX.focus, seed=_seed(), values=(Literal(3),)
    )
    assert found.positive is not None and found.positive.validation.report.verified


def test_exact_rdf_cardinality_and_native_facets_are_both_checked() -> None:
    """Lexical aliases remain distinct RDF values; native facets check values."""
    pytest.importorskip("pyshacl")
    graph = Graph().parse(
        data=_PREFIX + "ex:R a sh:NodeShape; sh:property ex:P. ex:P sh:path ex:value; "
        "sh:minCount 2; sh:maxCount 2; sh:datatype xsd:integer; sh:minInclusive 1.",
        format="turtle",
    )
    one = Literal("01", datatype=XSD.integer, normalize=False)
    another = Literal("1", datatype=XSD.integer, normalize=False)
    result = generate_examples(
        prepare_rdf(graph),
        EX.R,
        EX.focus,
        seed=_seed(),
        values=(one, another),
    )
    assert result.positive is not None and result.positive.validation.conforms
    assert set(result.positive.source.graph().objects(EX.focus, EX.value)) == {
        one,
        another,
    }


def test_unsupported_closure_and_absent_focus_never_vacuously_validate() -> None:
    """Original recursion, queries and complex paths remain required unknown."""
    for body in (
        "ex:R a sh:NodeShape; sh:node ex:R.",
        'ex:R a sh:NodeShape; sh:sparql [sh:select "SELECT $this WHERE {}"].',
        "ex:R a sh:NodeShape; sh:property "
        "[sh:path [sh:inversePath ex:value]; sh:minCount 1].",
    ):
        actual = generate_examples(
            _shapes(body), EX.R, EX.focus, seed=_seed(), values=()
        )
        assert actual.status == "unsupported" and actual.positive is None
        assert not actual.report.verified
    empty = generate_examples(
        _status(), EX.R, EX.focus, seed=prepare_rdf(Graph()), values=()
    )
    assert empty.status == "input_error" and empty.report.outcome == "input_error"
    foreign = Graph()
    foreign.add((EX.other, EX.anchor, Literal("present")))
    absent = generate_examples(
        _status(), EX.R, EX.focus, seed=prepare_rdf(foreign), values=()
    )
    assert absent.positive is None and not absent.report.verified


def test_owned_blank_root_and_physical_graph_selection_are_preserved() -> None:
    """Equal anonymous labels cannot replace original shapes authority."""
    pytest.importorskip("pyshacl")
    dataset = Dataset()
    selected = dataset.graph(EX.shapes)
    root = BNode("root")
    selected.add((root, RDF.type, SH.NodeShape))
    selected.add((root, SH.property, EX.P))
    selected.add((EX.P, SH.path, EX.status))
    selected.add((EX.P, SH.minCount, Literal(1)))
    dataset.graph(EX.unselected).add((EX.R, SH.node, EX.R))
    shapes = prepare_rdf(RdfSource(dataset, graph_name=EX.shapes))
    original = next(node for node in shapes.graph().subjects(RDF.type, SH.NodeShape))
    assert isinstance(original, BNode)
    valid = generate_examples(
        shapes, original, EX.focus, seed=_seed(), values=(EX.ready,)
    )
    assert valid.status == "found" and valid.positive is not None
    assert (
        valid.shapes_metadata is not None
        and valid.shapes_metadata.graph_name == str(EX.shapes)
    )
    wrong = generate_examples(
        shapes, BNode(str(original)), EX.focus, seed=_seed(), values=(EX.ready,)
    )
    assert wrong.positive is None and not wrong.report.verified


def test_shared_limits_cancel_and_missing_optional_backend_preserve_truth() -> None:
    """No incomplete native execution is a positive example or fresh allowance."""
    operation = Budget(Limits(max_work=1))
    actual = generate_examples(
        _status(), EX.R, EX.focus, seed=_seed(), values=(EX.ready,), budget=operation
    )
    assert actual.status == "exhausted" and not actual.report.verified
    assert 0 <= operation.remaining("work") <= 1
    cancel = threading.Event()
    cancel.set()
    stopped = generate_examples(
        _status(), EX.R, EX.focus, seed=_seed(), values=(), cancel=cancel
    )
    assert stopped.status == "exhausted" and stopped.positive is None
    cancel.clear()
    available = generate_examples(
        _status(),
        EX.R,
        EX.focus,
        seed=_seed(),
        values=(EX.ready, Literal("label")),
        cancel=cancel,
    )
    assert available.status == "found" and available.positive is not None
    assert available.positive.validation.report.verified
    if importlib.util.find_spec("pyshacl") is None:
        absent = generate_examples(
            _status(), EX.R, EX.focus, seed=_seed(), values=(EX.ready, Literal("label"))
        )
        assert absent.positive is None and not absent.report.verified


def test_preparation_exhaustion_retains_already_paid_profile_receipts() -> None:
    """A cutoff before preparation returns cannot erase its paid diagnostics."""
    budget = Budget(Limits(max_work=10000))
    result = generate_examples(
        _status(),
        EX.R,
        EX.focus,
        seed=_seed(),
        values=(EX.ready, Literal("sample")),
        negative=MissingValueExample(EX.Status, (EX.Label,)),
        budget=budget,
    )
    assert result.status == "exhausted" and not result.report.verified
    assert any(
        item.code == "shacl.profile_behavior_unverified"
        for item in result.report.issues
    )
