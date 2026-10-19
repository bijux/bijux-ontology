"""Exact native public signatures and caller-owned provider wiring."""

from dataclasses import replace

from rdflib import Dataset, Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.declarations.types import TermDeclaration
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.owl.obligations import OntologyObligation
from bijux_ontology.vocabulary.inspection import VocabularyView
from bijux_ontology.vocabulary.loading import load_vocabulary
from bijux_ontology.workflows.ontology.modules import (
    ModuleBinding,
    ModuleContract,
    ModulePropertyRequirement,
    ModuleProvider,
    ModuleRequirement,
    ModuleWiring,
    check_module_wiring,
)

E = "urn:modules:"
CHILD, PARENT, VALUE = (E + name for name in ("Child", "Parent", "value"))
EXPORTS = (
    TermDeclaration(CHILD, (str(OWL.Class),), "/api/class"),
    TermDeclaration(PARENT, (str(OWL.Class),), "/api/parent"),
    TermDeclaration(VALUE, (str(OWL.DatatypeProperty),), "/api/property"),
)
REQUIRED = ModuleRequirement(
    "data",
    EXPORTS,
    (
        OntologyObligation("subsumption", (CHILD,), PARENT, "/api/subsumption"),
        OntologyObligation(
            "datatype_inclusion", (str(XSD.integer),), str(XSD.decimal), "/api/datatype"
        ),
    ),
    (
        ModulePropertyRequirement(
            VALUE, "range", "datatype_inclusion", str(XSD.decimal), "/api/range"
        ),
    ),
)
CONSUMER = ModuleContract("consumer", (), (REQUIRED,))
BINDING = ModuleBinding("data", "provider")


def _graph(*, replacement: bool = False) -> Graph:
    """Keep every public name while changing both selected semantic guarantees."""
    graph = Graph()
    for term in EXPORTS:
        graph.add((URIRef(term.iri), RDF.type, URIRef(term.kinds[0])))
    graph.add(
        (URIRef(CHILD), RDFS.subClassOf, URIRef(E + "Other" if replacement else PARENT))
    )
    graph.add((URIRef(VALUE), RDFS.range, XSD.string if replacement else XSD.integer))
    return graph


def _provider(*, replacement: bool = False, name: str = "provider") -> ModuleProvider:
    """Only this explicit view supplies public declarations and native edges."""
    return ModuleProvider(
        ModuleContract(name, EXPORTS), VocabularyView(_graph(replacement=replacement))
    )


def test_explicit_provider_proves_independent_class_and_datatype_guarantees() -> None:
    provider = _provider()
    result = check_module_wiring(CONSUMER, (provider,), (BINDING,))
    assert result.report.verified
    attempt = result.requirements[0]
    assert attempt.providers == (provider,)
    assert all(answer.status == "established" for answer in attempt.obligations)
    assert attempt.obligations[0].witness == (
        (URIRef(CHILD), RDFS.subClassOf, URIRef(PARENT)),
    )
    assert attempt.property_contracts[0][1].values == (XSD.integer,)
    assert attempt.providers[0].view is provider.view
    private_requirement = ModuleRequirement(
        "private", (TermDeclaration(E + "Missing", (str(OWL.Class),)),)
    )
    private = replace(
        provider, contract=replace(provider.contract, requires=(private_requirement,))
    )
    finite = check_module_wiring(CONSUMER, (private,), (BINDING,))
    assert finite.report.verified
    assert finite.requirements[0].providers[0].contract.requires == (
        private_requirement,
    )
    assert finite.report.regime == "one-level-selected-module-signatures"


def test_replacement_retains_names_without_preserving_required_relationships() -> None:
    changed = check_module_wiring(CONSUMER, (_provider(replacement=True),), (BINDING,))
    assert not changed.report.verified and not changed.report.complete
    answers = changed.requirements[0].obligations
    assert [answer.status for answer in answers] == [
        "unknown",
        "established",
        "contradicted",
    ]
    assert answers[-1].datatype_relation == "disjoint"
    assert any(
        issue.location == "/api/range" and issue.kind == "unverified"
        for issue in changed.report
    )
    assert not any(
        issue.code == "ontology.module_obligation_contradicted"
        for issue in changed.report
    )
    # Effective ranges are conjunctive. One sufficient named included range
    # remains independently provable despite another constraint or expression.
    graph = _graph()
    graph.add((URIRef(VALUE), RDFS.range, XSD.string))
    provider = ModuleProvider(
        ModuleContract("provider", EXPORTS), VocabularyView(graph)
    )
    assert check_module_wiring(CONSUMER, (provider,), (BINDING,)).report.verified


def test_unbound_and_multiple_candidates_never_pick_the_first_provider() -> None:
    providers = (_provider(), _provider(name="other"))
    for bindings in (
        (),
        (BINDING, ModuleBinding("data", "other")),
        (ModuleBinding("data", "missing"),),
    ):
        result = check_module_wiring(CONSUMER, providers, bindings)
        assert result.report.outcome == "unverified"
        assert not result.requirements[0].obligations
    assert (
        len(
            check_module_wiring(
                CONSUMER, providers, (BINDING, ModuleBinding("data", "other"))
            )
            .requirements[0]
            .providers
        )
        == 2
    )
    assert check_module_wiring(CONSUMER, providers, (BINDING,)).report.verified
    empty = check_module_wiring(ModuleContract("consumer", ()), (), ())
    assert empty.report.outcome == "not_applicable"


def test_private_lookup_import_roles_and_excluded_graphs_do_not_supply_exports() -> (
    None
):
    provider = _provider()
    private = replace(provider, contract=ModuleContract("provider", EXPORTS[1:]))
    denied = check_module_wiring(CONSUMER, (private,), (BINDING,))
    assert not denied.report.verified
    assert any(
        issue.code == "ontology.module_export_missing"
        and issue.location == "/api/class"
        for issue in denied.report
    )
    vocabulary = load_vocabulary(Graph(), lookup=(_graph(),))
    for scopes in (("local",), ("local", "lookup")):
        lookup = ModuleProvider(
            ModuleContract("provider", EXPORTS),
            VocabularyView(vocabulary, scopes=scopes),
        )
        assert not check_module_wiring(CONSUMER, (lookup,), (BINDING,)).report.verified
    dataset = Dataset()
    selected = URIRef(E + "selected")
    excluded = URIRef(E + "excluded")
    for triple in _graph():
        dataset.graph(excluded).add(triple)
    dataset.graph(selected).add((URIRef(E + "ontology"), RDF.type, OWL.Ontology))
    vocabulary = load_vocabulary(dataset, graph_name=selected)
    excluded_provider = ModuleProvider(
        ModuleContract("provider", EXPORTS), VocabularyView(vocabulary)
    )
    assert not check_module_wiring(
        CONSUMER, (excluded_provider,), (BINDING,)
    ).report.verified
    root = Graph().add((URIRef(E + "ontology"), OWL.imports, URIRef(E + "import")))
    imports = load_vocabulary(root, imports={E + "import": _graph()})
    imported = ModuleProvider(
        ModuleContract("provider", EXPORTS),
        VocabularyView(imports, scopes=("imports",)),
    )
    assert check_module_wiring(CONSUMER, (imported,), (BINDING,)).report.verified
    local = replace(imported, view=VocabularyView(imports, scopes=("local",)))
    assert not check_module_wiring(CONSUMER, (local,), (BINDING,)).report.verified


def test_absent_or_wrong_role_operands_cannot_pass_native_reflexivity() -> None:
    term = TermDeclaration(CHILD, (str(OWL.Class),), "/class")
    requirement = ModuleRequirement(
        "data",
        (term,),
        (OntologyObligation("subsumption", (CHILD,), CHILD, "/reflexive"),),
    )
    consumer = ModuleContract("consumer", (), (requirement,))
    for graph in (
        Graph(),
        Graph().add((URIRef(CHILD), RDF.type, OWL.DatatypeProperty)),
    ):
        provider = ModuleProvider(
            ModuleContract("provider", (term,)), VocabularyView(graph)
        )
        result = check_module_wiring(consumer, (provider,), (BINDING,))
        assert not result.report.verified and not result.requirements[0].obligations
    # Direct independent questions still preserve positive proof beside unknown
    # whole-ontology consistency, rather than upgrading the latter to agreement.
    unknown = replace(
        REQUIRED, obligations=(*REQUIRED.obligations, OntologyObligation("consistency"))
    )
    result = check_module_wiring(
        replace(CONSUMER, requires=(unknown,)), (_provider(),), (BINDING,)
    )
    assert not result.report.verified
    assert [answer.status for answer in result.requirements[0].obligations[:3]] == [
        "established",
        "established",
        "unknown",
    ]


def test_shared_budgets_preserve_paid_prefix_and_reject_empty_required_interfaces() -> (
    None
):
    provider = replace(_provider(), contract=ModuleContract("provider", EXPORTS[1:]))

    def check(budget: Budget) -> ModuleWiring:
        return check_module_wiring(CONSUMER, (provider,), (BINDING,), budget=budget)

    pilot = Budget()
    check(pilot)
    paid_late = 0
    for kind in ("work", "states", "bytes"):
        used = pilot._counts[kind]
        for offset in (1, 25, 100):
            field = "max_total_bytes" if kind == "bytes" else "max_" + kind
            budget = Budget(replace(Limits(), **{field: max(1, used - offset)}))
            result = check(budget)
            paid = {id(issue) for issue in budget._retained_issues.values()}
            assert paid <= {id(issue) for issue in result.report}
            assert all(
                count
                <= getattr(
                    budget.limits,
                    "max_total_bytes" if name == "bytes" else "max_" + name,
                )
                for name, count in budget._counts.items()
            )
            if paid and result.report.outcome == "exhausted":
                paid_late += 1
    assert paid_late
    empty = replace(CONSUMER, requires=(ModuleRequirement("empty", ()),))
    assert check_module_wiring(empty, (), ()).report.outcome == "input_error"
    constrained = check(Budget(Limits(max_work=1)))
    assert constrained.report.outcome == "exhausted"
