"""Application tuple uniqueness preserves exact RDF and record association."""

from collections.abc import Sequence
from typing import Any, cast

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import XSD

from bijux_ontology.data.keys import KeyRecord, check_record_keys
from bijux_ontology.declarations.keys import KeyComponent, KeyContract
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Limits
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.types import RdfSource

EX = Namespace("urn:keys:")


def _records(graph: Graph) -> tuple[KeyRecord, ...]:
    """Share one owned graph while keeping independently selected subjects."""
    source = prepare_rdf(graph)
    return (KeyRecord("a", source, EX.a), KeyRecord("b", source, EX.b))


def test_compound_collisions_change_and_partial_corpus() -> None:
    graph = Graph()
    for subject in (EX.a, EX.b):
        graph.add((subject, EX.accession, Literal("ABC")))
        graph.add((subject, EX.version, Literal("1")))
    contract = KeyContract(
        "record", (KeyComponent(EX.accession), KeyComponent(EX.version))
    )
    records = _records(graph)
    result = check_record_keys(records, contract, complete=True)
    assert result.outcome == "disagreement"
    collision = next(
        issue for issue in result.issues if issue.code == "data.key_collision"
    )
    assert {
        r["identifier"] for r in cast(dict[str, Any], collision.evidence)["records"]
    } == {"a", "b"}
    graph.set((EX.b, EX.version, Literal("2")))
    assert check_record_keys(_records(graph), contract, complete=True).verified
    assert not check_record_keys(_records(graph), contract, complete=False).verified
    assert check_record_keys(records, contract, complete=True).outcome == "disagreement"


def test_exact_values_semantic_equality_and_business_normalization() -> None:
    graph = Graph().add(
        (EX.a, EX.value, Literal("01", datatype=XSD.integer, normalize=False))
    )
    graph.add((EX.b, EX.value, Literal("1.0", datatype=XSD.decimal, normalize=False)))
    records = _records(graph)
    assert check_record_keys(
        records, KeyContract("exact", (KeyComponent(EX.value),)), complete=True
    ).verified
    normalized_source = prepare_rdf(
        RdfSource(
            b"@prefix xsd: <http://www.w3.org/2001/XMLSchema#> . "
            b'<urn:keys:a> <urn:keys:value> "01"^^xsd:integer . '
            b'<urn:keys:b> <urn:keys:value> "1"^^xsd:integer .',
            format="turtle",
        ),
        preserve_literals=False,
    )
    unqualified = check_record_keys(
        (
            KeyRecord("a", normalized_source, EX.a),
            KeyRecord("b", normalized_source, EX.b),
        ),
        KeyContract("exact-source", (KeyComponent(EX.value),)),
        complete=True,
    )
    assert unqualified.outcome == "unverified" and not any(
        i.code == "data.key_collision" for i in unqualified.issues
    )
    semantic = KeyContract(
        "numeric", (KeyComponent(EX.value, equality="owl2-datatype-map"),)
    )
    assert any(
        i.code == "data.key_collision"
        for i in check_record_keys(records, semantic, complete=True).issues
    )
    graph = (
        Graph()
        .add((EX.a, EX.value, Literal("Straße")))
        .add((EX.b, EX.value, Literal("STRASSE")))
    )
    normalized = KeyContract(
        "business", (KeyComponent(EX.value, normalization="casefold"),)
    )
    report = check_record_keys(_records(graph), normalized, complete=True)
    issue = next(i for i in report.issues if i.code == "data.key_collision")
    assert {
        r["values"][0][1] for r in cast(dict[str, Any], issue.evidence)["records"]
    } == {
        "Straße",
        "STRASSE",
    }
    graph.add((EX.b, EX.value, Literal("strasse")))
    assert any(
        i.code == "data.key_multiple"
        for i in check_record_keys(_records(graph), normalized, complete=True).issues
    )
    with pytest.raises(InputError):
        KeyComponent(EX.value, equality="owl2-datatype-map", normalization="nfc")
    # A proved different tuple component defeats unrelated unknown value equality.
    graph = (
        Graph()
        .add((EX.a, EX.p, Literal("v", datatype=EX.Custom)))
        .add((EX.b, EX.p, Literal("v", datatype=EX.Custom)))
    )
    graph.add((EX.a, EX.q, Literal("one"))).add((EX.b, EX.q, Literal("two")))
    contract = KeyContract(
        "partial-equality",
        (KeyComponent(EX.p, equality="owl2-datatype-map"), KeyComponent(EX.q)),
    )
    assert check_record_keys(_records(graph), contract, complete=True).verified


def test_absence_multiplicity_and_source_graph_isolation() -> None:
    graph = Graph().add((EX.a, EX.p, Literal("one")))
    default = KeyContract("required", (KeyComponent(EX.p),))
    assert any(
        i.code == "data.key_absent"
        for i in check_record_keys(_records(graph), default, complete=True).issues
    )
    excluded = KeyContract("optional", default.components, absence="exclude")
    result = check_record_keys(_records(graph), excluded, complete=True)
    assert result.verified and result.exclusions == ("b",)
    empty_domain = check_record_keys((_records(graph)[1],), excluded, complete=True)
    assert empty_domain.outcome == "unverified" and empty_domain.exclusions == ("b",)
    unknown = KeyContract("unknown", default.components, absence="unverified")
    assert (
        check_record_keys(_records(graph), unknown, complete=True).outcome
        == "unverified"
    )
    # Same named subject in independent records cannot borrow its other component.
    a = prepare_rdf(Graph().add((EX.a, EX.p, Literal("one"))))
    b = prepare_rdf(Graph().add((EX.a, EX.q, Literal("two"))))
    pair = (KeyRecord("a", a, EX.a), KeyRecord("b", b, EX.a))
    both = KeyContract("tuple", (KeyComponent(EX.p), KeyComponent(EX.q)))
    assert (
        sum(
            i.code == "data.key_absent"
            for i in check_record_keys(pair, both, complete=True).issues
        )
        == 2
    )
    data = Dataset()
    data.graph(EX.primary).add((EX.a, EX.p, Literal("one")))
    data.graph(EX.support).add((EX.b, EX.p, Literal("one")))
    source = prepare_rdf(RdfSource(data, graph_name=str(EX.primary)))
    assert any(
        i.code == "data.key_absent"
        for i in check_record_keys(
            (KeyRecord("b", source, EX.b),), default, complete=True
        ).issues
    )
    with pytest.raises(InputError):
        check_record_keys(
            (KeyRecord("b", source, EX.b, union=True),), default, complete=True
        )


def test_finite_paths_owned_blanks_and_unknown_alternatives() -> None:
    graph = (
        Graph().add((EX.parent, EX.link, EX.a)).add((EX.parent, EX.code, Literal("A")))
    )
    graph.add((EX.other, EX.link, EX.b)).add((EX.other, EX.code, Literal("A")))
    path = ~EX.link / EX.code
    contract = KeyContract("inverse", (KeyComponent(path),))
    assert (
        check_record_keys(_records(graph), contract, complete=True).outcome
        == "disagreement"
    )
    unsupported = KeyContract("alternative", (KeyComponent(EX.link | EX.code),))
    assert (
        check_record_keys(_records(graph), unsupported, complete=True).outcome
        == "unverified"
    )
    blank = BNode("record")
    source = prepare_rdf(Graph().add((blank, EX.p, Literal("A"))))
    owned = next(source.graph().subjects())
    assert isinstance(owned, BNode)
    required = KeyContract("owned", (KeyComponent(EX.p),))
    assert check_record_keys(
        (KeyRecord("owned", source, owned),), required, complete=True
    ).verified
    assert (
        check_record_keys(
            (KeyRecord("foreign", source, BNode(str(owned))),), required, complete=True
        ).outcome
        == "unverified"
    )
    # Independent anonymous key endpoints never prove a cross-source match.
    a = prepare_rdf(Graph().add((EX.a, EX.p, BNode("x"))))
    b = prepare_rdf(Graph().add((EX.b, EX.p, BNode("x"))))
    assert (
        check_record_keys(
            (KeyRecord("a", a, EX.a), KeyRecord("b", b, EX.b)), required, complete=True
        ).outcome
        == "unverified"
    )


def test_bounded_capture_retains_a_known_collision_before_tail_exhaustion() -> None:
    graph = Graph()
    for subject in (EX.a, EX.b, EX.c):
        graph.add((subject, EX.p, Literal("same")))
    source = prepare_rdf(graph)
    records = tuple(KeyRecord(str(s), source, s) for s in (EX.a, EX.b, EX.c))
    contract = KeyContract("key", (KeyComponent(EX.p),))
    huge = KeyContract("x" * 100000, contract.components)
    stopped = check_record_keys(
        records, huge, complete=True, limits=Limits(max_bytes=1000)
    )
    assert stopped.outcome == "exhausted" and all(
        len(i.location) <= 1000 for i in stopped.issues
    )
    report = check_record_keys(
        records, contract, complete=True, limits=Limits(max_issues=1)
    )
    assert report.outcome == "exhausted" and any(
        i.code == "data.key_collision" for i in report.issues
    )
    with pytest.raises(InputError):
        check_record_keys((records[0], records[0]), contract, complete=True)
    with pytest.raises(InputError):
        check_record_keys(
            (records[0], KeyRecord(URIRef(records[0].identifier), source, EX.b)),
            contract,
            complete=True,
        )
    with pytest.raises(InputError):
        check_record_keys(
            cast(Sequence[KeyRecord], iter(records)), contract, complete=True
        )
    with pytest.raises(InputError):
        check_record_keys(
            (records[0], KeyRecord("alias", source, EX.a, graph_name="@default")),
            contract,
            complete=True,
        )


def test_real_query_projection_matches_native_and_preserves_owned_targets() -> None:
    from bijux_ontology.data.keys import project_record_key
    from bijux_ontology.data.query_rules.checking import run_query_rules
    from bijux_ontology.rdf.queries.types import QueryScope

    targets = [EX.a, EX.b]
    contract = KeyContract("projection", (KeyComponent(~EX.link / EX.code),))
    projection = project_record_key(
        contract, targets, scope=QueryScope("default"), complete=True
    )
    assert projection.report.verified and projection.shapes is not None
    stopped = project_record_key(
        KeyContract("x" * 100000, contract.components),
        targets,
        scope=QueryScope("default"),
        complete=True,
        limits=Limits(max_bytes=1000),
    )
    assert stopped.report.outcome == "exhausted" and all(
        len(i.location) <= 1000 for i in stopped.report.issues
    )
    targets.clear()
    for case in ("different", "collision", "missing", "multiple"):
        graph = Graph().add((EX.left, EX.link, EX.a)).add((EX.right, EX.link, EX.b))
        graph.add((EX.left, EX.code, Literal("one")))
        if case != "missing":
            graph.add(
                (EX.right, EX.code, Literal("one" if case == "collision" else "two"))
            )
        if case == "multiple":
            graph.add((EX.right, EX.code, Literal("three")))
        expected = case == "different"
        assert (
            check_record_keys(_records(graph), contract, complete=True).verified
            is expected
        )
        assert (
            run_query_rules(prepare_rdf(graph), projection.rules).report.verified
            is expected
        )
    unsupported = project_record_key(
        KeyContract("business", (KeyComponent(EX.p, normalization="nfc"),)),
        (EX.a,),
        scope=QueryScope("default"),
        complete=True,
    )
    assert unsupported.report.outcome == "unverified" and not unsupported.rules
    partial = project_record_key(
        KeyContract("partial", (KeyComponent(EX.p),)),
        (EX.a,),
        scope=QueryScope("default"),
        complete=False,
    )
    assert partial.report.outcome == "unverified" and partial.rules


def test_actual_shacl_projection_matches_native() -> None:
    pytest.importorskip("pyshacl")
    from bijux_ontology.data.keys import project_record_key
    from bijux_ontology.rdf.queries.types import QueryScope
    from bijux_ontology.shacl.checking import validate_shapes

    contract = KeyContract("shacl", (KeyComponent(EX.p), KeyComponent(EX.q)))
    projection = project_record_key(
        contract, (EX.a, EX.b), scope=QueryScope("named", EX.G), complete=True
    )
    assert projection.report.verified and projection.shapes is not None
    for case in ("different", "collision", "missing", "multiple"):
        data = Dataset()
        graph = data.graph(EX.G)
        for subject in (EX.a, EX.b):
            graph.add((subject, EX.p, Literal("same")))
            if case != "missing" or subject != EX.b:
                graph.add(
                    (
                        subject,
                        EX.q,
                        Literal("same" if case == "collision" else str(subject)),
                    )
                )
        if case == "multiple":
            graph.add((EX.b, EX.q, Literal("extra")))
        # A matching key in another named graph cannot poison the selected pair.
        data.graph(EX.Other).add((EX.a, EX.q, Literal(str(EX.b))))
        source = prepare_rdf(RdfSource(data, graph_name=str(EX.G)))
        assert check_record_keys(
            (KeyRecord("a", source, EX.a), KeyRecord("b", source, EX.b)),
            contract,
            complete=True,
        ).verified is (case == "different")
        result = validate_shapes(source, projection.shapes, trusted_sparql=True)
        assert result.report.verified is (case == "different")


def test_rdf_string_identity_counts_and_intermediate_path_joins() -> None:
    import importlib.util

    from bijux_ontology.data.keys import project_record_key
    from bijux_ontology.data.query_rules.checking import run_query_rules
    from bijux_ontology.rdf.queries.types import QueryScope
    from bijux_ontology.shacl.checking import validate_shapes

    collision = (
        Graph()
        .add((EX.a, EX.p, Literal("same")))
        .add((EX.b, EX.p, Literal("same", datatype=XSD.string)))
    )
    singleton = (
        Graph()
        .add((EX.a, EX.p, Literal("same")))
        .add((EX.a, EX.p, Literal("same", datatype=XSD.string)))
        .add((EX.b, EX.p, Literal("other")))
    )
    joined = (
        Graph()
        .add((EX.a, EX.p, Literal("alpha")))
        .add((EX.left, EX.q, Literal("alpha", datatype=XSD.string)))
        .add((EX.left, EX.code, Literal("one")))
    )
    joined.add((EX.b, EX.p, Literal("beta"))).add(
        (EX.right, EX.q, Literal("beta", datatype=XSD.string))
    ).add((EX.right, EX.code, Literal("two")))
    for graph, path, expected in (
        (collision, EX.p, False),
        (singleton, EX.p, True),
        (joined, EX.p / ~EX.q / EX.code, True),
    ):
        contract = KeyContract("rdf-identity", (KeyComponent(path),))
        projection = project_record_key(
            contract, (EX.a, EX.b), scope=QueryScope("default"), complete=True
        )
        assert projection.report.verified and projection.shapes is not None
        assert (
            check_record_keys(_records(graph), contract, complete=True).verified
            is expected
        )
        assert (
            run_query_rules(prepare_rdf(graph), projection.rules).report.verified
            is expected
        )
        if importlib.util.find_spec("pyshacl") is not None:
            assert (
                validate_shapes(
                    graph, projection.shapes, trusted_sparql=True
                ).report.verified
                is expected
            )


def test_lossy_literal_intermediates_cannot_establish_resource_keys() -> None:
    """Endpoint IRIs cannot conceal a normalized literal used in an inverse join."""
    payload = b"""@prefix e:<urn:keys:> .
        @prefix xsd:<http://www.w3.org/2001/XMLSchema#> .
        e:a e:p "  alpha  "^^xsd:token .
        e:left e:q "alpha"^^xsd:token; e:code e:one .
        e:b e:p "beta"^^xsd:token .
        e:right e:q "beta"^^xsd:token; e:code e:two ."""
    contract = KeyContract("joined", (KeyComponent(EX.p / ~EX.q / EX.code),))
    for preserve in (True, False):
        source = prepare_rdf(
            RdfSource(payload, format="turtle"), preserve_literals=preserve
        )
        records = (KeyRecord("a", source, EX.a), KeyRecord("b", source, EX.b))
        result = check_record_keys(records, contract, complete=True)
        assert not result.verified
        if preserve:
            assert any(issue.code == "data.key_absent" for issue in result.issues)
        else:
            assert result.outcome == "unverified"
            assert not any(
                issue.code in ("data.key_absent", "data.key_collision")
                for issue in result.issues
            )


def test_compound_discriminators_bound_mixed_key_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exact tenant excludes unrelated numeric and unknown value comparisons."""
    from bijux_ontology.data.keys.evidence import compare_keys

    graph = Graph()
    subjects = tuple(EX[f"record-{index}"] for index in range(256))
    for index, subject in enumerate(subjects):
        graph.add((subject, EX.tenant, Literal(str(index))))
        graph.add((subject, EX.value, Literal("1", datatype=XSD.integer)))
    prepared = prepare_rdf(graph)
    records = tuple(KeyRecord(str(s), prepared, s) for s in subjects)
    contract = KeyContract(
        "tenant-value",
        (KeyComponent(EX.value, equality="sparql-1.1"), KeyComponent(EX.tenant)),
    )
    calls = 0

    def compared(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return compare_keys(*args, **kwargs)

    monkeypatch.setattr("bijux_ontology.data.keys.checking.compare_keys", compared)
    assert check_record_keys(records, contract, complete=True).verified
    assert calls == 0  # Previously all 32,640 numeric pairs were compared.
    graph.set((subjects[-1], EX.tenant, Literal("0")))
    prepared = prepare_rdf(graph)
    collision = check_record_keys(
        tuple(KeyRecord(str(s), prepared, s) for s in subjects), contract, complete=True
    )
    assert calls == 1
    assert sum(i.code == "data.key_collision" for i in collision.issues) == 1
    graph.set((subjects[-1], EX.value, Literal("opaque", datatype=EX.Custom)))
    prepared = prepare_rdf(graph)
    unknown = check_record_keys(
        tuple(KeyRecord(str(s), prepared, s) for s in subjects), contract, complete=True
    )
    assert calls == 2
    assert any(i.code == "data.key_comparison_unverified" for i in unknown.issues)
    graph.set((subjects[-1], EX.tenant, Literal("unrelated")))
    prepared = prepare_rdf(graph)
    assert check_record_keys(
        tuple(KeyRecord(str(s), prepared, s) for s in subjects), contract, complete=True
    ).verified
    assert calls == 2  # Known tenant inequality defeats unsupported value equality.
