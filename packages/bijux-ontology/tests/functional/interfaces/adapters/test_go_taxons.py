"""Actual pinned GO-style resources preserve taxon/path/release distinctions."""

import threading
from dataclasses import replace
from hashlib import sha256

from rdflib import URIRef

from bijux_ontology.interfaces.adapters.go.taxon_constraints import (
    GoTaxonAnnotation,
    GoTaxonPolicy,
    GoTaxonRelease,
    GoTaxonResource,
    check_go_taxa,
)
from bijux_ontology.limits import Budget, Limits

P = """@prefix e: <urn:go:test:> .
@prefix obo: <http://purl.obolibrary.org/obo/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
"""
E = "urn:go:test:"
ROOT = URIRef(E + "Life")
POLICY = GoTaxonPolicy((ROOT,), True)


def _resource(role: str, content: str, version: str = "2026-01") -> GoTaxonResource:
    """Pin actual RDF bytes before native parsing, keeping source roles distinct."""
    raw = (P + content).encode()
    return GoTaxonResource(raw, "turtle", E + role, version, sha256(raw).hexdigest())


def _release(
    computed: str = (
        "e:Parent obo:RO_0002161 e:Mammal .\ne:Regulator obo:RO_0002162 e:Life ."
    ),
) -> GoTaxonRelease:
    """Class paths include both valid part_of and deliberately excluded regulates."""
    return GoTaxonRelease(
        _resource(
            "ontology",
            """
e:Parent a owl:Class .
e:Child a owl:Class ; rdfs:subClassOf e:Parent .
e:Part a owl:Class ; rdfs:subClassOf [ a owl:Restriction ;
 owl:onProperty obo:BFO_0000050 ; owl:someValuesFrom e:Child ] .
e:Regulator a owl:Class ; rdfs:subClassOf [ a owl:Restriction ;
 owl:onProperty obo:RO_0002211 ; owl:someValuesFrom e:Parent ] .
""",
        ),
        _resource("computed", computed),
        _resource(
            "groupings",
            """
obo:NCBITaxon_Union_0000002 a owl:Class ;
 owl:equivalentClass [ a owl:Class ; owl:unionOf (e:Bacteria e:Plant) ] .
""",
        ),
        _resource(
            "taxonomy",
            """
e:Life a owl:Class .
e:Mammal a owl:Class ; rdfs:subClassOf e:Life .
e:Human a owl:Class ; rdfs:subClassOf e:Mammal .
e:Bacteria a owl:Class ; rdfs:subClassOf e:Life .
e:Plant a owl:Class ; rdfs:subClassOf e:Life .
e:Arabidopsis a owl:Class ; rdfs:subClassOf e:Plant .
""",
        ),
    )


def _annotation(
    term: str, taxon: str, location: str = "/annotations/0"
) -> GoTaxonAnnotation:
    """Keep actual selected annotation occurrence identifiers and locations."""
    return GoTaxonAnnotation(URIRef(E + term), URIRef(E + taxon), location)


def test_is_a_and_part_of_inherit_but_regulates_does_not() -> None:
    """Known permitted taxa pass; native forbidden descendants remain located."""
    release = _release()
    good = check_go_taxa(
        release,
        (
            _annotation("Part", "Arabidopsis"),
            _annotation("Regulator", "Human", "/annotations/1"),
        ),
        policy=POLICY,
    )
    assert good.report.verified, good.report.to_dict()
    assert all(observation.accepted for observation in good.observations)
    assert len(good.observations[1].evaluations) == 1
    assert good.observations[0].evaluations[0].inheritance.steps
    bad = check_go_taxa(
        release,
        (_annotation("Child", "Human"), _annotation("Part", "Human", "/annotations/1")),
        policy=POLICY,
    )
    assert bad.report.outcome == "disagreement"
    assert {
        issue.location
        for issue in bad.report.issues
        if issue.code == "go.taxon_disallowed"
    } == {"/annotations/0", "/annotations/1"}
    assert bad.resources[0].sha256 == release.ontology.sha256


def test_union_alternatives_intersect_independent_restrictions_and_deduplicate() -> (
    None
):
    """A second real condition narrows an OR group; computed copies are evidence."""
    union = "obo:NCBITaxon_Union_0000002"
    release = _release(
        f"e:Parent obo:RO_0002162 {union} .\ne:Child obo:RO_0002162 {union} ."
    )
    first = check_go_taxa(release, (_annotation("Child", "Bacteria"),), policy=POLICY)
    assert first.report.verified, first.report.to_dict()
    assert len(first.observations[0].evaluations) == 1
    assert first.observations[0].evaluations[0].additional_evidence
    assert first.observations[0].evaluations[0].group_structure
    narrowed = replace(
        release,
        computed=_resource(
            "computed",
            f"e:Parent obo:RO_0002162 {union} . e:Child obo:RO_0002162 e:Plant .",
            "2026-02",
        ),
    )
    bad = check_go_taxa(narrowed, (_annotation("Child", "Bacteria"),), policy=POLICY)
    good = check_go_taxa(
        narrowed, (_annotation("Child", "Arabidopsis"),), policy=POLICY
    )
    assert bad.report.outcome == "disagreement" and good.report.verified
    assert len(bad.observations[0].evaluations) == 2
    assert bad.resources != first.resources


def test_missing_ancestry_groupings_constraints_and_bad_pins_remain_unknown() -> None:
    """Empty extraction/go-basic and incomplete taxonomy never manufacture truth."""
    release = _release()
    assert release.computed is not None
    for changed in (
        replace(release, computed=None),
        replace(release, computed=_resource("computed", "e:unrelated a owl:Class .")),
        replace(release, taxonomy=None),
        replace(
            release,
            taxonomy=_resource(
                "taxonomy",
                "e:Life a owl:Class . e:Human a owl:Class ; "
                "rdfs:subClassOf e:Mammal . e:Mammal a owl:Class .",
            ),
        ),
        replace(release, computed=replace(release.computed, sha256="0" * 64)),
    ):
        result = check_go_taxa(changed, (_annotation("Child", "Human"),), policy=POLICY)
        assert not result.report.verified and any(
            issue.kind == "unverified" for issue in result.report.issues
        )
    assert not check_go_taxa(
        release,
        (_annotation("Child", "Human"),),
        policy=replace(POLICY, complete_ancestry=False),
    ).report.verified
    missing = _release("e:Parent obo:RO_0002162 obo:NCBITaxon_Union_9999999 .")
    assert not check_go_taxa(
        missing, (_annotation("Child", "Human"),), policy=POLICY
    ).report.verified
    assert not check_go_taxa(release, (), policy=POLICY).report.verified

    union = _release("e:Parent obo:RO_0002162 obo:NCBITaxon_Union_0000002 .")
    assert union.groupings is not None
    for extra in (
        "obo:NCBITaxon_Union_0000002 owl:equivalentClass e:Plant .",
        "obo:NCBITaxon_Union_0000002 owl:equivalentClass "
        "[ owl:intersectionOf (e:Plant e:Mammal) ] .",
        "obo:NCBITaxon_Union_0000002 owl:equivalentClass "
        "[ owl:unionOf (e:Bacteria e:Plant) ; "
        "owl:intersectionOf (e:Plant e:Mammal) ] .",
    ):
        content = extra
        if "owl:unionOf" not in extra:
            content = union.groupings.content.decode() + extra
        changed = replace(union, groupings=_resource("groupings", content))
        result = check_go_taxa(
            changed, (_annotation("Child", "Bacteria"),), policy=POLICY
        )
        assert not result.report.verified and result.report.outcome == "unverified"


def test_authored_existentials_keep_quantifiers_and_explicit_import_scope() -> None:
    """Native allValuesFrom cannot replace someValuesFrom or a local declaration."""
    release = _release("e:Regulator obo:RO_0002162 e:Life .")
    authored = (
        release.ontology.content.decode()
        + "\ne:Parent rdfs:subClassOf [ a owl:Restriction ; "
        "owl:onProperty obo:RO_0002162 ; owl:someValuesFrom e:Plant ] ."
    )
    raw = authored.encode()
    source = replace(release.ontology, content=raw, sha256=sha256(raw).hexdigest())
    good = check_go_taxa(
        replace(release, ontology=source),
        (_annotation("Part", "Arabidopsis"),),
        policy=POLICY,
    )
    assert good.report.verified, good.report.to_dict()
    assert any(
        item.constraint.role == "ontology" for item in good.observations[0].evaluations
    )
    universal = raw.replace(b"someValuesFrom e:Plant", b"allValuesFrom e:Plant")
    result = check_go_taxa(
        replace(
            release,
            ontology=replace(
                source, content=universal, sha256=sha256(universal).hexdigest()
            ),
        ),
        (_annotation("Part", "Arabidopsis"),),
        policy=POLICY,
    )
    assert not result.report.verified
    importing = (
        release.ontology.content
        + b"\ne:ontology a owl:Ontology ; owl:imports e:auxiliary ."
    )
    imports = replace(
        _release(),
        ontology=replace(
            release.ontology, content=importing, sha256=sha256(importing).hexdigest()
        ),
    )
    assert not check_go_taxa(
        imports, (_annotation("Child", "Arabidopsis"),), policy=POLICY
    ).report.verified
    supplied = replace(
        imports, imports=(_resource("auxiliary", "e:Other a owl:Class ."),)
    )
    assert check_go_taxa(
        supplied, (_annotation("Child", "Arabidopsis"),), policy=POLICY
    ).report.verified


def test_cumulative_budget_cuts_keep_paid_negative_evidence_and_cancel() -> None:
    """Native selected checks retain their paid prefix on later bounded failure."""
    release = _release()
    annotation = (_annotation("Part", "Human"),)
    baseline = Budget()
    completed = check_go_taxa(release, annotation, policy=POLICY, budget=baseline)
    assert completed.report.outcome == "disagreement"
    paid_late = 0
    for offset in (1, 32, 64, 128, 256, 512, 1024):
        budget = Budget(Limits(max_work=max(1, baseline._counts["work"] - offset)))
        result = check_go_taxa(release, annotation, policy=POLICY, budget=budget)
        paid = {
            id(issue)
            for issue in budget._retained_issues.values()
            if issue.kind == "finding"
        }
        assert paid <= {id(issue) for issue in result.report.issues}
        assert budget.remaining("work") >= 0 and not result.report.verified
        paid_late += bool(paid) and any(
            issue.kind == "exhausted" for issue in result.report.issues
        )
    assert paid_late
    event = threading.Event()
    event.set()
    stopped = check_go_taxa(release, annotation, policy=POLICY, cancel=event)
    assert not stopped.report.verified and any(
        issue.kind == "exhausted" for issue in stopped.report.issues
    )
