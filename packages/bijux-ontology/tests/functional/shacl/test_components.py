"""Real reusable SHACL components preserve RDF, parameter roles and local trust."""

import threading
from decimal import Decimal
from pathlib import Path

import pytest
from rdflib import BNode, Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, SH, XSD

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.documents.export import export_rdf
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.literals.checking import check_literal
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.sources.types import RdfSource
from bijux_ontology.shacl.authoring.parameters import ShaclParameter
from bijux_ontology.shacl.checking import validate_shapes
from bijux_ontology.shacl.components.checking import check_components
from bijux_ontology.shacl.components.compiling import compile_components
from bijux_ontology.shacl.components.types import (
    ComponentShapes,
    ComponentUse,
    ComponentValidator,
    ShaclComponent,
)

EX = Namespace("https://example.org/components#")


def _shapes() -> PreparedSource:
    """Own independent node/property roots and opaque shared source assertions."""
    detail = BNode()
    graph = Graph().add((EX.Node, RDF.type, SH.NodeShape))
    graph.add((EX.Node, SH.targetNode, EX.value))
    graph.add((EX.Property, RDF.type, SH.PropertyShape))
    graph.add((EX.Property, SH.targetNode, EX.record))
    graph.add((EX.Property, SH.path, EX.p)).add((EX.Property, SH.minCount, Literal(1)))
    graph.add((EX.Node, EX.opaque, detail)).add((EX.Property, EX.opaque, detail))
    graph.add((detail, EX.annotation, Literal("owned")))
    return prepare_rdf(graph)


def _component(
    *, validators: tuple[ComponentValidator, ...] | None = None
) -> ShaclComponent:
    """Require exact RDF identity rather than approximate value equality."""
    return ShaclComponent(
        str(EX.Equal),
        (ShaclParameter(str(EX.expected)),),
        validators
        or (ComponentValidator(ask="ASK { FILTER(sameTerm($value, $expected)) }"),),
    )


def _uses() -> tuple[ComponentUse, ...]:
    """Use the same parameterized definition on independently targeted roles."""
    return tuple(
        ComponentUse(shape, str(EX.Equal), ((str(EX.expected), EX.value),))
        for shape in (EX.Node, EX.Property)
    )


def _compiled() -> ComponentShapes:
    """Compile one supported reusable ordinary ASK component."""
    return compile_components(_shapes(), (_component(),), _uses())


def test_generic_ask_actual_node_property_reuse_trust_and_portable_reload(
    tmp_path: Path,
) -> None:
    """ASK works with ordinary advanced=False execution and offline RDF reload."""
    original = _shapes()
    before = frozenset(original.graph())
    compiled = compile_components(original, (_component(),), _uses())
    assert compiled.source is not None and compiled.report.verified
    assert before <= frozenset(compiled.source.graph())
    assert frozenset(original.graph()) == before
    assert len(compiled.requirements) == 2
    for value in (EX.value, EX.bad):
        data = prepare_rdf(Graph().add((EX.record, EX.p, value)))
        actual = check_components(data, compiled, trusted_sparql=True)
        assert actual.result is not None and actual.result.conforms is (
            value == EX.value
        )
        assert actual.report.verified is (value == EX.value)
        assert (
            actual.result.constraints is not None and actual.result.constraints.verified
        )
    untrusted = check_components(
        prepare_rdf(Graph().add((EX.record, EX.p, EX.value))), compiled
    )
    assert not untrusted.report.verified
    exported = export_rdf(compiled.source, format="turtle")
    assert exported.report.verified and exported.content is not None
    destination = tmp_path / "components.ttl"
    destination.write_bytes(exported.content)
    reloaded = prepare_rdf(destination, format="turtle")
    for value in (EX.value, EX.bad):
        result = validate_shapes(
            Graph().add((EX.record, EX.p, value)),
            reloaded,
            use_shapes=compiled.roots,
            required_constraints=compiled.requirements,
            trusted_sparql=True,
            preflight=True,
        )
        assert result.conforms is (value == EX.value)


def test_role_select_prefixes_and_parameter_datatypes_are_real_constraints() -> None:
    """Role-specific SELECT and RDF prefix declarations survive portable packaging."""
    component = _component(
        validators=(
            ComponentValidator(
                role="node",
                select="SELECT $this WHERE { FILTER(!sameTerm($value,$expected)) }",
            ),
            ComponentValidator(
                role="property",
                select=(
                    "SELECT $this ?value WHERE { $this $PATH ?value . "
                    "FILTER(!sameTerm(?value,$expected)) }"
                ),
            ),
        )
    )
    compiled = compile_components(_shapes(), (component,), _uses())
    assert compiled.report.verified
    for value in (EX.value, EX.bad):
        result = check_components(
            prepare_rdf(Graph().add((EX.record, EX.p, value))),
            compiled,
            trusted_sparql=True,
        )
        assert result.result is not None and result.result.conforms is (
            value == EX.value
        )
    membership = ShaclComponent(
        str(EX.Member),
        (ShaclParameter(str(EX.klass)),),
        (
            ComponentValidator(
                ask="ASK { $value a $klass . FILTER($klass = c:Allowed) }"
            ),
        ),
        prefixes=(("c", str(EX)),),
    )
    use = ComponentUse(EX.Property, membership.iri, ((str(EX.klass), EX.Allowed),))
    packaged = compile_components(_shapes(), (membership,), (use,))
    assert packaged.report.verified and packaged.source is not None
    exported = export_rdf(packaged.source, format="turtle")
    assert exported.content is not None
    for allowed in (True, False):
        graph = Graph().add((EX.record, EX.p, EX.value))
        if allowed:
            graph.add((EX.value, RDF.type, EX.Allowed))
        reloaded_result = validate_shapes(
            graph,
            prepare_rdf(exported.content, format="turtle"),
            trusted_sparql=True,
            use_shapes=(EX.Property,),
            required_constraints=packaged.requirements,
        )
        assert reloaded_result.conforms is allowed
    conflicting = _shapes().graph()
    conflicting.add((EX.Ontology, RDF.type, OWL.Ontology))
    conflicting.add((EX.Ontology, SH.declare, EX.Declaration))
    conflicting.add((EX.Declaration, SH.prefix, Literal("c")))
    conflicting.add(
        (
            EX.Declaration,
            SH.namespace,
            Literal("https://other.org/", datatype=XSD.anyURI),
        )
    )
    assert (
        compile_components(prepare_rdf(conflicting), (membership,), (use,)).source
        is None
    )
    ordered = ShaclComponent(
        str(EX.Equal),
        (ShaclParameter(str(EX.expected), order=Decimal("1E+3")),),
        _component().validators,
    )
    ordered_shapes = compile_components(_shapes(), (ordered,), _uses())
    assert ordered_shapes.report.verified and ordered_shapes.source is not None
    order = next(ordered_shapes.source.graph().objects(None, SH.order))
    assert isinstance(order, Literal) and str(order) == "1000"
    assert check_literal(order).verified
    for huge in (Decimal("1E+100000000"), Decimal("1E-100000000")):
        large = ShaclComponent(
            str(EX.Equal),
            (ShaclParameter(str(EX.expected), order=huge),),
            _component().validators,
        )
        assert (
            compile_components(_shapes(), (large,), _uses()).report.outcome
            == "exhausted"
        )
    typed = ShaclComponent(
        str(EX.Typed),
        (ShaclParameter(str(EX.code), datatype=str(XSD.string)),),
        (ComponentValidator(ask="ASK { FILTER(isLiteral($code)) }"),),
    )
    invalid = compile_components(
        _shapes(),
        (typed,),
        (ComponentUse(EX.Node, typed.iri, ((str(EX.code), EX.value),)),),
    )
    assert invalid.source is None and not invalid.report.verified


def test_invalid_queries_and_prebound_roles_block_artifact() -> None:
    """No malformed form, retrieval or rebinding becomes an executable artifact."""
    missing = compile_components(
        _shapes(), (_component(),), (ComponentUse(EX.Node, str(EX.Equal), ()),)
    )
    assert missing.source is None and not missing.report.verified
    for query in (
        "SELECT $this WHERE {}",
        "ASK { SERVICE <https://example.org/remote> { ?s ?p ?o } }",
        "ASK FROM <https://example.org/remote> { ?s ?p ?o }",
        (
            "ASK { { SELECT $this WHERE { "
            "SERVICE <https://example.org/remote> {?s ?p ?o} } } }"
        ),
        "ASK { BIND(1 AS ?other) BIND(2 AS ?expected) }",
        "ASK { FILTER(BOUND($shapesGraph)) }",
        "ASK { FILTER(BOUND($PATH)) }",
        "ASK { VALUES ?x { 1 } }",
        "ASK { MINUS { ?s ?p ?o } }",
        "ASK { { SELECT ?this WHERE {} } }",
    ):
        component = _component(validators=(ComponentValidator(ask=query),))
        result = compile_components(_shapes(), (component,), _uses())
        assert result.source is None and not result.report.verified, query
    for role, query in (("generic", "select"), ("property", "ask")):
        with pytest.raises(InputError):
            ComponentValidator(role=role, **{query: "ASK {}"})  # type: ignore[arg-type]
    unsupported = compile_components(
        _shapes(),
        (
            _component(
                validators=(
                    ComponentValidator(
                        ask="ASK { FILTER(<https://example.org/custom>($value)) }"
                    ),
                )
            ),
        ),
        _uses(),
    )
    assert unsupported.source is not None and unsupported.structural_report.verified
    assert not unsupported.report.verified


def test_shape_parameter_ownership_and_selected_scope_are_explicit() -> None:
    """Foreign blanks, conflicting catalogs/bindings and hidden shapes cannot join."""
    for wrong in (BNode(str(EX.Equal)), Literal(str(EX.Equal))):
        with pytest.raises(InputError):
            ShaclComponent(wrong, _component().parameters, _component().validators)
        with pytest.raises(InputError):
            ComponentUse(EX.Node, wrong, ())
        with pytest.raises(InputError):
            ComponentUse(EX.Node, str(EX.Equal), ((wrong, EX.value),))
        with pytest.raises(InputError):
            ShaclComponent(
                str(EX.Equal),
                _component().parameters,
                _component().validators,
                prefixes=(("c", wrong),),
            )
    base = _shapes()
    compiled = _compiled()
    assert compiled.source is not None
    assert compile_components(compiled.source, (_component(),), _uses()).source is None
    conflict = base.graph().add((EX.Node, EX.expected, EX.bad))
    assert (
        compile_components(prepare_rdf(conflict), (_component(),), _uses()).source
        is None
    )
    blank = next(base.graph().objects(EX.Node, EX.opaque))
    assert isinstance(blank, BNode)
    anonymous = compile_components(
        base,
        (_component(),),
        (ComponentUse(EX.Node, str(EX.Equal), ((str(EX.expected), blank),)),),
    )
    assert anonymous.source is not None and anonymous.structural_report.verified
    assert not anonymous.report.verified
    foreign = ComponentUse(
        BNode(str(blank)), str(EX.Equal), ((str(EX.expected), EX.value),)
    )
    assert compile_components(base, (_component(),), (foreign,)).source is None
    from rdflib import Dataset

    dataset = Dataset()
    for triple in base.graph():
        dataset.graph(EX.hidden).add(triple)
    selected = prepare_rdf(RdfSource(dataset, graph_name=str(EX.visible)))
    assert compile_components(selected, (_component(),), _uses()).source is None
    for parameter in (str(EX.this),):
        reserved = ShaclComponent(
            str(EX.Bad),
            (ShaclParameter(parameter),),
            (ComponentValidator(ask="ASK {}"),),
        )
        assert (
            compile_components(
                base,
                (reserved,),
                (ComponentUse(EX.Node, reserved.iri, ((parameter, EX.value),)),),
            ).source
            is None
        )


def test_bounds_cancellation_and_unexercised_roots_stay_nongreen() -> None:
    """Shared quota exhaustion and absent actual targets never imply execution."""
    for limits in (Limits(max_work=1), Limits(max_states=1), Limits(max_bytes=16)):
        stopped = compile_components(_shapes(), (_component(),), _uses(), limits=limits)
        assert stopped.report.outcome == "exhausted"
    spent = Budget(Limits(max_issues=1))
    spent.consume("issues")
    assert (
        compile_components(
            _shapes(),
            (_component(),),
            (ComponentUse(EX.Node, str(EX.Equal), ()),),
            budget=spent,
        ).report.outcome
        == "exhausted"
    )
    stop = threading.Event()
    stop.set()
    assert (
        compile_components(
            _shapes(), (_component(),), _uses(), cancel=stop
        ).report.outcome
        == "exhausted"
    )
    compiled = _compiled()
    assert (
        check_components(
            prepare_rdf(Graph()), compiled, trusted_sparql=True, cancel=stop
        ).report.outcome
        == "exhausted"
    )
    unexercised = _shapes().graph()
    unexercised.remove((None, SH.targetNode, None))
    untargeted = compile_components(prepare_rdf(unexercised), (_component(),), _uses())
    assert untargeted.report.verified
    actual = check_components(prepare_rdf(Graph()), untargeted, trusted_sparql=True)
    assert not actual.report.verified


def test_role_catalogue_retains_default_terms_and_qualifies_unused_roles() -> None:
    """Selected roles execute independently of all declared syntax obligations."""
    lexical = Literal("01", datatype=XSD.integer, normalize=False)
    graph = _shapes().graph().add((EX.Node, EX.code, lexical))
    source = prepare_rdf(graph)
    before = frozenset(source.graph())
    parameter = ShaclParameter(str(EX.code), datatype=str(XSD.integer))
    node = ComponentValidator(role="node", select="SELECT $this WHERE {FILTER(1=2)}")
    generic = ComponentValidator(ask="ASK {FILTER(1=2)}")
    unused = ComponentValidator(
        role="property",
        select="SELECT $this ?value WHERE {$this $PATH ?value . FILTER(1=2)}",
    )
    component = ShaclComponent(str(EX.Roles), (parameter,), (generic, node, unused))
    use = ComponentUse(EX.Node, component.iri, ())
    compiled = compile_components(source, (component,), (use,))
    assert compiled.report.verified and compiled.source is not None
    retained = next(compiled.source.graph().objects(EX.Node, EX.code))
    assert isinstance(retained, Literal) and str(retained) == "01"
    assert retained.datatype == XSD.integer and len(compiled.requirements) == 1
    data = prepare_rdf(Graph().add((EX.value, RDF.type, EX.Class)))
    actual = check_components(data, compiled, trusted_sparql=True)
    assert actual.report.verified and actual.result is not None
    assert actual.result.conforms is True
    generic_only = ShaclComponent(component.iri, (parameter,), (generic,))
    failed = check_components(
        data, compile_components(source, (generic_only,), (use,)), trusted_sparql=True
    )
    assert failed.result is not None and failed.result.conforms is False
    invalid = ShaclComponent(
        component.iri,
        (parameter,),
        (node, ComponentValidator(role="property", select="SELECT ?other WHERE {}")),
    )
    rejected = compile_components(source, (invalid,), (use,))
    assert rejected.structural_report.verified and rejected.source is None
    assert not rejected.report.verified and frozenset(source.graph()) == before


def test_native_false_filter_loss_is_unknown_only_for_selected_execution() -> None:
    """A valid constant filter cannot disappear from a required native query."""
    source = _shapes()
    data = prepare_rdf(Graph().add((EX.record, EX.p, EX.value)))
    for expression in ("false", "0", '""'):
        component = _component(
            validators=(ComponentValidator(ask=f"ASK {{ FILTER({expression}) }}"),)
        )
        compiled = compile_components(source, (component,), _uses())
        assert compiled.structural_report.verified and compiled.source is not None
        assert compiled.report.outcome == "unverified"
        actual = check_components(data, compiled, trusted_sparql=True)
        assert not actual.report.verified
        raw = validate_shapes(
            data,
            compiled.source,
            trusted_sparql=True,
            use_shapes=compiled.roots,
            required_constraints=compiled.requirements,
        )
        assert raw.report.outcome == "unverified" and raw.conforms is None
    generic = ComponentValidator(ask="ASK {FILTER(false)}")
    node = ComponentValidator(role="node", select="SELECT $this WHERE {FILTER(1=2)}")
    prop = ComponentValidator(
        role="property",
        select="SELECT $this ?value WHERE {$this $PATH ?value . FILTER(1=2)}",
    )
    for shape, validators in (
        (
            EX.Node,
            (
                generic,
                node,
                ComponentValidator(
                    role="property",
                    select=(
                        "SELECT $this ?value WHERE {$this $PATH ?value . FILTER(false)}"
                    ),
                ),
            ),
        ),
        (
            EX.Property,
            (
                generic,
                ComponentValidator(
                    role="node", select="SELECT $this WHERE {FILTER(false)}"
                ),
                prop,
            ),
        ),
    ):
        component = _component(validators=validators)
        use = ComponentUse(shape, component.iri, ((str(EX.expected), EX.value),))
        compiled = compile_components(source, (component,), (use,))
        assert compiled.report.verified
        actual = check_components(data, compiled, trusted_sparql=True)
        assert actual.report.verified and actual.result is not None
        assert actual.result.conforms is True
    for body, conforms in (
        ("FILTER(true)", True),
        ("FILTER(1=2)", False),
        ("FILTER(false) FILTER(true)", False),
    ):
        component = _component(validators=(ComponentValidator(ask=f"ASK {{{body}}}"),))
        compiled = compile_components(source, (component,), _uses())
        assert compiled.report.verified
        actual = check_components(data, compiled, trusted_sparql=True)
        assert actual.result is not None and actual.result.conforms is conforms
