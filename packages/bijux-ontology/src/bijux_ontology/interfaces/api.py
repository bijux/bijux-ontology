"""Supported structural checks for RDF vocabularies and application declarations."""

from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.data.query_rules.checking import run_query_rules
from bijux_ontology.data.query_rules.loading import (
    load_query_rules,
    query_rules_from_data,
)
from bijux_ontology.data.query_rules.types import (
    QueryAnswers,
    QueryRule,
    QueryRuleResult,
)
from bijux_ontology.data.rules import DataView, run_rules
from bijux_ontology.data.usage import check_data_usage
from bijux_ontology.declarations.codecs import (
    models_from_data,
    terms_from_data,
)
from bijux_ontology.declarations.types import (
    EmissionCondition,
    EmissionCoverage,
    EmissionGroup,
    EmissionScenario,
    FieldDeclaration,
    ModelDeclaration,
    OutputRequirement,
    SubjectIdentity,
    TermDeclaration,
    ValueDeclaration,
)
from bijux_ontology.documents.compaction import export_jsonld
from bijux_ontology.documents.export import RDFExport, export_rdf, write_export
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.interfaces.execution.executor import ExecutionResult, WorkerExecutor
from bijux_ontology.jsonld.checking import PreparedUsages, check_context, prepare_usages
from bijux_ontology.jsonld.codecs import expectations_from_data
from bijux_ontology.jsonld.comparison import (
    compare_context_usages,
    compare_serializations,
)
from bijux_ontology.jsonld.conversion import RDFConversion, convert_to_rdf
from bijux_ontology.jsonld.expansion import expand_document
from bijux_ontology.jsonld.expectations import ContextExpectation
from bijux_ontology.jsonld.flattening import (
    FlatNode,
    FlattenedDocument,
    flatten_document,
)
from bijux_ontology.jsonld.lists import ExpectedList
from bijux_ontology.jsonld.observations import ObservedOccurrence, ObservedUsage
from bijux_ontology.jsonld.preparation import JSONLDPreparation, prepare_jsonld
from bijux_ontology.jsonld.resources import (
    ContextInputError,
    OfflineDocumentLoader,
    context_document_loader,
)
from bijux_ontology.jsonld.selectors import (
    ArrayItems,
    EntitySelector,
    KeyedItems,
    Property,
    Selection,
    SourceSelector,
)
from bijux_ontology.jsonld.values import ValueTraits
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.models.checking import ModelAssessment, assess_models, check_models
from bijux_ontology.models.context_probes.checking import probe_context
from bijux_ontology.models.context_probes.types import ContextProbe, ProbeSample
from bijux_ontology.models.corpus import (
    BranchCoverage,
    CorpusCheck,
    ScenarioCoverage,
    SerializerCase,
    check_serializer_corpus,
)
from bijux_ontology.models.emission import ModelUsage, check_emission
from bijux_ontology.models.preparation import PreparedModels
from bijux_ontology.models.rdf_emission import RdfModelUsage, check_rdf_emission
from bijux_ontology.models.serialization import InstanceCheck, check_instance
from bijux_ontology.owl.obligations import (
    ObligationResult,
    OntologyObligation,
    prove_obligations,
)
from bijux_ontology.rdf.canonical.export import CanonicalRDF, canonicalize_rdf
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.queries.access import QueryResult, QueryScope, query_rdf
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.rdf.transforms import (
    Transformation,
    extract_neighbourhood,
    merge_sources,
    recheck_transformation,
    remap_iris,
)
from bijux_ontology.reports import (
    AcceptancePolicy,
    CheckError,
    Issue,
    IssueSelection,
    Location,
    Report,
    combine_reports,
)
from bijux_ontology.skos.checking import check_skos
from bijux_ontology.skos.collections import check_collection_sequence
from bijux_ontology.skos.labels import LabelHomonym, check_label_collisions
from bijux_ontology.skos.publication import (
    ConceptReachability,
    HierarchyStep,
    SchemeCoverage,
    TopConceptCheck,
    TopConceptPolicy,
    check_scheme_coverage,
    check_top_concepts,
)
from bijux_ontology.vocabulary.catalogs import OntologyVersion, load_catalog
from bijux_ontology.vocabulary.checking import check_vocabulary
from bijux_ontology.vocabulary.collections import (
    CollectionExpansion,
    CollectionOccurrence,
    expand_collection,
)
from bijux_ontology.vocabulary.comparison import (
    Change,
    VocabularyChanges,
    compare_vocabularies,
)
from bijux_ontology.vocabulary.inspection import VocabularyView, inspect_vocabulary
from bijux_ontology.vocabulary.loading import load_vocabulary
from bijux_ontology.vocabulary.registry import (
    IdentifierRegistry,
    NamespaceContribution,
    RegistryMerge,
    merge_identifier_registry,
)
from bijux_ontology.vocabulary.results import TermInfo
from bijux_ontology.vocabulary.templates import (
    IdentifierBatch,
    IdentifierRecognition,
    IdentifierRecord,
    IdentifierTemplate,
    TemplateKey,
)
from bijux_ontology.vocabulary.view import Vocabulary
from bijux_ontology.workflows.ontology.checking import check_ontology
from bijux_ontology.workflows.projects.checking import PreparedChecks, check_project
from bijux_ontology.workflows.projects.configuration import ProjectConfig, load_project
from bijux_ontology.workflows.projects.contracts import (
    ContractObligation,
    ContractResult,
    check_contract,
)
from bijux_ontology.workflows.upgrades.assessment import (
    UpgradeAssessment,
    assess_upgrade,
)
from bijux_ontology.workflows.upgrades.corpus import (
    ContextCorpusDocument,
    ContextCorpusUpgrade,
    CorpusBranch,
    CorpusWitness,
    assess_context_corpus,
)
from bijux_ontology.workflows.upgrades.uses import UseIndex, index_uses

__all__ = [
    "ABSENT",
    "Absent",
    "AcceptancePolicy",
    "ArrayItems",
    "BranchCoverage",
    "Budget",
    "CanonicalRDF",
    "Change",
    "CheckError",
    "CollectionExpansion",
    "CollectionOccurrence",
    "ContextCorpusDocument",
    "ContextCorpusUpgrade",
    "ContextExpectation",
    "ContextInputError",
    "ContextProbe",
    "ContractObligation",
    "ContractResult",
    "CorpusBranch",
    "CorpusCheck",
    "CorpusWitness",
    "DataView",
    "EntitySelector",
    "ExecutionResult",
    "ExpectedList",
    "EmissionCondition",
    "EmissionCoverage",
    "EmissionGroup",
    "EmissionScenario",
    "FieldDeclaration",
    "FlatNode",
    "FlattenedDocument",
    "IdentifierBatch",
    "IdentifierRecognition",
    "IdentifierRecord",
    "IdentifierRegistry",
    "IdentifierTemplate",
    "InputError",
    "InstanceCheck",
    "Issue",
    "IssueSelection",
    "JSONLDPreparation",
    "KeyedItems",
    "LabelHomonym",
    "LimitExceeded",
    "Limits",
    "Location",
    "ModelAssessment",
    "ModelDeclaration",
    "OutputRequirement",
    "NamespaceContribution",
    "ModelUsage",
    "ObligationResult",
    "ObservedOccurrence",
    "ObservedUsage",
    "OfflineDocumentLoader",
    "OntologyObligation",
    "OntologyVersion",
    "PreparedChecks",
    "PreparedModels",
    "PreparedSource",
    "PreparedUsages",
    "ProbeSample",
    "ProjectConfig",
    "Property",
    "QueryResult",
    "QueryAnswers",
    "QueryRule",
    "QueryRuleResult",
    "QueryScope",
    "RDFConversion",
    "RDFExport",
    "RdfModelUsage",
    "RdfSource",
    "RegistryMerge",
    "Report",
    "ResourceError",
    "ScenarioCoverage",
    "Selection",
    "SourceSelector",
    "SerializerCase",
    "SubjectIdentity",
    "TemplateKey",
    "TermDeclaration",
    "ConceptReachability",
    "HierarchyStep",
    "SchemeCoverage",
    "TopConceptCheck",
    "TopConceptPolicy",
    "TermInfo",
    "Transformation",
    "UpgradeAssessment",
    "UseIndex",
    "ValueDeclaration",
    "ValueTraits",
    "Vocabulary",
    "VocabularyChanges",
    "VocabularyView",
    "WorkerExecutor",
    "assess_context_corpus",
    "assess_models",
    "assess_upgrade",
    "canonicalize_rdf",
    "check_collection_sequence",
    "check_context",
    "check_contract",
    "check_data_usage",
    "check_emission",
    "check_instance",
    "check_label_collisions",
    "check_models",
    "check_ontology",
    "check_project",
    "check_rdf_emission",
    "check_serializer_corpus",
    "check_scheme_coverage",
    "check_skos",
    "check_top_concepts",
    "check_vocabulary",
    "combine_reports",
    "compare_context_usages",
    "compare_rdf",
    "compare_serializations",
    "compare_vocabularies",
    "context_document_loader",
    "convert_to_rdf",
    "expand_collection",
    "expand_document",
    "expectations_from_data",
    "export_jsonld",
    "export_rdf",
    "extract_neighbourhood",
    "flatten_document",
    "index_uses",
    "inspect_vocabulary",
    "load_project",
    "load_query_rules",
    "load_catalog",
    "load_vocabulary",
    "merge_identifier_registry",
    "merge_sources",
    "models_from_data",
    "prepare_jsonld",
    "prepare_source",
    "prepare_usages",
    "probe_context",
    "query_rdf",
    "query_rules_from_data",
    "prove_obligations",
    "recheck_transformation",
    "remap_iris",
    "run_rules",
    "run_query_rules",
    "terms_from_data",
    "write_export",
]
