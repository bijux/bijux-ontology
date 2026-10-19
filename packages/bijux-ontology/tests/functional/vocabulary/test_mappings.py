"""Actual maintained SSSOM interchange preserves occurrences and explicit policy."""

import importlib.util
import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, SKOS

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.mappings import (
    MappingPolicy,
    MappingSet,
    check_mappings,
    export_sssom,
    read_sssom,
)
from bijux_ontology.vocabulary.sources.view import Vocabulary

E = Namespace("urn:example:")
LICENSE = "https://creativecommons.org/publicdomain/zero/1.0/"
ID = "urn:test:mappings"
HEADER = (
    "# mapping_set_id: urn:test:mappings\n"
    "# license: https://creativecommons.org/publicdomain/zero/1.0/\n"
    '# mapping_set_title: "Owned example"\n'
    '# creator_id: ["ex:reviewer"]\n'
    '# curie_map:\n#   ex: "urn:example:"\n'
    "subject_id\tpredicate_id\tobject_id\tmapping_justification\tconfidence\tcomment\n"
)


def _row(
    subject: str = "A",
    relation: str = "closeMatch",
    target: str = "B",
    comment: str = "first",
) -> str:
    """Use standard CURIE slots and a real SSSOM justification enum member."""
    return (
        f"ex:{subject}\tskos:{relation}\tex:{target}\t"
        f"semapv:ManualMappingCuration\t0.900\t{comment}\n"
    )


def _read(text: str) -> MappingSet:
    """Require the normally installed optional parser for supported positives."""
    if importlib.util.find_spec("sssom") is None:
        pytest.skip("Supported SSSOM interchange requires the sssom extra")
    result = read_sssom(text.encode(), mapping_set_id=ID, license=LICENSE)
    assert result.report.verified, result.report.to_dict()
    assert result.mapping_set is not None
    return result.mapping_set


def _view(*names: str) -> VocabularyView:
    """Provide explicit endpoint type evidence, without mapping-derived roles."""
    graph = Graph()
    for name in names:
        graph.add((E[name], RDF.type, SKOS.Concept))
    return VocabularyView(graph)


def test_real_tsv_roundtrip_preserves_all_lexical_occurrences_and_metadata(
    tmp_path: Path,
) -> None:
    """Pandas/schema/SSSOM writer all execute; confidence never selects truth."""
    body = (
        _row(comment="second\x00retained")
        + _row(comment="first")
        + _row(comment="first")
    )
    owned = _read(HEADER + body)
    assert len(owned.rows) == 3
    assert [dict(row.cells)["comment"] for row in owned.rows] == [
        "second\x00retained",
        "first",
        "first",
    ]
    assert all(dict(row.cells)["confidence"] == "0.900" for row in owned.rows)
    assert all(row.predicate == str(SKOS.closeMatch) for row in owned.rows)
    assert owned.backend_version == "0.4.21" and owned.schema_version == "1.1.0a5"
    metadata = json.loads(owned.metadata_json)
    assert metadata["mapping_set_title"] == "Owned example"
    assert metadata["creator_id"] == ["ex:reviewer"]
    exported = export_sssom(owned)
    assert exported.report.verified and exported.content is not None
    path = tmp_path / "mapping.tsv"
    path.write_bytes(exported.content)
    reread = read_sssom(path, mapping_set_id=ID, license=LICENSE)
    assert reread.report.verified and reread.mapping_set is not None
    assert reread.mapping_set.rows == owned.rows
    assert json.loads(reread.mapping_set.metadata_json) == metadata
    assert str(OWL.sameAs).encode() not in exported.content
    policy = MappingPolicy(
        (str(SKOS.closeMatch),),
        subject_kinds=(str(SKOS.Concept),),
        object_kinds=(str(SKOS.Concept),),
        maximum_targets=1,
    )
    assert check_mappings(
        owned,
        subject_vocabulary=_view("A"),
        object_vocabulary=_view("B"),
        policy=policy,
    ).report.verified


def test_endpoint_scope_types_cardinality_and_relation_conflicts_are_explicit() -> None:
    """Unknown endpoints stay unknown and duplicate occurrences are not targets."""
    owned = _read(
        HEADER
        + _row()
        + _row(relation="broadMatch")
        + _row(target="C")
        + _row(subject="C")
    )
    view = _view("A", "B", "C", "D")
    policy = MappingPolicy(
        (str(SKOS.closeMatch), str(SKOS.broadMatch)),
        maximum_targets=1,
        maximum_sources=1,
        conflicts=((str(SKOS.closeMatch), str(SKOS.broadMatch)),),
    )
    result = check_mappings(
        owned, subject_vocabulary=view, object_vocabulary=view, policy=policy
    )
    assert result.report.outcome == "disagreement"
    assert {i.code for i in result.report} >= {
        "mapping.relation_conflict",
        "mapping.maximum_exceeded",
    }
    assert {
        tuple(
            json.loads(i.evidence_json or "{}")[key]
            for key in ("requirement", "endpoint", "limit", "observed")
        )
        for i in result.report
        if i.code == "mapping.maximum_exceeded"
    } == {
        ("maximum_targets", str(E.A), 1, 2),
        ("maximum_sources", str(E.B), 1, 2),
    }
    unresolved = check_mappings(
        owned,
        subject_vocabulary=_view("A"),
        object_vocabulary=_view("B"),
        policy=policy,
    )
    assert not unresolved.report.complete and any(
        i.code == "mapping.endpoint_unverified" for i in unresolved.report
    )
    forbidden = check_mappings(
        owned,
        subject_vocabulary=view,
        object_vocabulary=view,
        policy=MappingPolicy((str(SKOS.exactMatch),)),
    )
    assert any(i.code == "mapping.relation_forbidden" for i in forbidden.report)
    minimum = check_mappings(
        owned,
        subject_vocabulary=view,
        object_vocabulary=view,
        policy=MappingPolicy(policy.relations, subjects=(str(E.D),), minimum_targets=1),
    )
    assert any(i.code == "mapping.minimum_missing" for i in minimum.report)
    missing = next(i for i in minimum.report if i.code == "mapping.minimum_missing")
    assert json.loads(missing.evidence_json or "{}") == {
        "requirement": "minimum_targets",
        "endpoint": str(E.D),
        "limit": 1,
        "observed": 0,
    }
    punned = Graph().add((E.A, RDF.type, OWL.Class)).add((E.A, RDF.type, SKOS.Concept))
    assert check_mappings(
        _read(HEADER + _row()),
        subject_vocabulary=VocabularyView(punned),
        object_vocabulary=view,
        policy=MappingPolicy(
            (str(SKOS.closeMatch),), subject_kinds=(str(SKOS.Concept), str(OWL.Class))
        ),
    ).report.verified
    lookup = Graph().add((E.B, RDF.type, SKOS.Concept))
    scoped = VocabularyView(Vocabulary(Graph(), lookup=(lookup,)), scopes=("local",))
    assert not check_mappings(
        _read(HEADER + _row()),
        subject_vocabulary=view,
        object_vocabulary=scoped,
        policy=MappingPolicy((str(SKOS.closeMatch),)),
    ).report.verified


def test_direction_and_uninterpreted_predicates_modifiers_remain_distinct() -> None:
    """No broad/narrow reversal, close equivalence or negation coercion occurs."""
    owned = _read(
        HEADER
        + _row(relation="broadMatch")
        + _row(subject="B", relation="narrowMatch", target="A")
    )
    assert [(r.subject, r.predicate, r.object) for r in owned.rows] == [
        (str(E.A), str(SKOS.broadMatch), str(E.B)),
        (str(E.B), str(SKOS.narrowMatch), str(E.A)),
    ]
    view = _view("A", "B")
    for slot, kind, expected in (
        ("object_type", "composed entity expression", "unverified"),
        ("subject_type", "owl class", "disagreement"),
        ("subject_type", "skos concept", "agreement"),
    ):
        typed = _read(
            HEADER.replace("\tcomment\n", "\tcomment\t" + slot + "\n")
            + _row().rstrip("\n")
            + "\t"
            + kind
            + "\n"
        )
        actual = check_mappings(
            typed,
            subject_vocabulary=view,
            object_vocabulary=view,
            policy=MappingPolicy(
                (str(SKOS.closeMatch),), subjects=(str(E.A),), minimum_targets=1
            ),
        ).report
        assert actual.outcome == expected, actual.to_dict()
        if expected == "unverified":
            assert not any(i.code == "mapping.minimum_missing" for i in actual)
    literal = read_sssom(
        (
            HEADER.replace("\tcomment\n", "\tcomment\tsubject_type\n")
            + _row().rstrip("\n")
            + "\trdfs literal\n"
        ).encode(),
        mapping_set_id=ID,
        license=LICENSE,
    )
    assert not literal.report.verified and literal.mapping_set is None
    self_typed = Graph().add((E.A, RDF.type, E.A))
    class_row = _read(
        HEADER.replace("\tcomment\n", "\tcomment\tsubject_type\n")
        + _row().rstrip("\n")
        + "\towl class\n"
    )
    assert check_mappings(
        class_row,
        subject_vocabulary=VocabularyView(self_typed),
        object_vocabulary=view,
        policy=MappingPolicy((str(SKOS.closeMatch),)),
    ).report.verified
    metadata_type = _read(
        HEADER.replace("# curie_map:", "# subject_type: owl class\n# curie_map:")
        + _row()
    )
    assert (
        check_mappings(
            metadata_type,
            subject_vocabulary=view,
            object_vocabulary=view,
            policy=MappingPolicy((str(SKOS.closeMatch),)),
        ).report.outcome
        == "disagreement"
    )
    unmapped = _read((HEADER + _row() + _row()).replace("ex:B", "sssom:NoTermFound", 1))
    declared_sentinel = _view("A", "B")
    sentinel_graph = declared_sentinel._vocabulary.local
    sentinel_graph.add(
        (URIRef("https://w3id.org/sssom/NoTermFound"), RDF.type, SKOS.Concept)
    )
    sentinel_view = VocabularyView(sentinel_graph)
    uncertain = check_mappings(
        unmapped,
        subject_vocabulary=sentinel_view,
        object_vocabulary=sentinel_view,
        policy=MappingPolicy(
            (str(SKOS.closeMatch),),
            subjects=(str(E.A),),
            minimum_targets=2,
        ),
    ).report
    assert uncertain.outcome == "unverified"
    maximum = check_mappings(
        unmapped,
        subject_vocabulary=sentinel_view,
        object_vocabulary=sentinel_view,
        policy=MappingPolicy((str(SKOS.closeMatch),), maximum_targets=1),
    ).report
    assert maximum.outcome == "unverified" and not any(
        i.code == "mapping.maximum_exceeded" for i in maximum
    )
    assert not any(
        i.code in ("mapping.minimum_missing", "mapping.maximum_exceeded")
        for i in uncertain
    )
    modified = (
        HEADER.replace("\tcomment\n", "\tcomment\tpredicate_modifier\n")
        + _row().rstrip("\n")
        + "\tNot\n"
    )
    negated = _read(modified)
    result = check_mappings(
        negated,
        subject_vocabulary=_view("A"),
        object_vocabulary=_view("B"),
        policy=MappingPolicy((str(SKOS.closeMatch),)),
    )
    assert result.report.outcome == "unverified"
    assert any(i.code == "mapping.semantics_unverified" for i in result.report)
    qualifier = "custom:qualifier=uninterpreted"
    for text in (
        HEADER.replace("\tcomment\n", "\tcomment\tother\n")
        + _row().rstrip("\n")
        + "\t"
        + qualifier
        + "\n",
        HEADER.replace("# curie_map:", "# other: " + qualifier + "\n# curie_map:")
        + _row(),
    ):
        qualified = _read(text)
        qualified_report = check_mappings(
            qualified,
            subject_vocabulary=view,
            object_vocabulary=view,
            policy=MappingPolicy(
                (str(SKOS.closeMatch),), subjects=(str(E.A),), minimum_targets=1
            ),
        ).report
        assert qualified_report.outcome == "unverified"
        assert any(i.code == "mapping.semantics_unverified" for i in qualified_report)
        assert not any(i.code == "mapping.minimum_missing" for i in qualified_report)
        assert qualifier in qualified.source.decode()
    other = _read((HEADER + _row()).replace("skos:closeMatch", "owl:equivalentClass"))
    assert other.rows[0].predicate == str(OWL.equivalentClass)
    assert (
        check_mappings(
            other,
            subject_vocabulary=_view("A"),
            object_vocabulary=_view("B"),
            policy=MappingPolicy((str(OWL.equivalentClass),)),
        ).report.outcome
        == "unverified"
    )


def test_lexical_parser_hazards_and_fabricated_defaults_never_pass() -> None:
    """Body hash/undefined prefixes/warnings cannot hide dropped row data."""
    if importlib.util.find_spec("sssom") is None:
        pytest.skip("Actual malformed-schema cases require the maintained backend")
    for scalar in ("false", "0", "[]", "{}", "null"):
        text = HEADER.replace("# curie_map:", "# other: " + scalar + "\n# curie_map:")
        result = read_sssom(
            (text + _row()).encode(), mapping_set_id=ID, license=LICENSE
        )
        assert result.mapping_set is None and result.report.outcome == "unverified"
    retained = _read(
        HEADER.replace("# curie_map:", "# other: 'false'\n# curie_map:") + _row()
    )
    assert json.loads(retained.metadata_json)["other"] == "false"
    for text in (
        "# mapping_set_id: urn:discarded\n" + HEADER + _row(),
        HEADER.replace(
            'ex: "urn:example:"', 'ex: "urn:discarded:"\n#   ex: "urn:example:"'
        )
        + _row(),
        "# defaults: &defaults {license: urn:discarded}\n# <<: *defaults\n"
        + HEADER
        + _row(),
        "# comment: &cycle [*cycle]\n" + HEADER + _row(),
        HEADER + _row(target="B#fragment"),
        HEADER.replace("\tcomment\n", "\tcomment\tcomment\n")
        + _row().rstrip("\n")
        + "\tduplicate\n",
        HEADER + _row(comment='"quoted#value"'),
        (HEADER + _row()).replace("semapv:ManualMappingCuration", ""),
        (HEADER + _row()).replace("ex:A", "unknown:A"),
        HEADER.replace('ex: "urn:example:"', 'skos: "urn:false:"') + _row(),
        (HEADER + _row()).replace(
            "# mapping_set_id: urn:test:mappings", "# mapping_set_id: urn:wrong"
        ),
    ):
        result = read_sssom(text.encode(), mapping_set_id=ID, license=LICENSE)
        assert not result.report.verified and result.mapping_set is None, (
            result.report.to_dict()
        )
    supplied = _read(
        (HEADER + _row())
        .replace("# mapping_set_id: urn:test:mappings\n", "")
        .replace("# license: " + LICENSE + "\n", "")
    )
    assert json.loads(supplied.metadata_json)["mapping_set_id"] == ID
    assert json.loads(supplied.metadata_json)["license"] == LICENSE
    for invalid in ("", BNode("urn:test:mappings"), Literal("urn:test:mappings")):
        result = read_sssom(
            (HEADER + _row()).encode(), mapping_set_id=invalid, license=LICENSE
        )
        assert not result.report.verified and result.mapping_set is None


def test_owned_inputs_limits_known_prefix_and_deadline_recovery() -> None:
    """Exhaustion cannot erase an already paid violation or publish partial bytes."""
    owned = _read(HEADER + _row() + _row(comment="long " * 1000))
    with pytest.raises(InputError):
        replace(owned, source=b"unqualified")
    with pytest.raises(InputError):
        MappingSet()
    with pytest.raises(InputError):
        MappingPolicy(cast(tuple[str, ...], "urn:relation"))
    view = _view("A", "B")
    policy = MappingPolicy((str(SKOS.exactMatch),))
    found = False
    for work in range(300, 12001, 300):
        report = check_mappings(
            owned,
            subject_vocabulary=view,
            object_vocabulary=view,
            policy=policy,
            limits=Limits(max_work=work),
        ).report
        if report.outcome == "exhausted" and any(
            i.code == "mapping.relation_forbidden" for i in report
        ):
            found = True
            break
    assert found
    assert export_sssom(owned, limits=Limits(max_bytes=100)).content is None
    pilot = Budget()
    assert export_sssom(owned, budget=pilot).report.verified
    shared = Budget(Limits(max_work=2 * pilot._counts["work"] - 1))
    assert export_sssom(owned, budget=shared).report.verified
    exhausted = export_sssom(owned, budget=shared)
    assert exhausted.report.outcome == "exhausted" and exhausted.content is None
    assert shared.remaining("work") >= 0
    inherited_cancel = threading.Event()
    parent = Operation(None, None, 30, inherited_cancel)
    inherited_cancel.set()
    assert export_sssom(owned, budget=parent).content is None
    huge = read_sssom(
        (HEADER + _row()).encode(),
        mapping_set_id="urn:" + "x" * 100000,
        license=LICENSE,
        limits=Limits(max_bytes=1000),
    )
    assert huge.mapping_set is None and huge.report.outcome == "exhausted"
    deep = (
        HEADER.replace(
            "# curie_map:",
            "# comment: " + "[" * 20 + "value" + "]" * 20 + "\n# curie_map:",
        )
        + _row()
    )
    depth = read_sssom(
        deep.encode(), mapping_set_id=ID, license=LICENSE, limits=Limits(max_depth=5)
    )
    assert depth.report.outcome == "exhausted" and depth.mapping_set is None
    cancellation = threading.Event()
    cancellation.set()
    stopped = read_sssom(
        (HEADER + _row()).encode(),
        mapping_set_id=ID,
        license=LICENSE,
        cancel=cancellation,
    )
    assert stopped.report.outcome == "exhausted" and stopped.mapping_set is None
    start = time.monotonic()
    deadline = read_sssom(
        (HEADER + _row()).encode(), mapping_set_id=ID, license=LICENSE, timeout=0.02
    )
    assert deadline.report.outcome == "exhausted" and deadline.mapping_set is None
    assert time.monotonic() - start < 2
    assert _read(HEADER + _row()).rows[0].subject == str(E.A)
