"""Structural model checks preserve RDF's open-world and branch semantics."""

import pytest
from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.collection import Collection
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.declarations.types import FieldDeclaration as Field
from bijux_ontology.declarations.types import ModelDeclaration as Model
from bijux_ontology.declarations.types import ValueDeclaration as Value
from bijux_ontology.models.contracts.checking import check_models
from bijux_ontology.vocabulary.sources.loading import load_vocabulary

EX = Namespace("https://example.org/vocabulary#")


def vocabulary() -> Graph:
    """Provide independently declared classes and property constraints."""
    graph = Graph()
    for kind in (EX.Parent, EX.Child, EX.SpecialChild, EX.Other, EX.Unrelated):
        graph.add((kind, RDF.type, OWL.Class))
    graph.add((EX.SpecialChild, RDFS.subClassOf, EX.Child))
    graph.add((EX.Other, OWL.disjointWith, EX.Child))
    graph.add((EX.child, RDF.type, OWL.ObjectProperty))
    graph.add((EX.child, RDFS.domain, EX.Parent))
    graph.add((EX.child, RDFS.range, EX.Child))
    graph.add((EX.other, RDF.type, OWL.ObjectProperty))
    graph.add((EX.other, RDFS.range, EX.Other))
    graph.add((EX.name, RDF.type, OWL.DatatypeProperty))
    graph.add((EX.name, RDFS.range, XSD.string))
    return graph


def test_asserted_subclass_succeeds_but_unrelated_type_remains_unverified() -> None:
    graph = vocabulary()
    field = Field(
        "child", str(EX.child), (Value("object", types=(str(EX.SpecialChild),)),)
    )
    assert check_models([Model("parent", (str(EX.Parent),), (field,))], graph).verified
    unrelated = Field(
        "child", str(EX.child), (Value("object", types=(str(EX.Unrelated),)),)
    )
    report = check_models([Model("parent", (str(EX.Parent),), (unrelated,))], graph)
    assert report.exit_code() == 0
    assert not report.verified
    assert {issue.kind for issue in report.issues} == {"unverified"}
    assert report.exit_code(strict=True) == 1


def test_union_branches_and_reused_recursive_child_are_checked_independently() -> None:
    graph = vocabulary()
    child = Model(
        "child",
        (str(EX.SpecialChild),),
        (
            Field(
                "again", str(EX.other), (Value("object", model="child"),), optional=True
            ),
        ),
    )
    parent = Model(
        "parent",
        (str(EX.Parent),),
        (
            Field("good", str(EX.child), (Value("object", model="child"),)),
            Field(
                "bad", str(EX.other), (Value("object", model="child"),), optional=True
            ),
            Field(
                "either",
                str(EX.child),
                (
                    Value("object", types=(str(EX.SpecialChild),)),
                    Value("object", types=(str(EX.Other),)),
                ),
            ),
        ),
    )
    report = check_models([parent, child], graph)
    conflicts = {
        issue.location
        for issue in report.issues
        if issue.code == "models.disjoint_conflict"
    }
    assert conflicts == {"parent.bad[0]", "parent.either[1]", "child.again[0]"}
    assert report.exit_code() == 1


def test_conjunctive_ranges_and_inherited_disjoint_groups_expose_conflicts() -> None:
    graph = vocabulary()
    group = BNode()
    head = BNode()
    graph.add((group, RDF.type, OWL.AllDisjointClasses))
    graph.add((group, OWL.members, head))
    Collection(graph, head, [EX.Child, EX.Unrelated])
    graph.add((EX.child, RDFS.range, EX.Unrelated))
    field = Field(
        "child", str(EX.child), (Value("object", types=(str(EX.SpecialChild),)),)
    )
    report = check_models([Model("parent", (str(EX.Parent),), (field,))], graph)
    assert any(issue.code == "models.disjoint_conflict" for issue in report.issues)
    assert any(issue.expected == str(EX.Unrelated) for issue in report.issues)


def test_reverse_property_checks_swapped_domain_and_range() -> None:
    graph = vocabulary()
    field = Field(
        "parent",
        str(EX.child),
        (Value("object", types=(str(EX.Parent),)),),
        reverse=True,
    )
    assert check_models([Model("child", (str(EX.Child),), (field,))], graph).verified
    literal = Field(
        "parent",
        str(EX.child),
        (Value("literal", datatype=str(XSD.string)),),
        reverse=True,
    )
    report = check_models([Model("child", (str(EX.Child),), (literal,))], graph)
    assert any(
        issue.code == "models.reverse_literal_conflict" for issue in report.issues
    )


def test_literal_datatypes_and_object_property_representation_are_distinct() -> None:
    graph = vocabulary()
    parent = Model(
        "parent",
        (str(EX.Parent),),
        (
            Field("name", str(EX.name), (Value("literal", datatype=str(XSD.token)),)),
            Field("language", str(EX.name), (Value("literal", language="en"),)),
            Field("reference", str(EX.name), (Value("iri"),)),
        ),
    )
    report = check_models([parent], graph)
    assert {(issue.code, issue.location) for issue in report.issues} == {
        ("models.datatype_conflict", "parent.language[0]"),
        ("models.value_kind_conflict", "parent.reference[0]"),
        ("models.relationship_unverified", "parent.reference[0]"),
    }


def test_anonymous_classes_unknown_serializers_and_lists_are_never_verified() -> None:
    graph = vocabulary()
    graph.add((EX.Parent, RDFS.subClassOf, BNode()))
    broken_group = BNode()
    broken_list = BNode()
    graph.add((broken_group, RDF.type, OWL.AllDisjointClasses))
    graph.add((broken_group, OWL.members, broken_list))
    graph.add((broken_list, RDF.first, EX.Child))
    graph.add((broken_list, RDF.rest, broken_list))
    parent = Model(
        "parent",
        (str(EX.Parent),),
        (
            Field("custom", str(EX.child), (Value("unknown"),)),
            Field(
                "ordered",
                str(EX.child),
                (Value("object", types=(str(EX.Child),), container="@list"),),
            ),
        ),
    )
    report = check_models([parent], graph)
    assert not report.verified
    assert {issue.code for issue in report.issues} >= {
        "models.expression_unverified",
        "models.value_unverified",
        "models.relationship_unverified",
        "models.disjoint_group_unverified",
    }


def test_conflicting_model_ids_and_missing_references_are_input_errors() -> None:
    graph = vocabulary()
    parent = Model(
        "parent",
        (str(EX.Parent),),
        (Field("child", str(EX.child), (Value("object", model="absent"),)),),
    )
    report = check_models([parent], graph)
    assert report.exit_code() == 2
    assert report.issues[-1].code == "input.model_reference_missing"
    assert (
        check_models([parent, Model("parent", (str(EX.Other),))], graph).exit_code()
        == 2
    )


def test_named_expression_and_datatype_restriction_remain_unverified() -> None:
    graph = vocabulary()
    graph.add((EX.Parent, OWL.complementOf, EX.Other))
    graph.add((EX.Restricted, RDF.type, RDFS.Datatype))
    graph.add((EX.Restricted, OWL.onDatatype, XSD.integer))
    graph.add((EX.Restricted, OWL.withRestrictions, BNode()))
    graph.set((EX.name, RDFS.range, EX.Restricted))
    parent = Model(
        "parent",
        (str(EX.Parent),),
        (
            Field(
                "number", str(EX.name), (Value("literal", datatype=str(EX.Restricted)),)
            ),
        ),
    )
    report = check_models([parent], graph)
    assert {issue.code for issue in report.issues} == {
        "models.expression_unverified",
        "models.datatype_unverified",
    }
    assert not report.verified


def test_external_lookup_imports_do_not_become_model_required_imports() -> None:
    csvw = Namespace("http://www.w3.org/ns/csvw#")
    local = Graph().add((EX.Table, RDF.type, OWL.Class))
    local.add((EX.Table, RDFS.subClassOf, csvw.Table))
    lookup = Graph().add((csvw.Table, RDF.type, OWL.Class))
    lookup.add((csvw.Column, RDF.type, OWL.Class))
    lookup.add((csvw.column, RDF.type, OWL.ObjectProperty))
    lookup.add((csvw.column, RDFS.domain, csvw.Table))
    lookup.add((csvw.column, RDFS.range, csvw.Column))
    lookup.add(
        (
            URIRef("http://www.w3.org/ns/csvw"),
            OWL.imports,
            URIRef("http://www.w3.org/ns/prov"),
        )
    )
    sources = load_vocabulary(local, lookup=[lookup])
    model = Model(
        "table",
        (str(EX.Table),),
        (
            Field(
                "columns",
                str(csvw.column),
                (Value("object", types=(str(csvw.Column),)),),
            ),
        ),
    )
    assert check_models([model], sources).verified
    assert len(sources.imports) == 0
    assert (
        URIRef("http://www.w3.org/ns/csvw"),
        OWL.imports,
        URIRef("http://www.w3.org/ns/prov"),
    ) in lookup


@pytest.mark.parametrize(
    ("predicate", "branch"),
    [
        ("@type", Value("object", model="absent")),
        (str(EX.child), Value("object", model="absent", container="@list")),
        (str(EX.child), Value("unknown", model="absent")),
    ],
)
def test_referenced_models_are_checked_before_unsupported_shortcuts(
    predicate: str, branch: Value
) -> None:
    graph = vocabulary()
    model = Model("parent", (str(EX.Parent),), (Field("value", predicate, (branch,)),))
    report = check_models((model,), graph)
    assert report.exit_code() == 2
    assert any(
        issue.code == "input.model_reference_missing"
        and issue.location == "parent.value[0]"
        for issue in report.issues
    )
    assert any(issue.kind == "unverified" for issue in report.issues)


@pytest.mark.parametrize(
    "relation",
    ["direct", "super", "equivalent", "inverse", "double_inverse", "reversed_inverse"],
)
def test_named_property_relations_preserve_endpoint_conflicts(relation: str) -> None:
    graph = vocabulary()
    graph.add((EX.alias, RDF.type, OWL.ObjectProperty))
    graph.add((EX.middle, RDF.type, OWL.ObjectProperty))
    reverse = relation == "reversed_inverse"
    predicate = EX.alias
    if relation == "direct":
        predicate = EX.child
    elif relation == "super":
        graph.add((EX.alias, RDFS.subPropertyOf, EX.child))
        graph.add((EX.child, RDFS.subPropertyOf, EX.alias))
    elif relation == "equivalent":
        graph.add((EX.child, OWL.equivalentProperty, EX.alias))
    elif relation == "inverse":
        graph.add((EX.alias, OWL.inverseOf, EX.middle))
        graph.add((EX.middle, RDFS.domain, EX.Child))
        graph.add((EX.middle, RDFS.range, EX.Parent))
    elif relation == "double_inverse":
        graph.add((EX.alias, OWL.inverseOf, EX.middle))
        graph.add((EX.child, OWL.inverseOf, EX.middle))
    else:
        graph.add((EX.alias, OWL.inverseOf, EX.child))
    field = Field(
        "value",
        str(predicate),
        (Value("object", types=(str(EX.Other),)),),
        reverse=reverse,
    )
    report = check_models((Model("parent", (str(EX.Parent),), (field,)),), graph)
    assert any(
        issue.code == "models.disjoint_conflict" and issue.location == "parent.value[0]"
        for issue in report.issues
    )
    supported = Field(
        "value",
        str(predicate),
        (Value("object", types=(str(EX.SpecialChild),)),),
        reverse=reverse,
    )
    assert check_models(
        (Model("parent", (str(EX.Parent),), (supported,)),), graph
    ).verified


@pytest.mark.parametrize("construct", [OWL.datatypeComplementOf, OWL.disjointUnionOf])
def test_relevant_named_constructs_cannot_pass_by_identity(construct: URIRef) -> None:
    graph = vocabulary()
    graph.add((EX.Child, construct, EX.Other))
    parent = Model(
        "parent",
        (str(EX.Parent),),
        (Field("child", str(EX.child), (Value("object", types=(str(EX.Child),)),)),),
    )
    report = check_models((parent,), graph)
    assert any(issue.code == "models.expression_unverified" for issue in report.issues)


def test_malformed_disjoint_groups_do_not_contaminate_unrelated_comparisons() -> None:
    graph = vocabulary()
    group, head = BNode(), BNode()
    graph.add((group, RDF.type, OWL.AllDisjointClasses))
    graph.add((group, OWL.members, head))
    graph.add((head, RDF.first, EX.Unrelated))
    graph.add((head, RDF.rest, head))
    assert check_models((Model("parent", (str(EX.Parent),)),), graph).verified
    report = check_models((Model("unrelated", (str(EX.Unrelated),)),), graph)
    assert any(
        issue.code == "models.disjoint_group_unverified" for issue in report.issues
    )


def test_class_inheriting_nothing_cannot_describe_an_instance() -> None:
    graph = vocabulary()
    graph.add((EX.Parent, RDFS.subClassOf, OWL.Nothing))
    report = check_models((Model("parent", (str(EX.Parent),)),), graph)
    assert any(
        issue.code == "models.disjoint_conflict" and issue.actual == str(OWL.Nothing)
        for issue in report.issues
    )


def test_literal_datatype_roles_are_checked_without_range_constraints() -> None:
    graph = vocabulary()
    graph.remove((EX.name, RDFS.range, None))
    field = Field("name", str(EX.name), (Value("literal", datatype=str(EX.Parent)),))
    report = check_models((Model("parent", (str(EX.Parent),), (field,)),), graph)
    assert any(
        issue.code == "vocabulary.entity_kind_mismatch" for issue in report.issues
    )
    assert any(issue.kind == "unverified" for issue in report.issues)


def test_literal_samples_validate_actual_lexical_forms_without_proving_all_values() -> (
    None
):
    graph = vocabulary()
    graph.remove((EX.name, RDFS.range, None))
    sample = Literal("1e3", datatype=XSD.decimal, normalize=False)
    branch = Value("literal", datatype=str(XSD.decimal), samples=(sample,))
    field = Field("name", str(EX.name), (branch,))
    report = check_models((Model("parent", (str(EX.Parent),), (field,)),), graph)
    assert any(
        issue.code == "literal.lexical_invalid"
        and issue.location.endswith("samples[0]")
        for issue in report.issues
    )
    assert str(sample) == "1e3"


def test_owl_annotation_domains_are_not_logical_instance_constraints() -> None:
    graph = vocabulary()
    graph.add((EX.note, RDF.type, OWL.AnnotationProperty))
    graph.add((EX.note, RDFS.domain, EX.Child))
    graph.add((EX.Parent, OWL.disjointWith, EX.Child))
    field = Field("note", str(EX.note), (Value("literal", datatype=str(XSD.string)),))
    model = Model("parent", (str(EX.Parent),), (field,))
    assert any(
        issue.code == "models.disjoint_conflict"
        for issue in check_models((model,), graph).issues
    )
    assert check_models((model,), graph, regime="owl-dl").verified


def test_conflicting_registry_identity_has_no_arbitrary_model_winner() -> None:
    graph = vocabulary()
    first = Model("duplicate", (str(EX.Parent),))
    second = Model("duplicate", (str(EX.Other),))
    before = check_models((first, second), graph)
    after = check_models((second, first), graph)
    assert before.to_dict() == after.to_dict()
    assert before.checked == 0 and before.exit_code() == 2


def test_owl_dl_class_membership_does_not_implicitly_promote_datatypes() -> None:
    model = Model("typed", (str(XSD.integer),))
    assert check_models((model,), vocabulary(), regime="rdf").verified
    report = check_models((model,), vocabulary(), regime="owl-dl")
    assert any(
        issue.code == "vocabulary.entity_kind_mismatch" for issue in report.issues
    )


def test_broad_disjoint_groups_only_compare_reachable_members_under_work_limits() -> (
    None
):
    from bijux_ontology.execution.limits import Limits

    graph = vocabulary()
    members = tuple(EX[f"Disjoint{index}"] for index in range(1000))
    for member in members:
        graph.add((member, RDF.type, OWL.Class))
    group, head = BNode(), BNode()
    graph.add((group, RDF.type, OWL.AllDisjointClasses))
    graph.add((group, OWL.members, head))
    Collection(graph, head, list(members))
    limits = Limits(max_work=5000, max_states=100)
    unrelated = check_models(
        (Model("parent", (str(EX.Parent),)),), graph, limits=limits
    )
    assert unrelated.verified, unrelated.to_dict()
    conflict = check_models(
        (Model("conflict", (str(members[0]), str(members[-1]))),),
        graph,
        limits=limits,
    )
    assert conflict.complete and conflict.exit_code() == 1
    assert [(issue.code, issue.actual) for issue in conflict.issues] == [
        (
            "models.disjoint_conflict",
            ", ".join(sorted(map(str, (members[0], members[-1])))),
        )
    ]
    repeated, repeated_head = BNode(), BNode()
    graph.add((repeated, RDF.type, OWL.AllDisjointClasses))
    graph.add((repeated, OWL.members, repeated_head))
    Collection(graph, repeated_head, [EX.Parent, EX.Parent])
    self_disjoint = check_models(
        (Model("parent", (str(EX.Parent),)),), graph, limits=limits
    )
    assert [(issue.code, issue.actual) for issue in self_disjoint.issues] == [
        ("models.disjoint_conflict", str(EX.Parent))
    ]


def test_repeated_deep_class_closure_is_owned_cached_and_work_bounded() -> None:
    from bijux_ontology.execution.limits import Limits

    graph = Graph()
    classes = tuple(EX[f"Ancestor{index}"] for index in range(500))
    for index, cls in enumerate(classes):
        graph.add((cls, RDF.type, OWL.Class))
        if index:
            graph.add((cls, RDFS.subClassOf, classes[index - 1]))
    graph.add((classes[0], OWL.equivalentClass, classes[-1]))
    models = tuple(Model(f"record{index}", (str(classes[-1]),)) for index in range(20))
    report = check_models(models, graph, limits=Limits(max_work=5000, max_states=1000))
    assert report.verified, report.to_dict()
    graph.add((classes[0], RDFS.subClassOf, OWL.Nothing))
    changed = check_models(models[:1], graph)
    assert not changed.verified
    assert any(issue.actual == str(OWL.Nothing) for issue in changed.issues)
