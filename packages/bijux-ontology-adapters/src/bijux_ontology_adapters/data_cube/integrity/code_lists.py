"""Finite integrity families over the same owned normalized Cube facts."""

from rdflib import BNode, URIRef
from rdflib.namespace import OWL, RDF, SKOS
from rdflib.term import Node

from bijux_ontology.skos.subsets.selection import select_concepts
from bijux_ontology.vocabulary.browsing.navigation import traverse

from ..normalization import QB
from .progress import CubeIntegrity


def check_code_membership(state: CubeIntegrity) -> int:
    """Preserve normative membership through existing concept selection."""
    count = 0
    cache: dict[Node, tuple[frozenset[Node], bool, bool]] = {}
    for obs, code_list, value in state.read.code_obligations():
        if code_list not in cache:
            state.budget.consume("states")
            state.budget.consume("bytes", 128)
            kinds = set(state.read.values(code_list, RDF.type))
            if QB.HierarchicalCodeList in kinds and not kinds & {
                SKOS.ConceptScheme,
                SKOS.Collection,
                SKOS.OrderedCollection,
            }:
                cache[code_list] = frozenset(), False, False
                continue
            if (
                not isinstance(code_list, URIRef)
                or int(SKOS.ConceptScheme in kinds)
                + int(bool(kinds & {SKOS.Collection, SKOS.OrderedCollection}))
                != 1
            ):
                state.unknown(
                    code_list,
                    "IC19 needs a named unambiguous scheme or collection",
                )
                cache[code_list] = frozenset(), False, True
            else:
                scheme = SKOS.ConceptScheme in kinds
                selection = select_concepts(
                    state.read.code_view(),
                    scheme=str(code_list) if scheme else None,
                    collection=None if scheme else str(code_list),
                    closed=str(code_list) in state.closed,
                    deprecation="allow",
                    budget=state.budget,
                )
                state.accept(selection.report)
                qualified = selection.closed and selection.report.verified
                if not qualified:
                    state.unknown(
                        code_list,
                        "IC19 supplied code-list membership is open or incomplete",
                    )
                cache[code_list] = frozenset(selection.values), qualified, True
        values, qualified, applicable = cache[code_list]
        if not applicable:
            continue
        count += 1
        valid = value in values and (value, RDF.type, SKOS.Concept) in state.graph
        if (code_list, RDF.type, SKOS.ConceptScheme) in state.graph:
            valid &= (value, SKOS.inScheme, code_list) in state.graph
        if not valid:
            if qualified:
                state.finding(
                    "ic19_code_not_member",
                    obs,
                    "Dimension value is outside its declared code list",
                )
            else:
                state.unknown(
                    obs,
                    "IC19 cannot establish complete code-list membership",
                )
    return count


def hierarchy(state: CubeIntegrity, inverse: bool) -> int:
    """Reuse bounded traversal for forward or inverse hierarchies."""
    count = 0
    cache: dict[Node, tuple[frozenset[Node], bool] | None] = {}
    for obs, code_list, value in state.read.code_obligations():
        if (code_list, RDF.type, QB.HierarchicalCodeList) not in state.graph:
            continue
        parents = state.read.values(code_list, QB.parentChildProperty)
        is_inverse = len(parents) == 1 and isinstance(parents[0], BNode)
        if inverse != is_inverse:
            continue
        count += 1
        if code_list not in cache:
            state.budget.consume("states")
            state.budget.consume("bytes", 128)
            cache[code_list] = None
            if len(parents) != 1 or not isinstance(code_list, URIRef):
                state.unknown(
                    code_list,
                    "Hierarchy needs one supported property and named list",
                )
                continue
            prop = parents[0]
            if inverse:
                values = state.read.values(prop, OWL.inverseOf)
                prop = values[0] if len(values) == 1 else prop
            roots = state.read.values(code_list, QB.hierarchyRoot)
            if (
                not isinstance(prop, URIRef)
                or not roots
                or any(not isinstance(root, URIRef) for root in roots)
            ):
                state.unknown(
                    code_list,
                    "Hierarchy needs named roots and a forward/inverse property",
                )
                continue
            reached: set[Node] = set()
            qualified = str(code_list) in state.closed
            for root in roots:
                walk = traverse(
                    state.read.code_view(),
                    str(root),
                    predicates=(str(prop),),
                    direction="incoming" if inverse else "outgoing",
                    include_self=True,
                    budget=state.budget,
                )
                state.accept(walk.report)
                qualified &= walk.report.complete
                state.budget.consume("states", len(walk.items))
                state.budget.consume("bytes", 64 * len(walk.items))
                reached.update(URIRef(item.iri) for item in walk.items)
            cache[code_list] = frozenset(reached), qualified
            if not qualified:
                state.unknown(
                    code_list,
                    "Supplied hierarchy is not declared closed and complete",
                )
        result = cache[code_list]
        if result is not None and value not in result[0]:
            if result[1]:
                state.finding(
                    "ic21_code_not_reachable" if inverse else "ic20_code_not_reachable",
                    obs,
                    "Dimension value is outside the declared code hierarchy",
                )
            else:
                state.unknown(obs, "Code hierarchy membership cannot be established")
    return count


def check_hierarchy_paths(state: CubeIntegrity) -> int:
    """Check the supplied ordinary parent-to-child hierarchy interpretation."""
    return hierarchy(state, False)


def check_inverse_hierarchy_paths(state: CubeIntegrity) -> int:
    """Check the supplied explicitly inverse parent-to-child interpretation."""
    return hierarchy(state, True)
