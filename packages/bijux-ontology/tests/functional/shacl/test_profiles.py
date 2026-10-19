"""Finite selected profiles retain real constraints, scope and portable behavior."""

import threading
from pathlib import Path

from pytest import MonkeyPatch
from rdflib import BNode, Dataset, Graph, Literal, Namespace
from rdflib.namespace import RDF, RDFS, SH, XSD

from bijux_ontology.documents.export import export_rdf
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.types import RdfSource
from bijux_ontology.shacl.checking import validate_shapes
from bijux_ontology.shacl.profiles import ProfileCase, export_profile, extract_profile

EX = Namespace("urn:profile:")
SHAPES = """@prefix e:<urn:profile:>. @prefix sh:<http://www.w3.org/ns/shacl#>.
@prefix xsd:<http://www.w3.org/2001/XMLSchema#>.
e:Root a sh:NodeShape; sh:targetClass e:Record; sh:property e:Amount,e:Children;
 sh:and (e:Kinds); sh:prefixes e:Prefixes.
e:Amount a sh:PropertyShape; sh:path (e:measurement e:value);
 sh:datatype xsd:integer; sh:minCount 1.
e:Children a sh:PropertyShape; sh:path [sh:inversePath e:owner];
 sh:minCount 1; sh:node e:Child.
e:Child a sh:NodeShape; sh:property [sh:path e:name; sh:minCount 1].
e:Kinds a sh:NodeShape; sh:or ([sh:class e:KindA] [sh:class e:KindB]).
e:Prefixes sh:declare [sh:prefix "e"; sh:namespace "urn:profile:"^^xsd:anyURI].
e:Independent a sh:NodeShape; sh:targetClass e:Record;
 sh:property [sh:path e:unrelated; sh:minCount 1]."""


def _data(*, quantity: bool = True, child: bool = True, kind: bool = True) -> Graph:
    """Use independently authored actual records with separable constraint failures."""
    graph = Graph().add((EX.record, RDF.type, EX.Record))
    if kind:
        graph.add((EX.record, RDF.type, EX.Specific))
    graph.add((EX.record, EX.measurement, EX.measurement1))
    if quantity:
        graph.add((EX.measurement1, EX.value, Literal(2)))
    graph.add((EX.child, EX.owner, EX.record))
    if child:
        graph.add((EX.child, EX.name, Literal("actual")))
    return graph


def _cases() -> tuple[ProfileCase, ...]:
    """Select real positive and differentiated negative consumer outcomes."""
    return (
        ProfileCase("valid", prepare_rdf(_data()), True),
        ProfileCase("missing-quantity", prepare_rdf(_data(quantity=False)), False),
        ProfileCase("missing-child-name", prepare_rdf(_data(child=False)), False),
        ProfileCase("wrong-kind", prepare_rdf(_data(kind=False)), False),
    )


def test_nested_logical_paths_ontology_and_portable_profile(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Actual selected results agree before/after extraction and portable reload."""
    shapes = prepare_rdf(SHAPES, format="turtle")
    ontology = prepare_rdf(Graph().add((EX.Specific, RDFS.subClassOf, EX.KindA)))
    cases = _cases()
    profile = extract_profile(shapes, (str(EX.Root),), ontology=ontology, cases=cases)
    assert profile.source is not None and profile.structural_report.verified
    assert profile.report.verified and len(profile.comparisons) == len(cases)
    graph = profile.source.graph()
    assert (EX.Independent, None, None) not in graph
    assert (EX.Child, SH.property, None) in graph
    assert (EX.Prefixes, SH.declare, None) in graph
    assert {d.parameter for d in profile.dependencies} >= {
        SH.property,
        SH.node,
        SH["and"],
        SH["or"],
    }
    for comparison, case in zip(profile.comparisons, cases, strict=True):
        assert comparison.source is not None and comparison.derived is not None
        assert comparison.source.conforms is case.expected_conforms
        assert comparison.derived.conforms is case.expected_conforms
        assert comparison.report.verified
        if not case.expected_conforms:
            assert comparison.source.report.outcome == "disagreement"
            assert comparison.derived.report.outcome == "disagreement"
    exported = export_profile(profile, format="turtle")
    assert exported.report.verified and exported.shapes and exported.ontology
    assert exported.shapes.content and exported.ontology.content
    shape_path, ontology_path = tmp_path / "shapes.ttl", tmp_path / "ontology.ttl"
    shape_path.write_bytes(exported.shapes.content)
    ontology_path.write_bytes(exported.ontology.content)
    monkeypatch.chdir(tmp_path)
    reloaded = prepare_rdf(shape_path, format="turtle")
    support = prepare_rdf(ontology_path, format="turtle")
    for case in cases:
        actual = validate_shapes(
            case.data,
            reloaded,
            ontology=support,
            use_shapes=(str(EX.Root),),
            preflight=True,
        )
        assert actual.report.complete and actual.conforms is case.expected_conforms

    shared = prepare_rdf(
        """@prefix e:<urn:profile:>. @prefix sh:<http://www.w3.org/ns/shacl#>.
        e:S a sh:NodeShape;sh:targetNode e:r;
          sh:property [sh:path e:p;sh:hasValue _:shared]. e:r e:p _:shared.""",
        format="turtle",
    )
    anonymous = extract_profile(
        shared, (str(EX.S),), cases=(ProfileCase("shared-constant", shared, True),)
    )
    assert anonymous.structural_report.verified and anonymous.source is not None
    assert anonymous.comparisons[0].source is not None
    assert anonymous.comparisons[0].source.conforms is True
    assert anonymous.comparisons[0].derived is not None
    assert anonymous.comparisons[0].derived.conforms is True
    assert not anonymous.report.verified
    assert export_profile(anonymous, format="turtle").shapes is None
    raw_export = export_rdf(anonymous.source, format="turtle")
    assert raw_export.content is not None
    reloaded_anonymous = prepare_rdf(raw_export.content, format="turtle")
    assert (
        validate_shapes(shared, reloaded_anonymous, use_shapes=(str(EX.S),)).conforms
        is False
    )


def test_shared_anonymous_dependencies_and_shape_cycles_are_exact() -> None:
    """A finite cyclic graph preserves owned shared resources and original witnesses."""
    source = prepare_rdf(
        """@prefix sh:<http://www.w3.org/ns/shacl#>.
    @prefix e:<urn:profile:>.
    e:R a sh:NodeShape;sh:property [sh:path e:p;sh:node _:shared],
      [sh:path e:q;sh:node _:shared].
    _:shared a sh:NodeShape;sh:node e:R.
    e:Independent a sh:NodeShape;sh:targetNode e:record.""",
        format="turtle",
    )
    profile = extract_profile(source, (str(EX.R),))
    assert profile.source is not None and profile.structural_report.verified
    assert not profile.report.verified
    original = source.graph()
    shared = next(original.subjects(SH.node, EX.R))
    assert isinstance(shared, BNode)
    derived = profile.source.graph()
    assert sum(1 for _ in derived.subjects(SH.node, shared)) == 2
    assert any(node is shared for triple in derived for node in triple)
    assert (EX.Independent, None, None) not in derived
    assert all(triple in original for d in profile.dependencies for triple in d.via)
    foreign = extract_profile(source, (BNode(str(shared)),))
    assert not foreign.structural_report.verified


def test_missing_dynamic_and_bad_meta_shapes_never_claim_preservation() -> None:
    """Known incomplete closure and actual meta-SHACL failures remain nongreen."""
    for extra in ("sh:node e:Missing", 'sh:sparql [sh:select "SELECT $this WHERE {}"]'):
        source = prepare_rdf(
            f"""@prefix sh:<http://www.w3.org/ns/shacl#>.
        @prefix e:<urn:profile:>. e:R a sh:NodeShape;sh:targetNode e:record;{extra}.""",
            format="turtle",
        )
        profile = extract_profile(
            source,
            (str(EX.R),),
            cases=(ProfileCase("case", prepare_rdf(Graph()), True),),
        )
        assert not profile.structural_report.verified and not profile.report.verified
        assert profile.source is not None
        assert export_profile(profile, format="turtle").shapes is None
    malformed = prepare_rdf(
        """@prefix sh:<http://www.w3.org/ns/shacl#>.
    @prefix e:<urn:profile:>. e:R a sh:NodeShape;sh:targetNode e:record;
      sh:property [sh:path e:p;sh:minCount -1].""",
        format="turtle",
    )
    profile = extract_profile(
        malformed,
        (str(EX.R),),
        cases=(ProfileCase("case", prepare_rdf(Graph()), True),),
    )
    assert not profile.report.verified
    assert profile.comparisons[0].derived is not None
    assert any(
        i.code == "shacl.shapes_invalid"
        for i in profile.comparisons[0].derived.report.issues
    )


def test_scope_qualification_root_coverage_and_unrelated_targets() -> None:
    """Hidden graphs cannot supply missing dependencies or target another root."""
    dataset = Dataset()
    dataset.graph(EX.G).parse(
        data="""@prefix sh:<http://www.w3.org/ns/shacl#>.
    @prefix e:<urn:profile:>.
    e:R a sh:NodeShape;sh:targetNode e:record;sh:node e:Child.""",
        format="turtle",
    )
    dataset.graph(EX.hidden).add((EX.Child, RDF.type, SH.NodeShape))
    selected = prepare_rdf(RdfSource(dataset, graph_name=str(EX.G)))
    missing = extract_profile(selected, (str(EX.R),))
    assert not missing.structural_report.verified
    untargeted = prepare_rdf(
        """@prefix sh:<http://www.w3.org/ns/shacl#>.
    @prefix e:<urn:profile:>.
    e:R a sh:NodeShape;sh:property [sh:path e:p;sh:minCount 1].""",
        format="turtle",
    )
    profile = extract_profile(
        untargeted,
        (str(EX.R),),
        cases=(ProfileCase("unexercised", prepare_rdf(Graph()), True),),
    )
    assert profile.structural_report.verified and not profile.report.verified

    selected_graph = Dataset()
    selected_graph.graph(EX.G).parse(
        data="""@prefix sh:<http://www.w3.org/ns/shacl#>.
        @prefix e:<urn:profile:>. e:R a sh:NodeShape;sh:targetNode e:record;
        sh:property [sh:path e:p;sh:minCount 1].""",
        format="turtle",
    )
    explicit = extract_profile(
        prepare_rdf(selected_graph),
        (str(EX.R),),
        graph_name=EX.G,
        cases=(
            ProfileCase(
                "selected", prepare_rdf(Graph().add((EX.record, EX.p, EX.value))), True
            ),
        ),
    )
    assert explicit.source is not None and explicit.source.graph_name == str(EX.G)
    assert explicit.report.verified


def test_qualified_disjoint_siblings_retain_incoming_parent_context() -> None:
    """A directly selected property still depends on its authored sibling context."""
    shapes = prepare_rdf(
        """@prefix sh:<http://www.w3.org/ns/shacl#>.
    @prefix e:<urn:profile:>. e:Parent a sh:NodeShape;sh:property e:Chosen,e:Sibling.
    e:Chosen a sh:PropertyShape;sh:targetNode e:record;sh:path e:p;
      sh:qualifiedValueShape [sh:class e:A];sh:qualifiedMinCount 1;
      sh:qualifiedValueShapesDisjoint true.
    e:Sibling a sh:PropertyShape;sh:path e:p;sh:qualifiedValueShape [sh:class e:B].
    """,
        format="turtle",
    )
    good = Graph().add((EX.record, EX.p, EX.value)).add((EX.value, RDF.type, EX.A))
    bad = (
        Graph()
        .add((EX.record, EX.p, EX.value))
        .add((EX.value, RDF.type, EX.A))
        .add((EX.value, RDF.type, EX.B))
    )
    for lexical in ("true", "1"):
        graph = shapes.graph()
        graph.set(
            (
                EX.Chosen,
                SH.qualifiedValueShapesDisjoint,
                Literal(lexical, datatype=XSD.boolean, normalize=False),
            )
        )
        profile = extract_profile(
            prepare_rdf(graph),
            (str(EX.Chosen),),
            cases=(
                ProfileCase("exclusive", prepare_rdf(good), True),
                ProfileCase("sibling-excluded", prepare_rdf(bad), False),
            ),
        )
        assert profile.source is not None and profile.report.verified
        assert (EX.Parent, SH.property, EX.Sibling) in profile.source.graph()


def test_bounds_and_cancellation_keep_structural_prefix_truth() -> None:
    """Stopped extraction or behavior cannot produce an accepted profile."""
    source = prepare_rdf(SHAPES, format="turtle")
    for limits in (Limits(max_work=1), Limits(max_states=1), Limits(max_bytes=16)):
        result = extract_profile(source, (str(EX.Root),), limits=limits)
        assert result.report.outcome == "exhausted" and not result.report.verified
    long_name = "urn:profile:" + "x" * 100_000
    pinned_dataset = Dataset()
    for triple in source.graph():
        pinned_dataset.graph(long_name).add(triple)
    pinned = prepare_rdf(RdfSource(pinned_dataset, graph_name=long_name))
    pinned._read()
    assert (
        extract_profile(
            pinned, (str(EX.Root),), limits=Limits(max_bytes=1000)
        ).report.outcome
        == "exhausted"
    )
    spent = Budget(Limits(max_issues=1))
    spent.consume("issues")
    assert (
        extract_profile(source, (str(EX.Root),), budget=spent).report.outcome
        == "exhausted"
    )
    stop = threading.Event()
    stop.set()
    assert (
        extract_profile(source, (str(EX.Root),), cancel=stop).report.outcome
        == "exhausted"
    )
