"""Supported structural checks for RDF vocabularies and application declarations."""

from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.data.compatibility import check_data_compatibility
from bijux_ontology.data.keys import (
    KeyProjection,
    KeyRecord,
    check_record_keys,
    project_record_key,
)
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
from bijux_ontology.data.topology import TopologyRecord, check_record_topology
from bijux_ontology.data.usage import check_data_usage
from bijux_ontology.declarations.codecs import (
    models_from_data,
    terms_from_data,
)
from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.declarations.keys import KeyComponent, KeyContract
from bijux_ontology.declarations.path_fields import (
    IntermediateRequirement,
    PathFieldDeclaration,
)
from bijux_ontology.declarations.resource_roles import ResourceRole, RoleBridge
from bijux_ontology.declarations.topology import TopologyContract, TopologyRelation
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
from bijux_ontology.declarations.value_constraints import ValueConstraints
from bijux_ontology.documents.compaction import export_jsonld
from bijux_ontology.documents.export import RDFExport, export_rdf, write_export
from bijux_ontology.documents.forms import (
    DocumentFormCheck,
    FormRequirement,
    check_document_form,
)
from bijux_ontology.documents.migrations import export_migration
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
from bijux_ontology.jsonld.index_maps import (
    IndexBinding,
    IndexMapCheck,
    IndexMapExpectation,
    IndexMapObservation,
    check_index_maps,
)
from bijux_ontology.jsonld.lists import ExpectedList
from bijux_ontology.jsonld.observations import ObservedOccurrence, ObservedUsage
from bijux_ontology.jsonld.preparation import JSONLDPreparation, prepare_jsonld
from bijux_ontology.jsonld.resources import (
    ContextInputError,
    OfflineDocumentLoader,
    context_document_loader,
)
from bijux_ontology.jsonld.selectors.types import (
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
from bijux_ontology.models.context_compilation import CompiledContext, compile_context
from bijux_ontology.models.context_probes.checking import probe_context
from bijux_ontology.models.context_probes.types import ContextProbe, ProbeSample
from bijux_ontology.models.corpus import (
    BranchCoverage,
    CorpusCheck,
    ScenarioCoverage,
    SerializerCase,
    check_serializer_corpus,
)
from bijux_ontology.models.emission.checking import check_emission
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.serialization import InstanceCheck, check_instance
from bijux_ontology.models.emission.types import ModelUsage, RdfModelUsage
from bijux_ontology.models.path_fields import PathFieldUsage, check_path_fields
from bijux_ontology.models.preparation import PreparedModels
from bijux_ontology.models.resource_roles import RoleUse, check_resource_roles
from bijux_ontology.owl.obligations import (
    ObligationResult,
    OntologyObligation,
    prove_obligations,
)
from bijux_ontology.rdf.canonical.export import CanonicalRDF, canonicalize_rdf
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.datatype_evaluators import (
    DatatypeConversion,
    DatatypeEvaluator,
    DatatypeRegistry,
)
from bijux_ontology.rdf.migrations import MigrationCandidate, MigrationCheck
from bijux_ontology.rdf.migrations.graphs import GraphRoute, migrate_graphs
from bijux_ontology.rdf.migrations.values import (
    LiteralConversion,
    ValueMap,
    ValueMigrationCandidate,
    ValueSelection,
    migrate_values,
)
from bijux_ontology.rdf.patches import (
    BlankNodePair,
    DatasetPatch,
    PatchBlank,
    PatchBlankBinding,
    PatchLiteral,
    PatchPlan,
    PatchQuad,
    SnapshotWitness,
    apply_rdf_patch,
    patch_from_data,
    patch_to_data,
    plan_rdf_patch,
)
from bijux_ontology.rdf.queries.access import QueryResult, QueryScope, query_rdf
from bijux_ontology.rdf.roles import TermRoleOccurrence, classify_statement
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.rdf.transforms import (
    Transformation,
    extract_neighbourhood,
    merge_sources,
    recheck_transformation,
    remap_iris,
)
from bijux_ontology.rdf.value_comparison import (
    LiteralValueComparison,
    compare_literal_values,
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
from bijux_ontology.skos.label_relations import (
    XLRelation,
    XLRelationPolicy,
    check_xl_relations,
)
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
from bijux_ontology.skos.subsets import ConceptSubset, select_concepts
from bijux_ontology.vocabulary.annotations import (
    AnnotationCoverage,
    AnnotationRequirement,
    LanguageCoverage,
    LanguageRequirement,
    check_annotation_coverage,
)
from bijux_ontology.vocabulary.anonymous import (
    AnonymousChange,
    AnonymousDifferences,
    NamedAttachment,
)
from bijux_ontology.vocabulary.browsing import (
    VocabularyCursor,
    VocabularyPage,
    search_page,
    statements_page,
    terms_page,
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
from bijux_ontology.vocabulary.facts import (
    AnonymousFactComparison,
    FactGraph,
    FactPredicate,
    FactSelection,
    SelectedFact,
    SelectedFactDifference,
    SelectedFactDifferences,
    compare_selected_facts,
)
from bijux_ontology.vocabulary.inspection import VocabularyView, inspect_vocabulary
from bijux_ontology.vocabulary.languages import matches_language, negotiate_annotations
from bijux_ontology.vocabulary.loading import load_vocabulary
from bijux_ontology.vocabulary.obo import (
    OboAttachment,
    OboRecord,
    OboRecords,
    read_obo_records,
)
from bijux_ontology.vocabulary.registry import (
    IdentifierRegistry,
    NamespaceContribution,
    RegistryMerge,
    merge_identifier_registry,
)
from bijux_ontology.vocabulary.restrictions import (
    RestrictionPath,
    RestrictionProjection,
    existential_path,
    project_restrictions,
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
from bijux_ontology.workflows.projects.building import (
    ApplicationInput,
    CorpusDocumentInput,
    ProjectBuilder,
)
from bijux_ontology.workflows.projects.checking import PreparedChecks, check_project
from bijux_ontology.workflows.projects.configuration import ProjectConfig, load_project
from bijux_ontology.workflows.projects.contracts import (
    ContractObligation,
    ContractResult,
    check_contract,
)
from bijux_ontology.workflows.upgrades.anonymous import AnonymousAffectedUse
from bijux_ontology.workflows.upgrades.applications import (
    ApplicationDeclarationChange,
    ApplicationField,
    ApplicationOutput,
    ApplicationSampleComparison,
    ApplicationUpgrade,
    ApplicationValueChange,
    ApplicationVersion,
    compare_applications,
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
from bijux_ontology.workflows.upgrades.facts import FactAffectedUse
from bijux_ontology.workflows.upgrades.uses import UseIndex, index_uses

__all__ = [
    "GraphRoute",
    "migrate_graphs",
    "LiteralConversion",
    "MigrationCandidate",
    "MigrationCheck",
    "ValueMap",
    "ValueMigrationCandidate",
    "ValueSelection",
    "export_migration",
    "migrate_values",
    "BlankNodePair",
    "DatasetPatch",
    "PatchBlank",
    "PatchBlankBinding",
    "PatchLiteral",
    "PatchPlan",
    "PatchQuad",
    "SnapshotWitness",
    "apply_rdf_patch",
    "patch_from_data",
    "patch_to_data",
    "plan_rdf_patch",
    "ApplicationDeclarationChange",
    "ApplicationField",
    "ApplicationInput",
    "ApplicationOutput",
    "ApplicationSampleComparison",
    "ApplicationUpgrade",
    "ApplicationValueChange",
    "ApplicationVersion",
    "compare_applications",
    "IntermediateRequirement",
    "PathFieldDeclaration",
    "PathFieldUsage",
    "check_path_fields",
    "TopologyContract",
    "TopologyRelation",
    "TopologyRecord",
    "check_record_topology",
    "KeyComponent",
    "KeyContract",
    "KeyProjection",
    "KeyRecord",
    "check_record_keys",
    "project_record_key",
    "ABSENT",
    "Absent",
    "AcceptancePolicy",
    "AnnotationCoverage",
    "AnnotationRequirement",
    "ArrayItems",
    "BranchCoverage",
    "Budget",
    "CanonicalRDF",
    "Change",
    "CheckError",
    "CollectionExpansion",
    "CollectionOccurrence",
    "CompiledContext",
    "ContextCorpusDocument",
    "ContextCorpusUpgrade",
    "ContextExpectation",
    "ContextInputError",
    "ContextProbe",
    "ConceptSubset",
    "ContractObligation",
    "ContractResult",
    "CorpusBranch",
    "CorpusDocumentInput",
    "CorpusCheck",
    "CorpusWitness",
    "DataView",
    "DatatypeConversion",
    "DatatypeEvaluator",
    "DatatypeRegistry",
    "DocumentFormCheck",
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
    "FormRequirement",
    "IdentifierBatch",
    "IdentifierRecognition",
    "IdentifierRecord",
    "IdentifierRegistry",
    "IdentifierTemplate",
    "IndexBinding",
    "IndexMapCheck",
    "IndexMapExpectation",
    "IndexMapObservation",
    "InputError",
    "InstanceCheck",
    "Issue",
    "IssueSelection",
    "JSONLDPreparation",
    "KeyedItems",
    "LabelHomonym",
    "LanguageCoverage",
    "LanguageRequirement",
    "LimitExceeded",
    "Limits",
    "Location",
    "LiteralValueComparison",
    "ModelAssessment",
    "ModelDeclaration",
    "OutputRequirement",
    "OboAttachment",
    "OboRecord",
    "OboRecords",
    "NamedAttachment",
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
    "ProjectBuilder",
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
    "RestrictionPath",
    "RestrictionProjection",
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
    "ValueConstraints",
    "ResourceRole",
    "RoleBridge",
    "RoleUse",
    "TermRoleOccurrence",
    "ValueDeclaration",
    "ValueEnumeration",
    "ValueTraits",
    "Vocabulary",
    "AnonymousAffectedUse",
    "AnonymousChange",
    "AnonymousDifferences",
    "AnonymousFactComparison",
    "FactAffectedUse",
    "FactGraph",
    "FactPredicate",
    "FactSelection",
    "SelectedFact",
    "SelectedFactDifference",
    "SelectedFactDifferences",
    "VocabularyChanges",
    "VocabularyCursor",
    "VocabularyPage",
    "VocabularyView",
    "WorkerExecutor",
    "XLRelation",
    "XLRelationPolicy",
    "assess_context_corpus",
    "assess_models",
    "assess_upgrade",
    "canonicalize_rdf",
    "check_annotation_coverage",
    "check_collection_sequence",
    "check_context",
    "check_contract",
    "check_resource_roles",
    "classify_statement",
    "check_data_compatibility",
    "check_data_usage",
    "check_document_form",
    "check_emission",
    "check_instance",
    "check_index_maps",
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
    "check_xl_relations",
    "combine_reports",
    "compare_context_usages",
    "compare_rdf",
    "compare_literal_values",
    "compare_serializations",
    "compare_selected_facts",
    "compare_vocabularies",
    "compile_context",
    "context_document_loader",
    "convert_to_rdf",
    "existential_path",
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
    "matches_language",
    "negotiate_annotations",
    "merge_sources",
    "models_from_data",
    "prepare_jsonld",
    "prepare_source",
    "prepare_usages",
    "probe_context",
    "project_restrictions",
    "query_rdf",
    "query_rules_from_data",
    "prove_obligations",
    "read_obo_records",
    "recheck_transformation",
    "remap_iris",
    "run_rules",
    "run_query_rules",
    "search_page",
    "select_concepts",
    "statements_page",
    "terms_page",
    "terms_from_data",
    "write_export",
]
