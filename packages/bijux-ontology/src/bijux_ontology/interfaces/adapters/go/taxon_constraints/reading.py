"""Exact pinned resource preparation and native GO taxon union inspection."""

from dataclasses import dataclass
from hashlib import sha256

from rdflib import BNode, Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import IssueCollector, bound_text, retain_report
from bijux_ontology.rdf.patches.types import PatchUnavailable
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.metadata import SnapshotMetadata
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.sources.types import RdfSource
from bijux_ontology.rdf.structure import read_rdf_list
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.vocabulary.browsing.inspection import (
    VocabularyView,
    inspect_vocabulary,
)
from bijux_ontology.vocabulary.browsing.results import Edge
from bijux_ontology.vocabulary.restrictions.projection import project_restrictions
from bijux_ontology.vocabulary.sources.loading import load_vocabulary

from .types import GoTaxonReceipt, GoTaxonRelease, GoTaxonResource, TaxonConstraint, iri

IN_TAXON = URIRef("http://purl.obolibrary.org/obo/RO_0002162")
ONLY_IN_TAXON = URIRef("http://purl.obolibrary.org/obo/RO_0002160")
NEVER_IN_TAXON = URIRef("http://purl.obolibrary.org/obo/RO_0002161")
PART_OF = URIRef("http://purl.obolibrary.org/obo/BFO_0000050")
UNION_PREFIX = "http://purl.obolibrary.org/obo/NCBITaxon_Union_"


@dataclass(frozen=True)
class _Resource:
    """One prepared local source and its exact version/hash/role receipt."""

    descriptor: RdfSource
    source: PreparedSource
    view: VocabularyView
    graph: Graph
    receipt: GoTaxonReceipt
    metadata: SnapshotMetadata


class Reader:
    """Keep native scoped views and original group/axiom witnesses distinct."""

    def __init__(
        self,
        operation: Operation,
        issues: IssueCollector,
        reports: list[Report],
        receipts: list[GoTaxonReceipt],
    ) -> None:
        """Reuse the enclosing operation's allowance and report references."""
        self.operation, self.issues, self.reports, self.receipts = (
            operation,
            issues,
            reports,
            receipts,
        )
        self.resources: dict[str, _Resource] = {}
        self.groups: dict[URIRef, tuple[tuple[URIRef, ...], tuple[Edge, ...]]] = {}
        self.authored_owners: tuple[URIRef, ...] = ()

    def reserve(self, count: int = 1) -> None:
        """Prepay returned report/observation reference storage before execution."""
        self.operation.consume("states", count + 2)
        self.operation.consume("bytes", 128 + 64 * count)

    def adopt(self, report: Report) -> None:
        """Retain native evidence before fallible metadata/report adoption."""
        self.reports.append(report)
        retain_report(report, self.operation, self.issues)

    def load(self, release: GoTaxonRelease) -> None:
        """Verify immutable byte pins, then use existing native/offline preparation."""
        if (
            not isinstance(release, GoTaxonRelease)
            or release.convention != "go-computed-2024-09"
        ):
            raise PatchUnavailable(
                "Select the supported explicit GO computed convention"
            )
        supplied = [
            ("ontology", release.ontology),
            ("computed", release.computed),
            ("groupings", release.groupings),
            ("taxonomy", release.taxonomy),
        ]
        for extra in capture_items(release.imports, GoTaxonResource, self.operation):
            supplied.append(("import", extra))
        captured: list[tuple[str, str, str, str, RdfSource]] = []
        bindings: dict[str, RdfSource] = {}
        for role, resource in supplied:
            if resource is None:
                continue
            if type(resource) is not GoTaxonResource:
                raise InputError(
                    "GO resource descriptors require ordinary GoTaxonResource"
                )
            content, format_name, document, version, digest, graph_name = (
                resource.content,
                resource.format,
                resource.document_iri,
                resource.release,
                resource.sha256,
                resource.graph_name,
            )
            for value in (format_name, document, version, digest):
                bound_text(value, self.operation)
            if graph_name is not None:
                bound_text(graph_name, self.operation)
            if (
                type(content) is not bytes
                or len(content) > self.operation.limits.max_bytes
            ):
                raise LimitExceeded(
                    "GO resource exceeds its immutable input byte bound"
                )
            self.operation.consume("work", len(content))
            self.operation.consume("bytes", len(content))
            if sha256(content).hexdigest() != digest:
                raise InputError(
                    "GO resource bytes differ from their selected release pin"
                )
            if document in bindings:
                raise InputError(
                    "Resource roles need distinct unambiguous document identities"
                )
            self.reserve()
            descriptor = RdfSource(
                content, format_name, document_iri=document, graph_name=graph_name
            )
            bindings[document] = descriptor
            captured.append((role, document, version, digest, descriptor))
            self.receipts.append(
                GoTaxonReceipt(role, document, version, digest, format_name, graph_name)
            )
        for role, document, version, digest, descriptor in captured:
            if role == "import":
                continue
            source = prepare_rdf(
                descriptor, budget=self.operation, require_complete_graphs=True
            )
            if source.union or source.is_dataset and source.graph_name is None:
                raise PatchUnavailable("GO roles require one explicit physical graph")
            vocabulary = load_vocabulary(
                source, imports=bindings, budget=self.operation
            )
            view = inspect_vocabulary(
                vocabulary, scopes=("local",), budget=self.operation
            )
            self.reserve()
            self.resources[role] = _Resource(
                descriptor,
                source,
                view,
                source._read(budget=self.operation),
                GoTaxonReceipt(
                    role,
                    document,
                    version,
                    digest,
                    descriptor.format or "",
                    descriptor.graph_name,
                ),
                SnapshotMetadata.capture(source, self.operation),
            )
        if "ontology" not in self.resources or "computed" not in self.resources:
            raise PatchUnavailable(
                "Ontology and computed resources are required; go-basic is insufficient"
            )
        if "taxonomy" not in self.resources:
            raise PatchUnavailable("Taxon checking requires explicit taxonomy ancestry")

    def constraints(self) -> tuple[TaxonConstraint, ...]:
        """Keep actual direct computed records and authored existential restrictions."""
        output: list[TaxonConstraint] = []
        for role in ("computed", "ontology"):
            resource = self.resources[role]
            self.reserve()
            statements = resource.view.all_statements(budget=self.operation)
            self.adopt(statements.report)
            owners: set[URIRef] = set()
            for edge in statements.items:
                self.operation.consume("work")
                if edge.predicate in (IN_TAXON, ONLY_IN_TAXON, NEVER_IN_TAXON):
                    if not isinstance(edge.subject, URIRef) or not isinstance(
                        edge.object, URIRef
                    ):
                        raise PatchUnavailable(
                            "Taxon targets require named taxa or unions"
                        )
                    self.reserve()
                    output.append(
                        TaxonConstraint(
                            edge.subject,
                            "never" if edge.predicate == NEVER_IN_TAXON else "only",
                            edge.object,
                            role,
                            edge,
                        )
                    )
                if (
                    role == "ontology"
                    and isinstance(edge.subject, URIRef)
                    and edge.predicate in (RDFS.subClassOf, OWL.equivalentClass)
                    and isinstance(edge.object, BNode)
                ):
                    self.operation.consume("states")
                    owners.add(edge.subject)
            if role == "ontology":
                self.operation.consume("states", len(owners))
                self.authored_owners = tuple(owners)
        self.operation.consume("states", len(output))
        return tuple(output)

    def authored(self, owner: URIRef) -> tuple[TaxonConstraint, ...]:
        """Project only owners actually reached by selected GO inheritance."""
        self.reserve()
        projected = project_restrictions(
            self.resources["ontology"].view, str(owner), budget=self.operation
        )
        self.adopt(projected.report)
        output = []
        for record in projected.items:
            self.operation.consume("work")
            if record.property not in (IN_TAXON, ONLY_IN_TAXON):
                continue
            if (
                record.status != "supported"
                or record.quantifier != OWL.someValuesFrom
                or not isinstance(record.filler, URIRef)
            ):
                raise PatchUnavailable(
                    "Authored taxon restriction is outside the supported profile"
                )
            self.reserve()
            output.append(
                TaxonConstraint(owner, "only", record.filler, "ontology", record)
            )
        return tuple(output)

    def grouping(
        self, target: URIRef, active: frozenset[URIRef] = frozenset()
    ) -> tuple[tuple[URIRef, ...], tuple[Edge, ...]]:
        """Read an actual named union definition; never interpret its label text."""
        iri(target, self.operation)
        if target in self.groups:
            return self.groups[target]
        if target in active or len(active) >= self.operation.limits.max_depth:
            raise PatchUnavailable(
                "Taxon union grouping is cyclic or exceeds its finite profile"
            )
        grouping = self.resources.get("groupings")
        taxonomy = self.resources["taxonomy"]
        if grouping is None:
            if (
                str(target).startswith(UNION_PREFIX)
                or taxonomy.view.lookup(str(target)).status != "declared"
            ):
                raise PatchUnavailable("Required named taxon grouping is unavailable")
            return (target,), ()
        graph = grouping.graph
        definitions = []
        has_expression = False
        self.reserve()
        for expression in graph.objects(target, OWL.equivalentClass):
            self.operation.consume("work")
            bound_node(expression, self.operation)
            if has_expression or not isinstance(expression, BNode):
                raise PatchUnavailable(
                    "Taxon grouping requires one anonymous union equivalence"
                )
            has_expression = True
            for predicate, value in graph.predicate_objects(expression):
                self.operation.consume("work")
                bound_node(predicate, self.operation)
                bound_node(value, self.operation)
                if predicate == RDF.type and value == OWL.Class:
                    continue
                if predicate != OWL.unionOf:
                    raise PatchUnavailable(
                        "Taxon union expression has an unsupported additional condition"
                    )
                self.reserve()
                definitions.append((expression, value))
        if not definitions:
            if has_expression:
                raise PatchUnavailable("Taxon grouping equivalence is unsupported")
            if (
                str(target).startswith(UNION_PREFIX)
                or taxonomy.view.lookup(str(target)).status != "declared"
            ):
                raise PatchUnavailable(
                    "Required grouping has no supported union definition"
                )
            return (target,), ()
        if len(definitions) != 1:
            raise PatchUnavailable("Taxon grouping has competing union definitions")
        expression, head = definitions[0]
        self.operation.consume("states", 2)
        edges = [
            Edge(target, OWL.equivalentClass, expression, "groupings"),
            Edge(expression, OWL.unionOf, head, "groupings"),
        ]
        members = read_rdf_list(
            graph,
            head,
            max_items=self.operation.limits.max_states,
            budget=self.operation,
        )
        if not members:
            raise PatchUnavailable(
                "An empty taxon union cannot satisfy required grouping coverage"
            )
        alternatives: list[URIRef] = []
        current = head
        for member in members:
            self.operation.consume("work")
            if not isinstance(member, URIRef):
                raise PatchUnavailable(
                    "Taxon group members require named taxa/groupings"
                )
            self.reserve(3)
            rest = graph.value(current, RDF.rest)
            assert rest is not None
            edges.extend(
                (
                    Edge(current, RDF.first, member, "groupings"),
                    Edge(current, RDF.rest, rest, "groupings"),
                )
            )
            nested, structure = self.grouping(member, active | {target})
            alternatives.extend(nested)
            edges.extend(structure)
            current = rest
        self.reserve(len(alternatives) + len(edges))
        result = tuple(dict.fromkeys(alternatives)), tuple(edges)
        self.groups[target] = result
        return result
