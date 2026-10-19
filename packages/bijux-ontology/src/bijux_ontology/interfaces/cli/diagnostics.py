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
    "selector": frozenset(
        "parent_count_mismatch count_mismatch "
        "structure_mismatch binding_unverified".split()
    ),
    "adapter": frozenset(
        "alias_unverified class_mapping_unverified computed_output_unverified "
        "emission_unverified excluded_emission_conflict mapping_conflict "
        "mapping_unverified field_contract_conflict alias_contract_conflict "
        "output_unverified predicate_contract_conflict".split()
    ),
    "context": frozenset(
        "change_unverified before_input_error after_input_error "
        "container_mismatch datatype_mismatch "
        "flatten_correspondence_unverified flatten_input_error "
        "direction_mismatch identifier_kind_mismatch identifier_mismatch "
        "input_error invalid_path language_mismatch mapping_mismatch "
        "omission_invalid omission_unverified required_scope_excluded "
        "order_mismatch order_unverified property_dropped property_omitted "
        "rdf_direction_loss rdf_empty_graph_loss rdf_identifier_loss "
        "rdf_interoperability_unverified rdf_language_invalid rdf_literal_loss "
        "rdf_output_unverified reverse_mismatch serialization_unverified "
        "term_dropped types_mismatch unsupported_check usage_dropped "
        "usage_meaning_changed value_kind_mismatch value_unverified".split()
    ),
    "data": frozenset("type_unverified".split()),
    "contract": frozenset("coverage_missing unavailable".split()),
    "emission": frozenset(
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
        "datatype_unverified lexical_invalid lexical_unverified".split()
    ),
    "models": frozenset(
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
    "ontology": frozenset(
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
        "export_empty_graph_exclusion export_empty_graph_loss "
        "export_graph_scope_unverified export_representation_unverified "
        "extraction_scope graph_scope_unverified merge_scope remap_collision "
        "remap_literal_loss remap_scope structure_changed".split()
    ),
    "shacl": frozenset(
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
        "collection_list_ambiguous collection_list_malformed "
        "navigation_anonymous".split()
    ),
    "upgrade": frozenset(
        "child_model_unavailable usage_unavailable deprecation_unverified "
        "corpus_baseline_missing corpus_candidate_missing "
        "corpus_comparison_unverified corpus_coverage_missing "
        "newly_deprecated contract_unverified anonymous_impact_unverified "
        "no_contracts contract_persistent contract_resolved contract_introduced".split()
    ),
    "vocabulary": frozenset(
        "coverage_gap coverage_source_missing declaration_conflict "
        "entity_kind_mismatch fact_changed reference_kind_mismatch reference_missing "
        "term_missing unknown_reserved_term".split()
    ),
}

# Meaning, corrective action and a minimal input example for each owned surface.
_FAMILIES: dict[str, tuple[str, str, str, str]] = {
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
    "data": (
        "supplied RDF data",
        "The selected data condition cannot be established by this supported check.",
        "Supply the missing type evidence or use an explicit supported "
        "data constraint.",
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
        "RDF literal lexical and datatype checks",
        "A lexical value violates, or is not supported by, the selected "
        "datatype check.",
        "Keep lexical text and datatype distinct; correct the value or "
        "declare a supported type.",
        '"01"^^<http://www.w3.org/2001/XMLSchema#integer>',
    ),
    "models": (
        "explicit application model contracts",
        "A supported model/type/property contract conflicts or remains unverified.",
        "Inspect the named field and ontology endpoint constraints; do "
        "not infer a remapping.",
        'FieldDeclaration("image", "urn:image", (ValueDeclaration("iri"),))',
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
