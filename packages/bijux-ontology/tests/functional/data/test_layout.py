"""Original dataset partitions remain distinct from RDF content validity."""

from dataclasses import replace

import pytest
from rdflib import BNode, Dataset, Literal, Namespace, URIRef
from rdflib.namespace import RDF, XSD

from bijux_ontology.data.layout import (
    ContentPlacement,
    DatasetLayout,
    GraphRequirement,
    GraphSelector,
    check_dataset_layout,
)
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.sources.types import RdfSource

EX = Namespace("urn:layout:")
META = GraphSelector((EX.metadata,))
OBS = GraphSelector((EX.observations,))


def _dataset() -> Dataset:
    """Publish independent metadata, observations and an explicit empty partition."""
    dataset = Dataset()
    dataset.graph(EX.metadata).add(
        (EX.dataset, EX.title, Literal("Measurements", lang="en"))
    )
    dataset.graph(EX.observations).add((EX.record, RDF.type, EX.Observation))
    dataset.graph(EX.observations).add(
        (EX.record, EX.value, Literal("01", datatype=XSD.integer, normalize=False))
    )
    dataset.graph(EX.empty)
    return dataset


def _layout() -> DatasetLayout:
    """Route explicitly selected assertions without ontology or union reasoning."""
    return DatasetLayout(
        allowed=(GraphSelector((None, EX.metadata, EX.observations, EX.empty)),),
        requirements=(GraphRequirement("empty-published", GraphSelector((EX.empty,))),),
        placements=(
            ContentPlacement(
                "observations",
                OBS,
                predicates=(EX.value,),
                types=(EX.Observation,),
                minimum=2,
            ),
        ),
        closed=True,
    )


def test_actual_partitions_and_original_offending_statement() -> None:
    """Moving valid content fails with its actual graph and exact lexical term."""
    dataset = _dataset()
    source = prepare_rdf(dataset)
    assert check_dataset_layout(source, _layout()).verified
    triple = (EX.record, EX.value, Literal("01", datatype=XSD.integer, normalize=False))
    dataset.graph(EX.observations).remove(triple)
    dataset.graph(EX.metadata).add(triple)
    # Existing prepared input owns its contents independently from caller edits.
    assert check_dataset_layout(source, _layout()).verified
    moved = check_dataset_layout(prepare_rdf(dataset), _layout())
    issue = next(
        item for item in moved.issues if item.code == "data.layout_content_misplaced"
    )
    assert issue.evidence is not None
    assert issue.evidence["graph"] == {"kind": "iri", "value": str(EX.metadata)}
    statement = issue.evidence["statement"]
    assert isinstance(statement, list) and statement[2]["lexical"] == "01"
    assert statement[2]["datatype"] == str(XSD.integer)
    assert any(item.code == "data.layout_count_mismatch" for item in moved.issues)

    intrinsic = Dataset()
    intrinsic.graph(EX.observations).add((EX.record, EX.value, Literal("same")))
    intrinsic.graph(EX.observations).add(
        (EX.record, EX.value, Literal("same", datatype=XSD.string))
    )
    singleton = DatasetLayout(
        placements=(
            ContentPlacement(
                "one RDF assertion", OBS, predicates=(EX.value,), maximum=1
            ),
        )
    )
    assert check_dataset_layout(prepare_rdf(intrinsic), singleton).verified
    intrinsic.graph(EX.observations).add(
        (EX.record, EX.value, Literal("01", datatype=XSD.integer, normalize=False))
    )
    intrinsic.graph(EX.observations).add(
        (EX.record, EX.value, Literal("1", datatype=XSD.integer, normalize=False))
    )
    result = check_dataset_layout(
        prepare_rdf(intrinsic),
        replace(singleton, placements=(replace(singleton.placements[0], maximum=2),)),
    )
    assert any(item.code == "data.layout_count_mismatch" for item in result.issues)
    cross_graph = Dataset()
    cross_graph.graph(EX.observations).add((EX.record, EX.value, Literal("same")))
    cross_graph.graph(EX.metadata).add(
        (EX.record, EX.value, Literal("same", datatype=XSD.string))
    )
    both = DatasetLayout(
        placements=(
            ContentPlacement(
                "graph occurrences", GraphSelector(), predicates=(EX.value,), maximum=1
            ),
        )
    )
    assert (
        check_dataset_layout(prepare_rdf(cross_graph), both).outcome == "disagreement"
    )


def test_required_empty_absence_open_inventory_and_forbidden_graphs() -> None:
    """Empty graph names are evidence; absence needs a complete graph inventory."""
    source = prepare_rdf(_dataset())
    assert check_dataset_layout(source, _layout()).verified
    absent = DatasetLayout(
        requirements=(GraphRequirement("absent", GraphSelector((EX.absent,))),)
    )
    assert check_dataset_layout(source, absent).outcome == "disagreement"
    forbidden = DatasetLayout(
        requirements=(GraphRequirement("forbidden", META, minimum=0, maximum=0),)
    )
    assert check_dataset_layout(source, forbidden).outcome == "disagreement"
    partial = PreparedSource(
        source.dataset(),
        is_dataset=True,
        document_iri=None,
        effective_url=None,
        literal_fidelity="source",
        graph_fidelity="nonempty-only",
    )
    assert check_dataset_layout(partial, absent).outcome == "unverified"
    assert not check_dataset_layout(partial, _layout()).verified
    assert check_dataset_layout(
        partial, DatasetLayout(requirements=(GraphRequirement("known", META),))
    ).verified
    dataset = _dataset()
    dataset.graph(EX.unselected).add((EX.other, EX.legal, EX.value))
    prepared = prepare_rdf(dataset)
    assert check_dataset_layout(prepared, replace(_layout(), closed=False)).verified
    assert any(
        item.code == "data.layout_graph_forbidden"
        for item in check_dataset_layout(prepared, _layout()).issues
    )


def test_graph_patterns_term_classes_and_graph_metadata_ordinary_roles() -> None:
    """Literal IRI boundary matching and graph-name metadata stay caller-authored."""
    dataset = Dataset()
    dataset.graph(EX["observations/one"]).add((EX.record, EX.value, Literal(1)))
    dataset.default_graph.add((EX["observations/one"], EX.owner, EX.organization))
    pattern = GraphSelector(
        kind="iri", iri_prefix=str(EX["observations/"]), iri_suffix="one"
    )
    layout = DatasetLayout(
        allowed=(GraphSelector(kind="default"), pattern),
        requirements=(GraphRequirement("series", pattern, minimum=1, maximum=1),),
        placements=(
            ContentPlacement(
                "metadata",
                GraphSelector(kind="default"),
                predicates=(EX.owner,),
                minimum=1,
            ),
            ContentPlacement("readings", pattern, predicates=(EX.value,), minimum=1),
        ),
        closed=True,
    )
    assert check_dataset_layout(prepare_rdf(dataset), layout).verified
    changed = replace(
        layout,
        requirements=(
            GraphRequirement(
                "other-suffix",
                GraphSelector(
                    kind="iri", iri_prefix=str(EX["observations/"]), iri_suffix="two"
                ),
            ),
        ),
    )
    assert check_dataset_layout(prepare_rdf(dataset), changed).outcome == "disagreement"


def test_exact_owned_blank_graph_and_subject_scope() -> None:
    """Same-labelled foreign blanks cannot select owned graph or subject scope."""
    dataset = Dataset()
    dataset.graph(BNode("partition")).add((BNode("record"), EX.value, Literal("kept")))
    source = prepare_rdf(dataset)
    exported = source.dataset()
    graph = next(
        item for item in exported.graphs() if isinstance(item.identifier, BNode)
    )
    assert isinstance(graph.identifier, BNode)
    subject = next(graph.subjects())
    assert isinstance(subject, BNode)
    exact = GraphSelector((graph.identifier,))
    layout = DatasetLayout(
        allowed=(GraphSelector(kind="default"), exact),
        placements=(ContentPlacement("owned", exact, subjects=(subject,), minimum=1),),
        closed=True,
    )
    assert check_dataset_layout(source, layout).verified
    foreign = GraphSelector((BNode(str(graph.identifier)),))
    result = check_dataset_layout(
        source, replace(layout, allowed=(foreign, GraphSelector(kind="default")))
    )
    assert result.outcome == "unverified" and not any(
        item.kind == "finding" for item in result.issues
    )
    wrong_subject = replace(
        layout,
        placements=(
            ContentPlacement(
                "foreign", exact, subjects=(BNode(str(subject)),), minimum=1
            ),
        ),
    )
    assert check_dataset_layout(source, wrong_subject).outcome == "unverified"
    many = Dataset()
    for index in range(256):
        many.graph(BNode(str(index)))
    prepared = prepare_rdf(many)
    names = tuple(prepared._graph_names())
    for name in names:
        prepared._read(name)
    ownership = DatasetLayout(allowed=(GraphSelector(names),))
    assert check_dataset_layout(
        prepared, ownership, limits=Limits(max_work=100000)
    ).verified
    assert check_dataset_layout(
        source,
        DatasetLayout(
            requirements=(GraphRequirement("blank class", GraphSelector(kind="blank")),)
        ),
    ).verified


def test_native_trig_fidelity_pins_and_unsupported_input_scope() -> None:
    """Actual parser inventory qualifies empties; a pin never grants full scope."""
    source = prepare_rdf(
        RdfSource(
            "<urn:layout:empty> {} <urn:layout:metadata> { <urn:s> <urn:p> <urn:o> }",
            format="trig",
        )
    )
    assert source.graph_fidelity == "complete"
    assert check_dataset_layout(
        source,
        DatasetLayout(
            requirements=(GraphRequirement("empty", GraphSelector((EX.empty,))),)
        ),
    ).verified
    pinned = prepare_rdf(
        RdfSource(
            "<urn:layout:metadata> { <urn:s> <urn:p> <urn:o> }",
            format="trig",
            graph_name=str(EX.metadata),
        )
    )
    assert check_dataset_layout(pinned, DatasetLayout()).outcome == "unverified"
    union = prepare_rdf(
        RdfSource(
            "<urn:layout:metadata> { <urn:s> <urn:p> <urn:o> }",
            format="trig",
            union=True,
        )
    )
    assert check_dataset_layout(union, DatasetLayout()).outcome == "unverified"
    assert (
        check_dataset_layout(
            prepare_rdf(Dataset().default_graph), DatasetLayout()
        ).outcome
        == "unverified"
    )


def test_finite_metadata_limits_and_definite_findings_survive_unknowns() -> None:
    """Bounds stop owned work before formatting and retain earlier raw failures."""
    source = prepare_rdf(_dataset())
    huge = replace(
        _layout(), requirements=(replace(_layout().requirements[0], name="x" * 100000),)
    )
    report = check_dataset_layout(source, huge, limits=Limits(max_bytes=1000))
    assert report.outcome == "exhausted" and all(
        not item.location for item in report.issues
    )
    metadata = PreparedSource(
        Dataset(),
        is_dataset=True,
        document_iri=None,
        effective_url=None,
        literal_fidelity="😀" * 1000,
    )
    encoded = check_dataset_layout(
        metadata, DatasetLayout(closed=True), limits=Limits(max_bytes=5000)
    )
    assert encoded.outcome == "exhausted"
    assert all(len(item.evidence_json or "") <= 5000 for item in encoded.issues)
    giant_number = replace(
        _layout(), requirements=(replace(_layout().requirements[0], minimum=10**2000),)
    )
    assert (
        check_dataset_layout(
            source, giant_number, limits=Limits(max_bytes=1000)
        ).outcome
        == "exhausted"
    )
    contract = DatasetLayout(
        requirements=(
            GraphRequirement("known-forbidden", META, minimum=0, maximum=0),
            GraphRequirement("next", META, minimum=0, maximum=0),
        )
    )
    quota = check_dataset_layout(source, contract, limits=Limits(max_issues=1))
    assert quota.outcome == "exhausted" and any(
        item.kind == "finding" for item in quota.issues
    )
    partial = PreparedSource(
        source.dataset(),
        is_dataset=True,
        document_iri=None,
        effective_url=None,
        literal_fidelity="source",
        graph_fidelity="nonempty-only",
    )
    combined = check_dataset_layout(
        partial, replace(_layout(), requirements=contract.requirements)
    )
    assert not combined.verified and any(
        item.kind == "finding" for item in combined.issues
    )
    maximum = DatasetLayout(
        placements=(ContentPlacement("no-statements", GraphSelector(), maximum=0),)
    )
    prefix_retained = False
    for work in range(1, 6000, 47):
        result = check_dataset_layout(source, maximum, limits=Limits(max_work=work))
        if result.outcome == "exhausted" and any(
            item.kind == "finding" for item in result.issues
        ):
            prefix_retained = True
            break
    assert prefix_retained
    budget = Budget(Limits(max_work=5))
    assert check_dataset_layout(source, _layout(), budget=budget).outcome == "exhausted"
    with pytest.raises(InputError):
        GraphSelector(names=(URIRef("relative"),))
    with pytest.raises(InputError):
        GraphSelector(kind="iri", names=(EX.metadata,))
    with pytest.raises(InputError):
        ContentPlacement("invalid", META, predicates=(BNode("predicate"),))  # type: ignore[arg-type]
