"""Stable diagnostic explanations scoped to the checks this package implements."""

from dataclasses import dataclass
from typing import cast


@dataclass(frozen=True)
class DiagnosticExplanation:
    """Human guidance, independent of severity, acceptance policy or check results."""

    code: str
    scope: str
    meaning: str
    action: str
    example: str

    def to_dict(self) -> dict[str, str]:
        """Return the installed explanation without altering an original finding."""
        return {
            "code": self.code,
            "scope": self.scope,
            "meaning": self.meaning,
            "action": self.action,
            "example": self.example,
        }


# Suffixes are exact registered diagnostics, not arbitrary prefix matches. Family
# guidance shares scope and remedies without duplicating implementation messages.
_CODES = {
    "analysis": frozenset(
        "annotation_unknown term_unknown parent_unknown projection_unknown "
        "hierarchy_cycle zero_frequency disconnected input".split()
    ),
    "projection": frozenset(
        "term_unverified value_invalid reference_unverified conversion_unverified "
        "path_unverified ambiguous cardinality list_invalid graph_unverified "
        "identity subject_unverified type_missing invalid_input".split()
    ),
    "shex": frozenset(
        "unsupported input_invalid backend_unavailable not_matched engine_error".split()
    ),
    "quantity": frozenset(
        "unit_unverified backend_unverified field_missing dimension_mismatch "
        "bound_mismatch unverified".split()
    ),
    "temporal": frozenset(
        "order_unverified value_missing order_violation invalid".split()
    ),
    "csvw": frozenset(
        "cell_invalid duplicate_key extraction_unverified input_invalid".split()
    ),
    "jsonld": frozenset("record_unavailable stream_empty".split()),
    "mapping": frozenset(
        "endpoint_unverified endpoint_kind semantics_unverified relation_forbidden "
        "relation_conflict maximum_exceeded minimum_missing invalid_input "
        "interchange_unverified composition_input_unverified "
        "composition_rule_unverified composition_semantics_unverified "
        "composition_invalid proposal_invalid "
        "reconciliation_unverified reconciliation_invalid".split()
    ),
    "taxonomy": frozenset(
        "class_role_unverified expression_unrepresented outside_parent "
        "annotation_invalid label_choice_invalid label_ambiguous "
        "scope_unverified".split()
    ),
    "acquisition": frozenset("resource_failed unavailable".split()),
    "publication": frozenset(
        "identity_mismatch media_mismatch profile_mismatch "
        "graph_scope_unverified reference_unverified variant_unverified "
        "input_unverified".split()
    ),
    "schema": frozenset(
        "translation_invalid translation_unverified "
        "output_invalid output_unverified".split()
    ),
    "roles": frozenset(
        "role_unverified punning_forbidden instance_type_missing "
        "bridge_unverified type_conflict profile_unavailable".split()
    ),
    "documents": frozenset(
        "form_shape form_key_missing form_key_unexpected form_embedding "
        "form_binding_unverified form_scope_unverified "
        "relocation_unverified relocation_invalid "
        "patch_precondition_failed patch_invalid patch_unverified "
        "patch_retained_missing patch_retained_changed "
        "patch_rebinding_unverified".split()
    ),
    "obo": frozenset(
        "xref_invalid xref_unverified xref_input_error "
        "publication_text publication_qualification publication_definition_xref "
        "publication_synonym_type publication_unverified "
        "alternative_id_unverified alternative_id_ambiguous "
        "attachment_shape attachment_unverified base_missing "
        "synonym_type_shape value_kind "
        "subset_undefined subset_definition_kind subset_membership_kind "
        "subset_unverified".split()
    ),
    "annotations": frozenset(
        "attachment_shape graph_unverified comparison_unverified "
        "hygiene_violation hygiene_unverified "
        "minimum_missing maximum_exceeded language_minimum_missing "
        "language_maximum_exceeded value_kind datatype entity_unresolved".split()
    ),
    "schemaorg": frozenset(
        "expectation_mismatch expectation_unverified "
        "selection_unverified superseded".split()
    ),
    "sdrf": frozenset("unverified semantic_mismatch consumer".split()),
    "serialization": frozenset(("coverage_missing",)),
    "selector": frozenset(
        "parent_count_mismatch count_mismatch "
        "structure_mismatch binding_unverified".split()
    ),
    "adapter": frozenset(
        "obographs_unverified obographs_input_error obographs_projection_loss "
        "oak_python_unsupported oak_backend_unavailable oak_unsupported "
        "fhir_backend_unavailable fhir_input_error fhir_unsupported "
        "fhir_membership_unverified fhir_export_error fhir_export_unverified "
        "fhir_valueset_unverified fhir_valueset_input_error fhir_valueset_code_invalid "
        "shape_binding_unverified shape_constraint_untranslated "
        "shape_list_unverified shape_output_backend_unavailable "
        "shape_representation_unverified shape_source_scope_unverified "
        "bioregistry_unverified bioregistry_input_error "
        "schema_unverified schema_mismatch "
        "alias_unverified class_mapping_unverified computed_output_unverified "
        "emission_unverified excluded_emission_conflict mapping_conflict "
        "mapping_unverified field_contract_conflict alias_contract_conflict "
        "output_unverified predicate_contract_conflict".split()
    ),
    "context": frozenset(
        "publication_mismatch publication_unverified "
        "composition_unverified "
        "html_extraction_unverified "
        "frame_unverified frame_input_error "
        "term_usage_disallowed term_usage_membership "
        "term_usage_unverified term_usage_input_error "
        "number_not_portable number_not_exact number_rdf_not_exact "
        "number_projection_missing number_unverified number_input_error "
        "definition_changed defaults_changed definition_unverified "
        "compilation_conflict compilation_unverified "
        "index_mismatch list_index_mismatch index_keys_mismatch "
        "index_origin_mismatch index_unverified index_binding_mismatch "
        "index_binding_missing "
        "list_group_mismatch list_order_mismatch list_unverified "
        "probe_unverified "
        "change_unverified before_input_error after_input_error "
        "container_mismatch datatype_mismatch "
        "flatten_correspondence_unverified flatten_input_error "
        "direction_mismatch identifier_kind_mismatch identifier_mismatch "
        "input_error invalid_path language_mismatch mapping_mismatch "
        "omission_invalid omission_unverified required_scope_excluded "
        "order_mismatch order_unverified property_dropped property_omitted "
        "rdf_direction_unverified rdf_direction_loss rdf_empty_graph_loss "
        "rdf_identifier_loss "
        "rdf_interoperability_unverified rdf_language_invalid rdf_literal_loss "
        "rdf_output_unverified reverse_mismatch serialization_unverified "
        "term_dropped types_mismatch unsupported_check usage_dropped "
        "usage_meaning_changed value_kind_mismatch value_unverified".split()
    ),
    "data": frozenset(
        "stream_unverified stream_global_unverified "
        "consolidation_unverified consolidation_conflict consolidation_input_error "
        "layout_unverified layout_count_mismatch layout_graph_forbidden "
        "layout_content_misplaced layout_input_error "
        "topology_binding_unverified topology_corpus_unverified topology_cycle "
        "topology_owners topology_path_unverified topology_record_missing "
        "topology_reference_missing topology_reference_unverified "
        "topology_roots_unverified topology_scope_unverified "
        "topology_target_invalid topology_unreachable "
        "foreign_target_duplicate foreign_lookup_unverified foreign_missing "
        "foreign_ambiguous foreign_source_unverified "
        "key_absent key_multiple key_collision key_binding_unverified "
        "key_path_unverified key_normalization_unverified "
        "key_comparison_unverified key_corpus_unverified key_projection_unverified "
        "disjoint_conflict expression_unverified asserted_type_missing "
        "datatype_conflict datatype_unverified property_unverified property_conflict "
        "type_unverified query_kind_mismatch query_answer_mismatch "
        "query_variables_mismatch query_answer_unexpected query_answer_missing "
        "query_violation query_empty_scope".split()
    ),
    "contract": frozenset("coverage_missing unavailable".split()),
    "project": frozenset("dependency_unverified job_pending input_error".split()),
    "profile": frozenset(
        "instantiation_unverified "
        "routing_unverified record_unverified declaration_missing "
        "declaration_ambiguous declaration_unknown "
        "composition_unverified description_unverified shape_unverified "
        "shape_conflict".split()
    ),
    "emission": frozenset(
        "path_mapping_unverified path_intermediate_count_mismatch "
        "path_intermediate_mismatch path_ownership_mismatch "
        "source_field_undeclared predicate_undeclared source_coverage_unverified "
        "extension_allowed "
        "group_unverified group_count_mismatch scenario_unverified "
        "scenario_mismatch "
        "cardinality_unverified empty_forbidden list_length_mismatch list_required "
        "null_forbidden source_presence_unverified value_count_mismatch "
        "binding_conflict nested_scope_unverified node_kind_mismatch node_unverified "
        "subject_mismatch graph_mismatch graph_unverified "
        "reference_unverified reference_missing required_entity_fact_missing "
        "required_occurrence_missing types_mismatch value_mismatch "
        "value_unverified".split()
    ),
    "execution": frozenset(
        "cancelled deadline incomplete limit_exceeded worker_failed".split()
    ),
    "input": frozenset(
        "coverage_scope_missing empty_coverage empty_scope invalid "
        "literal_declaration_conflict model_identity_conflict "
        "model_reference_missing output_alias value_declaration_conflict "
        "value_kind_invalid".split()
    ),
    "literal": frozenset(
        "conversion_unverified datatype_unverified "
        "lexical_invalid lexical_unverified value_unverified "
        "json_invalid json_mismatch json_unverified "
        "json_schema_mismatch json_schema_unverified "
        "markup_invalid markup_unverified markup_value_changed".split()
    ),
    "models": frozenset(
        "direction_unverified contextual_direction_unverified "
        "emission_group_conflict emission_group_unverified "
        "scenario_coverage_unverified emission_scenario_conflict "
        "scenario_condition_conflict scenario_condition_unverified "
        "bottom_property_conflict conditional_emission_unverified "
        "container_unverified datatype_conflict datatype_unverified "
        "disjoint_conflict disjoint_group_unverified expression_unverified "
        "guaranteed_emission_conflict incoming_contract_conflict "
        "keyword_unverified limit_exceeded list_head_kind_conflict "
        "list_unverified parent_kind_conflict property_expression_unverified "
        "relationship_unverified reverse_literal_conflict "
        "sample_contract_conflict type_unverified value_kind_conflict "
        "value_unverified".split()
    ),
    "owl": frozenset(
        "axiom_input_loss axiom_input_unverified axiom_unsupported axioms_unverified "
        "conversion_loss conversion_unverified "
        "assertion_contradiction assertion_unverified backend_unavailable "
        "loader_unverified profile_unverified profile_violation".split()
    ),
    "ontology": frozenset(
        "module_export_missing module_kind_mismatch module_obligation_unverified "
        "module_obligation_contradicted module_input_error "
        "ownership_unverified ownership_input_error owner_conflict "
        "definition_redefined "
        "annotation_shape annotation_statement_missing cardinality_shape "
        "obligation_unknown obligation_contradicted obligation_exhausted "
        "characteristic_conflict datatype_restriction_shape "
        "declaration_conflict deprecated_term deprecation_unverified "
        "replacement_absent replacement_ambiguous replacement_shape_unverified "
        "replacement_unresolved replacement_kind_unverified replacement_kind_mismatch "
        "expression_arity "
        "expression_component expression_list expression_operand "
        "expression_unverified facet_shape group_shape metadata_required "
        "qualified_cardinality_shape reference_kind reference_missing "
        "reference_shape restriction_filler restriction_shape "
        "self_restriction_shape version_ambiguous version_header version_shape".split()
    ),
    "rdf": frozenset(
        "materialization_input_error "
        "triple_terms_unverified "
        "container_contract container_unverified container_conversion_rejected "
        "query_transform_unverified query_transform_invalid query_unverified "
        "reification_contract reification_unverified "
        "merge_conflict merge_unverified merge_resolution_required "
        "algebra_identity_unverified "
        "skolem_unverified "
        "store_unverified entailment_unverified "
        "graph_inventory_conflict graph_route_scope graph_route_collision "
        "migration_contracts_unverified migration_acceptance_unverified "
        "value_migration_rejected value_migration_collision "
        "value_migration_unverified list_migration_rejected "
        "list_migration_unverified "
        "patch_unverified patch_precondition_failed patch_removal_missing "
        "patch_postcondition_failed "
        "literal_conversion_unverified "
        "canonicalization_literal_scope_unverified "
        "canonicalization_graph_scope_unverified canonicalization_empty_graph_loss "
        "canonicalization_unavailable canonicalization_input_invalid "
        "export_empty_graph_exclusion export_empty_graph_loss "
        "export_graph_scope_unverified export_representation_unverified "
        "extraction_scope graph_scope_unverified merge_scope remap_collision "
        "remap_literal_loss remap_scope structure_changed".split()
    ),
    "shacl": frozenset(
        "remediation_unverified "
        "external_aggregate_empty external_nonconformance "
        "external_detail_unverified external_projection_incomplete "
        "external_scope_missing external_proof_unverified external_structure_invalid "
        "partition_scope_unverified partition_input_error "
        "rules_unverified rules_input_error "
        "target_unavailable upgrade_unverified upgrade_target_removed "
        "component_unverified skos_scope_untranslated skos_rule_untranslated "
        "analysis_metadata_unverified analysis_profile_unverified "
        "analysis_qualification_unverified analysis_recursion_unverified "
        "static_incompatibility "
        "profile_weakening profile_refinement_unverified "
        "shape_projection_unverified shape_edit_unverified "
        "profile_dependency_unverified profile_behavior_unverified "
        "profile_behavior_changed "
        "path_field_unverified "
        "model_shape_unverified "
        "dataset_scope_unverified incremental_unverified "
        "backend_warning execution_incomplete feature_unverified "
        "path_unverified finding_unverified "
        "function_unavailable function_malformed function_untrusted "
        "function_recursive capability_unavailable "
        "constraint_unavailable constraint_malformed constraint_not_evaluated "
        "coverage_minimum coverage_missing coverage_pair_missing coverage_incomplete "
        "shapes_invalid unavailable "
        "failure input_error exhausted "
        "implicit_target_unverified nonconformance sparql_unverified".split()
    ),
    "skos": frozenset(
        "labels_unverified label_consumer_unverified "
        "subset.scope_unverified subset.lifecycle_unverified "
        "xl_relation.endpoint_not_resource xl_relation.form_shape "
        "xl_relation.form_conflict xl_relation.role_conflict "
        "xl_relation.forbidden xl_relation.policy_mismatch "
        "policy.label_collision policy.label_collision_unverified "
        "publication.root_not_published publication.root_scope_unverified "
        "publication.unreachable_component publication.hierarchy_cycle "
        "publication.scope_unverified "
        "publication.dependencies_unverified publication.collection_member_excluded "
        "publication.requirements_unverified "
        "publication.required_root_missing publication.root_not_allowed "
        "publication.root_has_broader publication.broader_scope_unverified "
        "publication.reference_unverified "
        "policy.collection_sequence_mismatch policy.collection_size_mismatch "
        "policy.collection_duplicate_member "
        "collection_member_role_conflict label_role_conflict mapping_conflict "
        "mapping_target_kind mapping_target_unresolved "
        "policy.collection_member_shape policy.cross_scheme_hierarchy "
        "policy.definition_missing policy.deprecated_active "
        "policy.hierarchy_cycle policy.isolated_concept "
        "policy.label_representation policy.language_missing "
        "policy.mapping_target_missing policy.member_list_ambiguous "
        "policy.member_list_malformed policy.multiple_schemes "
        "policy.notation_collision policy.notation_missing "
        "policy.notation_shape policy.notation_scope_unavailable "
        "policy.deprecation_unverified policy.scheme_missing policy.xl_form_shape "
        "policy.xl_label_shape preferred_label_conflict "
        "preferred_label_language_unverified reference_shape_unverified "
        "related_hierarchy_conflict resource_role_conflict selection_unresolved "
        "xl_form_conflict xl_form_unresolved xl_role_conflict xl_role_overlap".split()
    ),
    "terms": frozenset(
        "slim_root_unverified slim_view_unverified slim_uncovered slim_invalid "
        "relation_projection_unverified relation_source_unverified "
        "relation_path_missing relation_path_invalid relation_candidates_invalid "
        "restriction_malformed restriction_unverified "
        "collection_member_invalid collection_member_unverified "
        "collection_root_unverified collection_containment_cycle "
        "collection_list_ambiguous collection_list_malformed "
        "navigation_anonymous".split()
    ),
    "upgrade": frozenset(
        "mapping_obligation_changed mapping_cardinality_changed "
        "mapping_impact_unverified mapping_impact_input_error mapping_relation_changed "
        "lifecycle_violation lifecycle_unverified lifecycle_input_error "
        "identifier_changed identifier_unverified identifier_input_error "
        "matrix_unverified "
        "value_inadmissible value_unverified value_scope_unverified "
        "plan_unverified plan_final_changed "
        "replacement_unverified replacement_kind_mismatch "
        "application_unverified application_declaration_changed "
        "application_value_changed "
        "fact_impact_unverified "
        "child_model_unavailable usage_unavailable deprecation_unverified "
        "corpus_baseline_missing corpus_candidate_missing "
        "corpus_comparison_unverified corpus_coverage_missing "
        "newly_deprecated contract_unverified anonymous_impact_unverified "
        "no_contracts contract_persistent contract_resolved contract_introduced".split()
    ),
    "vocabulary": frozenset(
        "allowed_scope_unverified allowed_invalid binding_invalid "
        "binding_trait_unverified binding_no_case binding_cases_unverified "
        "binding_ambiguous binding_record_invalid "
        "accession_invalid accession_unverified accession_input_error "
        "authoring_rejected authoring_unverified "
        "fact_role_unverified fact_attribution_unverified fact_graph_unverified "
        "coverage_gap coverage_source_missing declaration_conflict "
        "entity_kind_mismatch fact_changed reference_kind_mismatch reference_missing "
        "term_missing unknown_reserved_term binding_name_unresolved "
        "registry_alias_conflict registry_canonical_conflict "
        "identifier_template_unverified identifier_template_mismatch "
        "identifier_collision identifier_input_error".split()
    ),
}

# Meaning, corrective action and a minimal input example for each owned surface.
_FAMILIES: dict[str, tuple[str, str, str, str]] = {
    "analysis": (
        "explicit finite annotation population and selected hierarchy",
        "A required analytical input or result is unavailable within its scope.",
        "Inspect the supplied universe, annotation rows, propagation witnesses "
        "and actual method identity; retain missing terms and incomplete work "
        "rather than shrinking the population.",
        "resnik_similarity(population, left, right)",
    ),
    "projection": (
        "explicit graph-qualified RDF record projection",
        "A selected field or record disagrees with its read contract or lacks proof.",
        "Inspect the exact graph, subject, path, original terms and cardinality; "
        "choose raw retention for unsupported conversion and require full "
        "verification before invoking a trusted consumer factory.",
        "project_records(source, (ReadBinding('record', subject, fields),))",
    ),
    "shex": (
        "explicit supplied ShEx node-to-shape obligations",
        "A native ShEx obligation fails or exceeds its qualified capability.",
        "Install bijux-ontology[shex], supply local ShapeDecl ShExJ and imports, "
        "and inspect each exact binding and its native diagnostics.",
        "bijux-ontology shex data.ttl schema.json --schema-iri urn:schema "
        "--binding urn:record urn:shape --json",
    ),
    "quantity": (
        "explicit selected-record quantity contracts",
        "A quantity differs from its reference-unit contract "
        "or lacks exact conversion.",
        "Install bijux-ontology[quantities], supply explicit unit bindings and "
        "inspect the actual value, dimension and reference-unit bounds.",
        'prepare_quantities((UnitBinding("mm", "millimeter"),))',
    ),
    "temporal": (
        "explicit emitted-record calendar contracts",
        "A selected interval or chronology disagrees or lacks a determinate order.",
        "Inspect the actual field text, declared datatype, timezone and explicit "
        "clock; keep unsupported precision or calendar arithmetic unknown.",
        "check_temporal(record, intervals=(interval,))",
    ),
    "csvw": (
        "explicit CSVW-described table extraction",
        "A selected cell, declared key or required conversion rule is unqualified.",
        "Inspect the original table, row, cell and explicit metadata; supply "
        "required resources and retain unsupported conversion rules as unknown.",
        "extract_csvw(description, tables, base=metadata_url)",
    ),
    "jsonld": (
        "explicit independent JSON-LD document records",
        "A required record capability is unavailable or no records were supplied.",
        "Inspect the framed inputs, explicit bases, installed JSON-LD capability "
        "and per-record reports; retain the unchecked tail as incomplete.",
        "check_jsonld_records(records, record_limits=Limits())",
    ),
    "mapping": (
        "explicit SSSOM interchange and mapping policy",
        "A supplied mapping row or required consumer scope failed qualification.",
        "Inspect original rows, exact endpoint vocabulary scopes and explicit "
        "relation, role and cardinality policies; retain unsupported semantics.",
        "check_mappings(mapping_set, subject_vocabulary=subjects, "
        "object_vocabulary=objects, policy=policy)",
    ),
    "taxonomy": (
        "explicit named taxonomy publication",
        "A selected class role, hierarchy assertion or publication choice failed.",
        "Inspect exact source and hierarchy selections, class bindings, label "
        "choices and omission policies; rerun the required consumer checks.",
        "project_taxonomy(source, hierarchy, policy, hierarchy_basis='asserted')",
    ),
    "acquisition": (
        "explicit offline resource preparation",
        "A selected acquisition closure failed or could not be qualified.",
        "Inspect exact origins, redirect/media/size limits and missing resources; "
        "keep ordinary checks offline and retain the original project inputs.",
        "acquire_bundle(source, requests=requests, policy=policy, destination=archive)",
    ),
    "publication": (
        "explicit negotiated RDF representations",
        "An observed variant differs or a required representation is unqualified.",
        "Inspect the variant, effective URL, media/profile and required resources; "
        "retain graph projection and unavailable processing boundaries.",
        "check_representations(resource, policy=policy)",
    ),
    "schema": (
        "explicit serialized JSON output contracts",
        "A selected JSON assertion fails or an RDF obligation lacks translation.",
        "Inspect the generated schema, linked RDF mappings and each translation; "
        "validate actual serializer output separately from RDF equivalence.",
        "compile_output_schema(models, roots, schema_iri=resource).validate(output)",
    ),
    "profile": (
        "explicit offline validation profiles",
        "Selected profile resources or obligations conflict or lack qualification.",
        "Inspect required parents, exact resource identities and named shape "
        "definitions; retain every mandatory validation when resolving conflicts.",
        "compose_profile(domain, {base.iri: base})",
    ),
    "project": (
        "declared project jobs and required dependencies",
        "Selected work is invalid, unavailable or awaiting unverified prerequisites.",
        "Inspect original inputs, named prerequisite reports and limits before "
        "rerunning the required scopes.",
        '{"name": "output", "kind": "emission", "depends_on": ["models"]}',
    ),
    "documents": (
        "selected JSON-LD source representation",
        "A selected source occurrence violates its explicit form or lacks a binding.",
        "Inspect the original source paths and requested keys, shapes or embedding; "
        "retain the independent semantic report.",
        'FormRequirement(SourceSelector(()), required_keys=("@graph",))',
    ),
    "obo": (
        "selected OBO annotation records",
        "The selected native OBO annotation convention is not established.",
        "Inspect literal roles, exact source/property/target anchors and their "
        "base assertions in the original source scope; retain opaque qualifiers.",
        'read_obo_records(view, "https://example.org/term")',
    ),
    "annotations": (
        "selected annotation publication requirements",
        "Selected values violate an explicit shape/count, or exact annotation "
        "attachments and their correspondence remain unresolved.",
        "Inspect the entity, predicate, language mode and owned source scopes; "
        "select fallback explicitly and retain original attachment witnesses.",
        'AnnotationRequirement("urn:label", languages=(LanguageRequirement("en"),))',
    ),
    "schemaorg": (
        "explicit supplied Schema.org vocabulary expectations",
        "An actual selected value disagrees with guidance or application policy.",
        "Inspect the pinned release, original includes alternatives and selected "
        "sections; choose strict application and text fallback policy explicitly.",
        "check_schemaorg(data, view, subjects=subjects, predicates=predicates)",
    ),
    "sdrf": (
        "selected local SDRF ontology checks",
        "The maintained consumer reports a mismatch or local evidence is incomplete.",
        "Inspect original cells, effective schema obligations and qualified local "
        "ontology resources; incomplete catalogues cannot prove absence.",
        "check_sdrf(parsed_table, effective_schema, terminology)",
    ),
    "serialization": (
        "required representative serializer coverage",
        "Actual cases do not establish every selected alternative or scenario.",
        "Supply unambiguous cases for each required branch and retain failed samples.",
        'BranchCoverage("record", "value", 0)',
    ),
    "selector": (
        "explicit JSON-LD match coverage",
        "Selected parents lack required matches or actual bindings are ambiguous.",
        "Inspect each selected parent, concrete source path and effective graph; "
        "retain incomplete identity associations and check each parent independently.",
        'SourceSelector((Property("records"), ArrayItems(), '
        'Property("title", minimum=1)))',
    ),
    "contract": (
        "named required consumer obligations",
        "Required comparison or binding coverage is missing, or a selected "
        "check could not execute.",
        "Supply the required bindings and resources, run every obligation, "
        "and retain incomplete evidence instead of accepting a permissive exit.",
        'ContractObligation("mapping", check, bindings=count, minimum_bindings=1)',
    ),
    "adapter": (
        "application declarations",
        "An adapter could not prove the declared application mapping or emission.",
        "Inspect aliases and serializers; supply explicit declarations "
        "where supported.",
        'FieldDeclaration("label", "urn:label", (ValueDeclaration("literal"),))',
    ),
    "context": (
        "actual selected JSON-LD usages",
        "A selected occurrence differs from its expectation or cannot be "
        "associated safely.",
        "Inspect the reported input path, actual expansion, logical base "
        "and owned contexts.",
        '{"@context":{"label":"https://example.org/label"},"label":"Colour"}',
    ),
    "roles": (
        "selected application resource interpretations",
        "A required class, concept or instance role conflicts with its policy "
        "or lacks an established witness.",
        "Inspect the selected resource, actual type witnesses, explicit bridge "
        "and punning policy; full profile validation needs its own capability.",
        'ResourceRole("instance", types=("urn:application:Record",))',
    ),
    "data": (
        "supplied RDF data, named ontology contracts and query obligations",
        "The selected data condition disagrees or cannot be established.",
        "Inspect selected graphs, named contracts, actual types and query bindings, "
        "and the explicit "
        "consumer obligation.",
        "<https://example.org/item> a <https://example.org/Image> .",
    ),
    "emission": (
        "representative model output",
        "Actual selected output conflicts with, or cannot establish, its "
        "model contract.",
        "Check the selected output path and model alternatives; examples "
        "do not prove all outputs.",
        '{"@id":"https://example.org/item","@type":"https://example.org/Image"}',
    ),
    "execution": (
        "requested execution",
        "Execution did not complete; this is not evidence of ontology agreement.",
        "Retain completed findings, resolve the interruption and rerun "
        "the requested scope.",
        "bijux-ontology context sample.json expectations.json --timeout 30",
    ),
    "input": (
        "explicit inputs and scope",
        "A required input, identity or requested scope is missing, "
        "invalid or ambiguous.",
        "Correct the reported input location before interpreting semantic results.",
        '[{"path":["label"],"predicate":"https://example.org/label"}]',
    ),
    "literal": (
        "RDF literal and value checks",
        "A lexical, datatype or value comparison fails or cannot be "
        "established under the selected regime.",
        "Retain the original terms and inspect the selected datatype "
        "capability or comparison regime.",
        '"01"^^<http://www.w3.org/2001/XMLSchema#integer>',
    ),
    "models": (
        "explicit application model contracts",
        "A supported model/type/property contract conflicts or remains unverified.",
        "Inspect the named field and ontology endpoint constraints; do "
        "not infer a remapping.",
        'FieldDeclaration("image", "urn:image", (ValueDeclaration("iri"),))',
    ),
    "owl": (
        "selected OWL assertions or maintained profile membership",
        "Assertions or actual profile checking report a finding or required unknown.",
        "Inspect the selected graph, assertion premises, backend capability and loader "
        "qualification; profile membership does not establish consistency.",
        "check_assertion_witnesses(source)",
    ),
    "ontology": (
        "selected RDF/OWL encoding rules",
        "An explicit declaration or encoding violates, or exceeds, a "
        "supported structural rule.",
        "Inspect the reported term and selected rule; these checks are "
        "not general OWL reasoning.",
        "<https://example.org/Image> a <http://www.w3.org/2002/07/owl#Class> .",
    ),
    "rdf": (
        "explicit RDF structure and serialization",
        "Selected RDF identity, graph membership or representation cannot "
        "be preserved as requested.",
        "Choose an explicit graph scope and a faithful representation; "
        "inspect actual loss findings.",
        'source.graph("@default")  # select a dataset graph explicitly',
    ),
    "shacl": (
        "supplied SHACL shapes with inference disabled",
        "Supplied data fails a selected shape, or the requested "
        "validation remains incomplete.",
        "Inspect the focus node and shape; separate nonconformance from "
        "unavailable features.",
        "[] a <http://www.w3.org/ns/shacl#NodeShape> .",
    ),
    "skos": (
        "selected SKOS integrity and explicit application policies",
        "A selected SKOS assertion conflicts, or its required evidence is unavailable.",
        "Inspect the concept, label or scheme and distinguish SKOS "
        "integrity from optional policies.",
        "<https://example.org/concept> "
        '<http://www.w3.org/2004/02/skos/core#prefLabel> "Colour"@en-GB .',
    ),
    "terms": (
        "bounded explicit RDF navigation",
        "A requested relationship or collection cannot be navigated unambiguously.",
        "Inspect the selected terms and RDF list; do not treat syntactic "
        "traversal as entailment.",
        "<https://example.org/list> "
        "<http://www.w3.org/1999/02/22-rdf-syntax-ns#rest> "
        "<http://www.w3.org/1999/02/22-rdf-syntax-ns#nil> .",
    ),
    "upgrade": (
        "selected application usage and upgrade impact",
        "Selected before/after facts or application contract outcomes differ.",
        "Supply the referenced model or an attributable observed usage; "
        "do not infer domain compatibility from a structural diff.",
        'ModelDeclaration("record", ("https://example.org/Record",))',
    ),
    "vocabulary": (
        "explicit term inventory and selected coverage",
        "A term declaration, kind, reference or requested coverage "
        "differs from supplied RDF.",
        "Check exact IRIs, declaration kinds, source roles and the chosen "
        "coverage scope.",
        'TermDeclaration("https://example.org/Image")',
    ),
}

_OVERRIDES: dict[str, tuple[str, str, str]] = {
    "adapter.fhir_backend_unavailable": (
        "The maintained FHIR R5 model is unavailable.",
        "Install bijux-ontology[fhir] and retain the original resource and bindings.",
        "read_code_system(content, bindings=bindings)",
    ),
    "rdf.literal_conversion_unverified": (
        "The selected backend reconstructs literal spelling outside exact scope.",
        "Retain exact source terms and use a supported query or explicit conversion.",
        'query_rdf(source, "SELECT ?value WHERE {?s ?p ?value}", scope=scope)',
    ),
    "context.probe_unverified": (
        "A declared branch lacks a supported finite JSON representative.",
        "Supply its explicit input shape or retain the unverified branch scope.",
        'ContextProbe(model, samples=(ProbeSample("field", 0, raw_json),))',
    ),
    "data.query_violation": (
        "A consumer SELECT rule returned an actual violating RDF binding.",
        "Inspect the exact bound terms and local query; correct data or the rule.",
        'QueryRule("missing-label", sparql, QueryScope("default"))',
    ),
    "data.query_answer_mismatch": (
        "A completed asserted ASK answer differs from its declared expectation.",
        "Inspect the selected graph and expected answer; this is no OWL proof.",
        'QueryRule("hierarchy", sparql, QueryScope("default"), expected=True)',
    ),
    "data.query_empty_scope": (
        "A required query selection contains no RDF statements.",
        "Supply the intended data scope or explicitly exclude nonempty coverage.",
        "run_query_rules(source, rules, allow_empty=True)",
    ),
    "rdf.canonicalization_empty_graph_loss": (
        "Canonical N-Quads cannot retain the selected empty named-graph inventory.",
        "Keep the dataset, or explicitly exclude empty graphs from the quad claim.",
        "canonicalize_rdf(source, ignore_empty_graphs=True)",
    ),
    "rdf.canonicalization_unavailable": (
        "The requested canonicalization backend or execution bound is unavailable.",
        "Use a supported runtime or platform; retain any required memory bound.",
        "canonicalize_rdf(source, memory_bytes=required_address_space)",
    ),
    "emission.source_field_undeclared": (
        "A selected source key has no declaration or proven coverage permission.",
        "Declare the actual field or its alias; allow only intentional extensions.",
        "ModelDeclaration(..., coverage=EmissionCoverage(...))",
    ),
    "emission.predicate_undeclared": (
        "A selected directed RDF predicate has no application field declaration.",
        "Declare the predicate or explicitly allow its direction as an extension.",
        "EmissionCoverage(reverse_extensions=(incoming_predicate,))",
    ),
    "emission.extension_allowed": (
        "An explicit extension is permitted without a declared value contract.",
        "Retain this visible scope decision; declare values for stronger checks.",
        "EmissionCoverage(extensions=(extension_predicate,))",
    ),
    "shacl.function_untrusted": (
        "Selected function definitions have not been explicitly trusted.",
        "Review the supplied catalog and set trusted=True only for approved bodies.",
        'ShaclFunctionCatalog(graph, selected=["https://example.org/double"], '
        "trusted=True)",
    ),
    "shacl.function_recursive": (
        "The selected function dependency closure contains unsupported recursion.",
        "Supply an acyclic supported catalog; do not bypass dependency checking.",
        "double(x) calls multiply(x, 2), with no dependency back to double.",
    ),
    "vocabulary.identifier_template_unverified": (
        "The template is outside the explicitly supported inverse profile.",
        "Use typed slots with reserved literal separators or retain uncertainty.",
        'IdentifierTemplate("https://example.org/{collection}/{record}", keys)',
    ),
    "vocabulary.identifier_template_mismatch": (
        "The original IRI is outside the template's exact typed expansion image.",
        "Check the original spelling and declared keys without normalizing identity.",
        'profile.recognize("https://example.org/images/item")',
    ),
    "vocabulary.identifier_collision": (
        "Multiple input records construct the same exact IRI.",
        "Choose an injective template or explicitly deduplicate the caller inputs.",
        "profile.construct_many(records)",
    ),
    "vocabulary.identifier_input_error": (
        "A supplied identifier or typed template binding is invalid.",
        "Supply the declared string/integer keys and preserve the original IRI.",
        'profile.construct({"collection": "images", "record": 1})',
    ),
    "vocabulary.registry_alias_conflict": (
        "A contributed prefix identifies different exact namespaces.",
        "Choose distinct permitted aliases; canonical spelling cannot remap an alias.",
        "NamespaceContribution(namespace, canonical_prefix, owner, aliases)",
    ),
    "vocabulary.registry_canonical_conflict": (
        "Owners prefer different canonical prefixes for the same exact namespace.",
        "Supply one explicit canonical choice from the contributed permitted aliases.",
        'merge_identifier_registry(items, canonical_prefixes={namespace: "ex"})',
    ),
    "vocabulary.term_missing": (
        "The exact declared term is absent from the selected vocabulary evidence.",
        "Correct the IRI or source selection; add a declaration only when "
        "it belongs to that vocabulary.",
        "<https://example.org/Image> a <http://www.w3.org/2002/07/owl#Class> .",
    ),
    "vocabulary.coverage_gap": (
        "A term in the requested ontology coverage has no application inventory entry.",
        "Add the intentional term to the inventory, or explicitly narrow "
        "the coverage scope.",
        'TermDeclaration("https://example.org/Image")',
    ),
    "context.mapping_mismatch": (
        "Actual JSON-LD expansion selects a different predicate from the expectation.",
        "Correct the context mapping or expectation; preserve the "
        "intended exact predicate IRI.",
        '{"@context":{"label":{"@id":"https://example.org/label"}}}',
    ),
    "context.property_dropped": (
        "The real JSON-LD processor omitted an unmapped or nonabsolute property.",
        "Supply its intended context mapping; exclude it only when "
        "omission is deliberate.",
        '{"@context":{"label":"https://example.org/label"},"label":"Colour"}',
    ),
    "upgrade.contract_introduced": (
        "A complete prior check did not have this newly observed contract finding.",
        "Review the affected location and original before/after evidence; "
        "choose the intended vocabulary or application correction.",
        "bijux-ontology upgrade before.ttl after.ttl --declarations models.json --json",
    ),
    "upgrade.contract_persistent": (
        "The same contract finding was already present before the upgrade.",
        "Keep the existing issue visible; distinguish it from a newly "
        "demonstrated upgrade break before choosing an exact exception.",
        "assessment.contracts  # inspect status, before and after",
    ),
    "upgrade.contract_resolved": (
        "A previous finding is absent after a complete check of the new version.",
        "Review the actual corrected contract; this does not establish "
        "compatibility for unobserved data or unsupported OWL expressions.",
        "assessment.contracts  # status='resolved' retains the old finding",
    ),
    "shacl.shapes_invalid": (
        "The supplied shapes could not establish a valid supported SHACL scope.",
        "Repair the shapes document; this is distinct from data nonconformance.",
        "validate_shapes(data, corrected_shapes)",
    ),
    "shacl.dataset_scope_unverified": (
        "Record-local validation cannot establish conformance of a wider dataset.",
        "Retain the local result and validate the complete dataset for global claims.",
        'session.validate(complete_data, scope="dataset")',
    ),
    "shacl.unavailable": (
        "The optional SHACL capability or required resource is unavailable.",
        "Install the prepared wheel with [shacl] and provide explicit inputs.",
        "uv pip install 'path/to/bijux_ontology.whl[shacl]'",
    ),
    "shacl.failure": (
        "The SHACL backend failed to complete the selected validation.",
        "Inspect the backend diagnostic and supported features; do not "
        "interpret execution failure as data conformance.",
        "result.report.raise_for_findings()",
    ),
    "shacl.input_error": (
        "A required SHACL input or option could not be established.",
        "Check the distinct data, shapes and ontology arguments and graph selection.",
        "validate_shapes(data, shapes, ontology=ontology)",
    ),
    "shacl.exhausted": (
        "SHACL execution exceeded a selected work or time bound.",
        "Reduce the workload or justify a larger bound; retain completed evidence.",
        "validate_shapes(data, shapes, timeout=30)",
    ),
    "execution.limit_exceeded": (
        "A configured size, work, state or resource allowance stopped the operation.",
        "Reduce the selected scope or justify a larger bound, then rerun; "
        "incomplete means unverified.",
        "bijux-ontology context sample.json expectations.json --max-work 2000000",
    ),
    "execution.cancelled": (
        "The requested operation was cancelled before completion.",
        "Retain completed findings and rerun when ready; cancellation "
        "does not establish agreement.",
        "bijux-ontology context sample.json expectations.json",
    ),
    "execution.deadline": (
        "The owned execution worker exceeded its wall-clock deadline.",
        "Investigate the workload before choosing a larger timeout; "
        "retained findings remain incomplete.",
        "bijux-ontology context sample.json expectations.json --timeout 30",
    ),
    "literal.lexical_invalid": (
        "The lexical text is invalid for the explicitly selected supported datatype.",
        "Correct the lexical text or datatype; do not silently "
        "canonicalise supplied content.",
        '"2024-02-29"^^<http://www.w3.org/2001/XMLSchema#date>',
    ),
    "rdf.export_empty_graph_loss": (
        "The selected serializer cannot retain an existing empty named graph.",
        "Keep the dataset representation, or explicitly exclude empty "
        "graph preservation if appropriate.",
        'export_rdf(source, format="trig", preserve_graphs=False)',
    ),
}


def explain_code(code: str) -> DiagnosticExplanation | None:
    """Explain a registered diagnostic without changing its outcome.

    Contextual model diagnostics retain the underlying registered meaning.
    Unknown codes do not acquire fabricated explanations.
    """
    if not isinstance(cast(object, code), str):
        return None
    family, separator, suffix = code.partition(".")
    if not separator:
        return None
    registered = suffix.removeprefix("contextual_") if family == "models" else suffix
    if registered not in _CODES.get(family, ()):
        return None
    scope, meaning, action, example = _FAMILIES[family]
    if registered.endswith("_unverified"):
        meaning = (
            "The requested condition remains unverified within this check's "
            "supported scope."
        )
        action = (
            "Inspect missing evidence or unsupported constructs; do not "
            "interpret this as a proven conflict. " + action
        )
    meaning, action, example = _OVERRIDES.get(code, (meaning, action, example))
    return DiagnosticExplanation(code, scope, meaning, action, example)
