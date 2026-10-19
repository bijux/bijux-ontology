"""Finite selectors keep per-parent obligations and explicit coverage units."""

from copy import deepcopy
from typing import cast

import pytest

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.jsonld.contracts.expectations import ContextExpectation
from bijux_ontology.jsonld.selectors.checking import resolve_selection
from bijux_ontology.jsonld.selectors.types import (
    ArrayItems,
    EntitySelector,
    KeyedItems,
    Property,
    SourceSelector,
)
from bijux_ontology.jsonld.usages.checking import prepare_usages
from bijux_ontology.models.emission.checking import check_emission
from bijux_ontology.models.emission.types import ModelUsage

EX = "https://example.org/"


def test_each_record_keeps_required_children_and_exact_failed_value() -> None:
    document: dict[str, object] = {
        "@context": {"@vocab": EX},
        "records": [{"title": "First"}, {"title": 2}, {"unrelated": "Last"}],
    }
    before = deepcopy(document)
    prepared = prepare_usages(document)
    selector = SourceSelector(
        (Property("records", minimum=1), ArrayItems(), Property("title", minimum=1))
    )
    selected = prepared.select(selector)
    assert selected.coverage_unit == "source_occurrence"
    assert selected.paths == (("records", 0, "title"), ("records", 1, "title"))
    assert [(issue.code, issue.location) for issue in selected.report.issues] == [
        ("selector.parent_count_mismatch", "/records/2")
    ]
    checked = prepared.check(
        [
            ContextExpectation(
                path, EX + "title", datatype="http://www.w3.org/2001/XMLSchema#string"
            )
            for path in selected.paths
        ]
    )
    assert [(issue.code, issue.location) for issue in checked.issues] == [
        ("context.datatype_mismatch", "/records/1/title")
    ]
    assert document == before


def test_optional_zero_is_not_applicable_and_required_zero_fails() -> None:
    prepared = prepare_usages({"@context": {"@vocab": EX}})
    optional = prepared.select(SourceSelector((Property("absent"),), minimum=0))
    assert optional.count == 0
    assert optional.report.outcome == "not_applicable"
    assert optional.report.complete
    required = prepared.select(SourceSelector((Property("absent"),)))
    assert required.report.outcome == "disagreement"
    assert [issue.code for issue in required.report.issues] == [
        "selector.count_mismatch"
    ]
    missing_child = prepared.select(
        SourceSelector((Property("absent", minimum=1),), minimum=0)
    )
    assert missing_child.report.outcome == "disagreement"
    assert missing_child.report.issues[0].location == "/"


def test_declared_keyed_containers_and_per_parent_maximum_are_finite() -> None:
    prepared = prepare_usages(
        {
            "@context": {"@vocab": EX},
            "groups": [
                {"members": {"a": {"title": "A"}, "b": {"title": "B"}}},
                {"members": {"a": {"title": "C"}}},
            ],
        }
    )
    selected = prepared.select(
        SourceSelector(
            (
                Property("groups"),
                ArrayItems(),
                Property("members"),
                KeyedItems(("a", "b"), maximum=1),
                Property("title", minimum=1),
            ),
            minimum=3,
        )
    )
    assert selected.count == 3
    assert [(issue.code, issue.location) for issue in selected.report.issues] == [
        ("selector.parent_count_mismatch", "/groups/0/members")
    ]
    maximum = prepared.select(
        SourceSelector((Property("groups"), ArrayItems()), maximum=1)
    )
    assert "selector.count_mismatch" in {issue.code for issue in maximum.report.issues}


def test_effective_identity_selection_ignores_graph_order_and_compact_spelling() -> (
    None
):
    nodes: list[dict[str, object]] = [
        {"@id": "ex:b", "@type": "ex:Record", "label": "B"},
        {"@id": "ex:a", "@type": "ex:Record", "label": "A"},
    ]
    selector = EntitySelector((EX + "a",), (EX + "Record",), graph=None)
    for reversed_order in (False, True):
        document = {
            "@context": {"ex": EX, "label": EX + "label"},
            "@graph": list(reversed(nodes)) if reversed_order else nodes,
        }
        result = prepare_usages(document).select(selector)
        assert result.report.verified
        assert result.coverage_unit == "rdf_entity"
        assert len(result.entities) == 1 and result.entities[0].identifier == EX + "a"
        assert not result.paths
        source = prepare_usages(document).select(
            EntitySelector(
                (EX + "a",),
                (EX + "Record",),
                graph=None,
                coverage_unit="source_occurrence",
            )
        )
        assert source.paths == (("@graph", 0 if reversed_order else 1),)
        assert source.report.verified
    nodes[1]["@id"] = EX + "a"
    nodes[1]["@type"] = EX + "Record"
    assert (
        prepare_usages({"@context": {"ex": EX}, "@graph": nodes})
        .select(selector)
        .entities[0]
        .identifier
        == EX + "a"
    )


def test_excluded_graph_and_unrelated_nested_record_cannot_supply_selected_facts() -> (
    None
):
    document = {
        "@context": {"@vocab": EX},
        "@graph": [
            {
                "@id": EX + "selected",
                "@type": "Record",
                "other": {"@id": EX + "unrelated", "@type": "Record", "label": "Other"},
            },
            {
                "@id": EX + "elsewhere",
                "@graph": [
                    {"@id": EX + "selected", "@type": "Record", "label": "Excluded"}
                ],
            },
        ],
    }
    prepared = prepare_usages(document)
    selected = prepared.select(
        EntitySelector(
            (EX + "selected",),
            graph=None,
            coverage_unit="source_occurrence",
            steps=(Property("label", minimum=1),),
            minimum=0,
        )
    )
    assert selected.paths == ()
    assert [(issue.code, issue.location) for issue in selected.report.issues] == [
        ("selector.parent_count_mismatch", "/@graph/0")
    ]
    named = prepared.select(EntitySelector((EX + "selected",), graph=EX + "elsewhere"))
    assert named.report.verified and named.entities[0].graph == EX + "elsewhere"


def test_entity_join_and_each_source_object_contract_have_different_coverage() -> None:
    document = {
        "@context": {"@vocab": EX},
        "@graph": [
            {"@id": EX + "record", "@type": "Record"},
            {"@id": EX + "record", "label": "Name"},
        ],
    }
    prepared = prepare_usages(document)
    entity = prepared.select(EntitySelector(types=(EX + "Record",), graph=None))
    assert entity.count == 1 and entity.report.verified
    model = ModelDeclaration(
        "record",
        (EX + "Record",),
        (
            FieldDeclaration(
                "label",
                EX + "label",
                (ValueDeclaration("literal"),),
                emission="guaranteed",
            ),
        ),
    )
    assert check_emission(
        document,
        [model],
        [
            ModelUsage("record", reference=key.identifier, graph=key.graph)
            for key in entity.entities
        ],
    ).verified
    objects = prepared.select(
        EntitySelector(
            types=(EX + "Record",), graph=None, coverage_unit="source_occurrence"
        )
    )
    assert objects.paths == (("@graph", 0), ("@graph", 1))
    source = check_emission(
        document, [model], [ModelUsage("record", path) for path in objects.paths]
    )
    assert ("emission.required_occurrence_missing", "/@graph/0/label") in {
        (issue.code, issue.location) for issue in source.issues
    }
    titles = prepared.select(
        EntitySelector(
            types=(EX + "Record",),
            graph=None,
            coverage_unit="source_occurrence",
            steps=(Property("label", minimum=1),),
        )
    )
    assert titles.paths == (("@graph", 1, "label"),)
    assert titles.report.issues[0].location == "/@graph/0"
    assert "source_occurrence" in titles.report.families
    assert "rdf_entity" in entity.report.families


def test_asserted_type_selection_does_not_apply_entailment_or_ignore_exact_policy() -> (
    None
):
    document = {
        "@context": {"@vocab": EX},
        "@graph": [
            {"@id": EX + "a", "@type": ["Record", "Other"]},
            {"@id": EX + "b", "@type": "SubRecord"},
            {
                "@id": EX + "SubRecord",
                "http://www.w3.org/2000/01/rdf-schema#subClassOf": {
                    "@id": EX + "Record"
                },
            },
        ],
    }
    prepared = prepare_usages(document)
    assert (
        prepared.select(EntitySelector(types=(EX + "Record",), graph=None)).count == 1
    )
    exact = prepared.select(
        EntitySelector(types=(EX + "Record",), exact_types=True, graph=None, minimum=0)
    )
    assert exact.count == 0 and exact.report.outcome == "not_applicable"


def test_reference_objects_contexts_and_opaque_json_are_not_source_descriptions() -> (
    None
):
    document = {
        "@context": {
            "@vocab": EX,
            "id": "@id",
            "kind": "@type",
            "payload": {"@id": EX + "payload", "@type": "@json"},
            "description": {"@id": EX + "description", "@type": "@id"},
            "fake": {"@id": EX + "fake", "@context": {"@vocab": EX}},
        },
        "@graph": [
            {
                "id": EX + "a",
                "kind": "Record",
                "payload": [{"id": EX + "a", "kind": "Record", "label": "opaque"}],
            },
            {"id": EX + "parent", "description": EX + "a"},
        ],
    }
    prepared = prepare_usages(document)
    result = prepared.select(
        EntitySelector((EX + "a",), graph=None, coverage_unit="source_occurrence")
    )
    assert result.paths == (("@graph", 0),) and result.report.verified
    literal_reference = {
        "@context": {"@vocab": EX},
        "@graph": [
            {"@id": EX + "a", "@type": "Record"},
            {"@id": EX + "other", "link": {"@id": EX + "a"}},
        ],
    }
    prepared = prepare_usages(literal_reference)
    default = prepared.select(
        EntitySelector((EX + "a",), graph=None, coverage_unit="source_occurrence")
    )
    assert default.paths == (("@graph", 0),)
    include = prepared.select(
        EntitySelector(
            (EX + "a",),
            graph=None,
            coverage_unit="source_occurrence",
            include_references=True,
        )
    )
    assert include.paths == (("@graph", 0), ("@graph", 1, "link"))


def test_exhaustion_and_ambiguous_bindings_never_claim_complete_coverage() -> None:
    document = {
        "@context": {"@vocab": EX},
        "items": [
            {"@type": "Record", "label": "one"},
            {"@type": "Record", "label": "two"},
        ],
    }
    with pytest.raises(LimitExceeded):
        prepare_usages(document, limits=Limits(max_work=100))
    selector = SourceSelector((Property("items"), ArrayItems(), Property("label")))
    measurement = Budget()
    measured = prepare_usages(document, budget=measurement)
    measured.select(selector)
    limits = Limits(
        max_work=measurement.limits.max_work - measurement.remaining("work")
    )
    prepared = prepare_usages(document, limits=limits)
    result = prepared.select(selector)
    # Selection and processor derivatives share the finite operation budget.
    for _ in range(100):
        if not result.report.complete:
            break
        result = prepared.select(selector)
    assert not result.report.complete and result.report.issues[-1].kind == "exhausted"
    duplicate = prepare_usages(document)
    ambiguous = duplicate.select(
        EntitySelector(
            types=(EX + "Record",), graph=None, coverage_unit="source_occurrence"
        )
    )
    assert not ambiguous.report.complete
    assert "selector.binding_unverified" in {
        issue.code for issue in ambiguous.report.issues
    }


def test_selector_variants_reject_unbounded_query_text_and_invalid_bounds() -> None:
    for construct in (
        lambda: Property("x", minimum=-1),
        lambda: ArrayItems(minimum=2, maximum=1),
        lambda: EntitySelector(("relative",), graph=None),
        lambda: SourceSelector(cast(tuple[Property, ...], ("eval",))),
        lambda: EntitySelector(graph=None, steps=(Property("x"),)),
    ):
        with pytest.raises(InputError):
            construct()


def test_pointer_metadata_is_prepaid_and_escapes_source_keys() -> None:
    key = EX + "a~/🧬"
    document = {key: ["text"]}
    prepared = prepare_usages(document)
    bounded = Budget(Limits(max_total_bytes=4096))
    stopped = resolve_selection(
        SourceSelector((Property("🧬" * 400000),)),
        document,
        prepared.entities,
        prepared.observe,
        bounded,
    )
    assert stopped.report.outcome == "exhausted" and not stopped.report.complete
    assert bounded._counts.get("bytes", 0) <= 4096
    selected = prepared.select(SourceSelector((Property(key), ArrayItems())))
    assert selected.report.verified and selected.paths == ((key, 0),)
    checked = prepared.check(
        [
            ContextExpectation(
                selected.paths[0],
                key,
                datatype="http://www.w3.org/2001/XMLSchema#integer",
            )
        ]
    )
    assert checked.issues[0].location == "/https:~1~1example.org~1a~0~1🧬/0"


@pytest.mark.parametrize("keyword", ["@list", "@set"])
def test_keyword_alias_wrappers_do_not_become_source_entities(keyword: str) -> None:
    document = {
        "@context": {"@vocab": EX, "entries": keyword},
        "items": {
            "entries": [{"@id": EX + "child", "@type": "Record", "label": "Name"}]
        },
    }
    selected = prepare_usages(document).select(
        EntitySelector((EX + "child",), graph=None, coverage_unit="source_occurrence")
    )
    assert selected.paths == (("items", "entries", 0),)
    assert selected.report.verified


def test_describing_node_association_is_distinct_from_whole_contribution() -> None:
    document = {
        "@context": {"@vocab": EX},
        "@graph": [
            {"@id": EX + "parent", "child": {"@id": EX + "child", "label": "Child"}},
            {"@id": EX + "other", "label": "Other"},
        ],
    }
    selector = EntitySelector(
        (EX + "parent",), graph=None, coverage_unit="source_occurrence"
    )
    prepared = prepare_usages(document)
    selected = resolve_selection(
        selector,
        document,
        prepared.entities,
        prepared.observe,
        Budget(),
        require_usage=False,
        association_only=True,
    )
    assert selected.paths == (("@graph", 0),) and selected.report.verified
    missing = resolve_selection(
        EntitySelector((EX + "absent",), graph=None, coverage_unit="source_occurrence"),
        document,
        prepared.entities,
        prepared.observe,
        Budget(),
        require_usage=False,
        association_only=True,
    )
    assert not missing.report.verified and not missing.paths
    with pytest.raises(InputError, match="whole entity paths"):
        resolve_selection(
            SourceSelector(()),
            document,
            prepared.entities,
            prepared.observe,
            Budget(),
            require_usage=False,
            association_only=True,
        )


@pytest.mark.parametrize("keyed", (False, True))
def test_selector_match_storage_is_prepaid_and_keeps_only_the_paid_prefix(
    keyed: bool,
) -> None:
    """Source matching must not publish an unbounded path inventory as verified."""
    values: list[int] | dict[str, int] = (
        {str(index): index for index in range(256)} if keyed else list(range(256))
    )
    document = {"@context": {"@vocab": EX}, "items": values}
    prepared = prepare_usages(document)
    selector = SourceSelector(
        (Property("items"), KeyedItems() if keyed else ArrayItems())
    )
    partial = resolve_selection(
        selector,
        document,
        prepared.entities,
        prepared.observe,
        Budget(Limits(max_states=8)),
        require_usage=False,
    )
    assert partial.report.outcome == "exhausted" and not partial.report.complete
    assert 0 < partial.count < len(values)
    for index, path in enumerate(partial.paths):
        assert path == ("items", str(index) if keyed else index)
    storage = Budget(Limits(max_total_bytes=2048))
    stopped = resolve_selection(
        selector,
        document,
        prepared.entities,
        prepared.observe,
        storage,
        require_usage=False,
    )
    assert stopped.report.outcome == "exhausted" and not stopped.report.complete
    assert stopped.count < len(values) and storage.remaining("bytes") >= 0
    recovered = resolve_selection(
        selector,
        document,
        prepared.entities,
        prepared.observe,
        Budget(),
        require_usage=False,
    )
    assert recovered.report.verified and recovered.count == len(values)


def test_entity_inventory_selection_streams_with_qualified_partial_coverage() -> None:
    """Entity matching cannot allocate its whole inventory before using the budget."""
    document = {
        "@context": {"@vocab": EX},
        "@graph": [{"@id": EX + str(index), "@type": "Record"} for index in range(32)],
    }
    prepared = prepare_usages(document)
    selector = EntitySelector(types=(EX + "Record",), graph=None)
    partial = resolve_selection(
        selector,
        document,
        prepared.entities,
        prepared.observe,
        Budget(Limits(max_states=8)),
    )
    assert partial.coverage_unit == "rdf_entity"
    assert partial.report.outcome == "exhausted" and not partial.report.complete
    assert 0 < partial.count < 32 and not partial.paths
    assert all(
        entity.identifier in {EX + str(i) for i in range(32)}
        for entity in partial.entities
    )
    assert prepared.select(selector).count == 32


def test_joint_associations_keep_graph_reference_and_opaque_scope_separate() -> None:
    from bijux_ontology.jsonld.selectors.checking import resolve_entity_associations

    document = {
        "@context": {
            "@vocab": EX,
            "payload": {"@id": EX + "payload", "@type": "@json"},
        },
        "@graph": [
            {"@id": EX + "a", "@type": "Record", "label": "first"},
            {"@id": EX + "a", "label": "second"},
            {
                "@id": EX + "other",
                "link": {"@id": EX + "a"},
                "payload": {"@id": EX + "a", "@type": "Record", "label": "opaque"},
            },
            {"@id": EX + "named", "@graph": [{"@id": EX + "a", "label": "named"}]},
        ],
    }
    prepared = prepare_usages(document)
    selectors = (
        EntitySelector((EX + "a",), graph=None, coverage_unit="source_occurrence"),
        EntitySelector(
            (EX + "a",),
            graph=None,
            coverage_unit="source_occurrence",
            include_references=True,
        ),
        EntitySelector(
            (EX + "a",), graph=EX + "named", coverage_unit="source_occurrence"
        ),
        EntitySelector(
            (EX + "missing",), graph=None, coverage_unit="source_occurrence", minimum=0
        ),
    )
    separate = tuple(
        resolve_selection(
            selector,
            document,
            prepared.entities,
            prepared.observe,
            Budget(),
            require_usage=False,
            association_only=True,
        )
        for selector in selectors
    )
    joint = resolve_entity_associations(
        selectors, document, prepared.entities, prepared.observe, Budget()
    )
    assert [selection.paths for selection in joint] == [
        (("@graph", 0), ("@graph", 1)),
        (("@graph", 0), ("@graph", 1), ("@graph", 2, "link")),
        (("@graph", 3, "@graph", 0),),
        (),
    ]
    assert [selection.report.to_dict() for selection in joint] == [
        selection.report.to_dict() for selection in separate
    ]


def test_joint_association_limit_retains_completed_paths_without_absence_claims() -> (
    None
):
    import sys
    from types import FrameType

    from bijux_ontology.jsonld.selectors.checking import (
        _SourceMatches,
        resolve_entity_associations,
    )

    document = {
        "@context": {"@vocab": EX},
        "@graph": [
            {"@id": EX + "a", "label": "first"},
            {"@id": EX + "b", "label": "second"},
        ],
    }
    prepared = prepare_usages(document)
    owner = Budget()
    stopped = False

    def stop_after_paid_path(frame: FrameType, event: str, argument: object) -> None:
        nonlocal stopped
        if (
            not stopped
            and event == "return"
            and frame.f_code is _SourceMatches.adopt.__code__
        ):
            request = frame.f_locals["self"]
            if request.paths:
                stopped = True
                owner.consume("work", owner.remaining("work"))

    selectors = tuple(
        EntitySelector((EX + name,), graph=None, coverage_unit="source_occurrence")
        for name in ("a", "b")
    )
    previous = sys.getprofile()
    sys.setprofile(stop_after_paid_path)
    try:
        partial = resolve_entity_associations(
            selectors, document, prepared.entities, prepared.observe, owner
        )
    finally:
        sys.setprofile(previous)
    assert stopped and partial[0].paths == (("@graph", 0),) and not partial[1].paths
    assert all(
        selection.report.outcome == "exhausted" and not selection.report.complete
        for selection in partial
    )
    assert all(
        issue.code == "execution.limit_exceeded"
        for selection in partial
        for issue in selection.report.issues
    )
