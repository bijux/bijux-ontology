# Consumer integration

Applications supply their own vocabulary snapshots, declarations and representative output. Use `TermDeclaration`, `ModelDeclaration`, `FieldDeclaration` and `ValueDeclaration` directly, or extract them through the optional Pydantic/LinkML adapters. The library does not discover application source or infer biological meaning. Runnable examples include ordinary dataclasses and independent catalogue workflows.

Select local, import and lookup scopes deliberately. Use coverage for owned namespaces and membership checks for external terms; lookup documents must not conceal missing local declarations. A required constructor field does not prove RDF emission: declare that guarantee explicitly and check representative serialised output with `check_emission`.

Opt into closed output coverage with `ModelDeclaration(..., coverage=EmissionCoverage(metadata=("@context", "@id", "@type")))`. Additional source keys or directed predicates require declarations or explicit `extensions`/`reverse_extensions`; permitted extensions stay visible as informational findings. `aliases=(("headline", "title"),)` checks alias values, children and combined counts without relaxing required primary-key presence. Native/entity checks cover selected incoming and outgoing predicates while source-key coverage remains unverified. Non-RDF metadata aliases without an established processor witness also remain unverified.

For finite RDFLib namespace classes, `namespace_inventory({"app.BIA": BIA})`
from `bijux_ontology.interfaces.adapters.namespaces` retains qualified declaration
names and exact IRIs. Call `inventory.check(vocabulary,
owned_namespaces=("http://bia/",))` for local membership; add
`require_coverage=True` and `coverage_terms=selected_owned_iris` for reverse coverage.
Ownership and accepted RDF kinds are explicit caller contracts.

Compose explicit namespaces with `merge_identifier_registry(contributions)`,
where each `NamespaceContribution(namespace, canonical_prefix, owner, aliases)`
retains ownership. Conflicting aliases never produce a partial registry;
competing canonical spellings require an explicit `canonical_prefixes` choice.
The immutable registry separates `.iri(value)` from `.expand_curie(value)` and
uses the longest namespace in `.compact_iri(value)` without guessing intent.

Install `bijux-ontology[templates]` for `IdentifierTemplate(template, keys)` with
explicit `TemplateKey` string/integer bindings. `.construct()` uses maintained
URI escaping; `.construct_many()` rejects exact IRI collisions atomically.
Forward profiles support one `{key}` or `{+key}` per key. `.recognize(iri)` retains
the original spelling and checks only unmodified slots separated by reserved
literals, strict single decoding and exact re-expansion. Ambiguous or unsupported
inverse profiles return no guessed record.

Pass `compression="gzip"` with an explicit RDF `format` for paths, bytes or
binary streams in `RdfSource` or `prepare_source`. `max_compressed_bytes` bounds
input bytes; `max_bytes` bounds expanded bytes, and both consume the shared byte
budget. All gzip members and trailers are checked before RDF is published.
Caller streams remain open; individual compressed documents are not archives.

Dataset-backed `RdfSource` resources declare `graph_name=graph_iri`,
`graph_name="@default"`, or `union=True`. Imports, lookup resources and prepared
SHACL inputs retain that selection. `prepared.dataset()` still exports every
original graph; conflicting graph overrides fail explicitly. Required import
aliases must agree on selected graph ownership and RDF content.

Exact source storage retains distinct `xsd:token` and `xsd:normalizedString`
spellings through comparison, worker transport, canonicalization and RDF export.
Queries can project those exact source terms. RDFLib query constants, casts and
`STRDT` constructions that cannot retain their spelling return
`rdf.literal_conversion_unverified`; SHACL preflight retains the same boundary.
Explicit `preserve_literals=False` permits normalization at source preparation.

Install `bijux-ontology[xsd]` for finite XSD 1.1 lexical checks of durations,
binary forms, partial Gregorian dates and ASCII Name/NCName/NMTOKEN values.
Raw spelling is checked without XML whitespace preprocessing or term replacement.
QName, document-dependent types and non-ASCII names remain unverified. Core
`xsd:anyURI` checks XML character validity, including relative strings and spaces;
it does not validate or resolve an RDF IRI. Custom datatype relations stay unknown.

Use `query_rdf(source, sparql, scope=QueryScope("dataset"))` for local ASK/SELECT. Choose `default`, `named` or `union` to project a graph instead. Results retain typed terms and unbound cells; a false ASK is a completed answer, while capped SELECT results stay incomplete. SERVICE and FROM retrieval are prohibited. Capture, worker execution and reply validation have separate finite work budgets and one deadline; opaque engine search is bounded by the owned worker deadline.

`run_query_rules(source, load_query_rules(Path("rules.json")))` checks named
consumer obligations over asserted RDF. The JSON array contains each rule's
`name`, either `query` text or a relative `query_file`, and an explicit
`scope: {"kind": "default"}` (or another `QueryScope` choice). A boolean
`expected` requires that ASK answer; absent/null `expected` requires zero
violating SELECT rows. Exact SELECT answer sets use
`expected: {"variables": ["term"], "rows": [[{"kind": "iri", "value": "urn:item"}]]}`.
Returned results retain named raw answers alongside diagnostics with failing RDF
bindings. Blank labels cannot identify expected answers across queries.
Required empty suites/data selections, query errors, answer disagreements and
exhaustion remain distinct. `allow_empty=True` explicitly excludes only the
nonempty-data requirement. Project jobs use `kind: "query-rules"`, a named RDF
`source` and a `rules` filename; every rule retains its own scope.

Use `check_serializer_corpus(cases, models, vocabulary=vocabulary,
required_branches=(BranchCoverage("record", "value", 0),))` to require actual
sample coverage. Each `SerializerCase` calls its serializer once with owned
options and retains checked output. `ScenarioCoverage` selects declared emission
scenarios. Overlapping alternatives need an actual unambiguous discriminator;
coverage never hides a failing sample or proves universal output correctness.

`probe_context(context, [ContextProbe(model, type_values=("Record",))])`
qualifies generated representatives for explicitly declared field branches
through maintained JSON-LD processing. Compact `type_values` select an actual
type-scoped context and must expand to the model's declared types. Scalars and
list/set values use neutral representatives; object and custom datatype branches
can supply `ProbeSample("field", branch_index, raw_json)`.
Each report scope records its branch, sample origin and source shape. Unsupported
or unrepresented branches stay unverified. Agreement covers these finite inputs,
including their predicate, direction, value and container checks; it does not
prove arbitrary serializer output or biological correctness. Map-container
qualification and structural `@id`/`@type` field aliases remain unverified.

`check_skos(vocabulary, profile="mapping-integrity")` composes resource roles,
relation clashes, exact-match conflicts and incident mapping endpoint checks.
It does not require external target declarations. `profile="core-integrity"`
and the omitted profile retain the four default integrity families; explicit
`rules` add selected conditions to a profile. For opt-in `acyclic`, `isolated`
and `same-scheme-hierarchy` policies, `hierarchy_basis="asserted"` uses authored
hierarchy edges; the default `effective` also includes supported mapping
implications. Semantic integrity retains broadMatch's hierarchy implications
under both policy choices. Reports list expanded conditions and the chosen basis.
The CLI exposes `--profile` and `--hierarchy-basis`; SKOS project jobs accept
`profile` and `hierarchy_basis`.

`check_top_concepts(view, TopConceptPolicy(scheme_iri, required=(root_iri,)))`
checks an opt-in published-root inventory using either SKOS top-concept inverse.
An optional `allowed` inventory and `broader="forbid-within-scheme"` qualify
authored broader/narrower assertions; parents with unresolved scheme membership
remain unverified. These consumer rules leave default SKOS integrity unchanged.

`check_scheme_coverage(view, scheme_iri)` returns actual root-to-concept witnesses
and unreachable components over authored hierarchy links. Choose traversal
`direction`, `cross_scheme` bridges and `cycles` explicitly when defaults differ
from the publication policy. Coverage concerns selected scheme concepts; it does
not establish logical validity. Incomplete traversal retains proved paths and
returns `unreachable=None`.

`check_label_collisions(view, schemes=(scheme_iri,))` applies optional uniqueness
to selected concept labels, separately for each scheme and language. Select
plain SKOS label `roles`, `comparison` (`exact`, `casefold`, `nfc`, `nfc-casefold`)
and explicit `LabelHomonym` owner sets. Comparison keys never alter RDF literals;
legal homonyms remain legal under default integrity checks.

`check_xl_relations(view, policies)` checks explicitly declared SKOS-XL relation
predicates and flat subrelation inventories. `XLRelationPolicy` selects endpoint
languages, exact literal-form comparisons, asserted/either orientation or
forbidden relations. Equal forms preserve distinct label resources; missing
forms remain unverified. Subrelations inherit no implicit reverse assertion.

`negotiate_annotations(view, iri, predicates=(label_iri,), ranges=("fr-CA",))`
uses [RFC 4647](https://www.rfc-editor.org/rfc/rfc4647.html) lookup, preserving
tied annotations and their original terms. Select basic/extended filtering,
source-role priority and explicit untagged/other fallback when required.
`matches_language("en-GB", "en")` is filtering only; it supplies no lookup
fallback or publication-coverage guarantee.

`check_annotation_coverage(view, requirements, kinds=(class_iri,))` applies
`AnnotationRequirement` shape/count rules to every selected entity. Per-language
`LanguageRequirement("en")` uses exact tags; `mode="basic"` can count `en-GB`.
Fallback is explicit, requires an empty completed primary inventory and never
fills a nonempty deficit. Original assertions and chosen fallback witnesses stay
visible; incomplete inventories cannot establish missing minima.

Use `project_restrictions(view, class_iri, inherited=True)` for finite asserted
OWL restriction records. Some/all/value quantifiers remain distinct; supported
intersections and named superclass paths retain original source/list witnesses.
`existential_path(view, source, target, properties=(property_iri,))` follows only
selected `someValuesFrom` projections. It creates no RDF relation triples and
claims no transitive-property entailment. Unsupported alternatives remain in the
report even when an independent path is found; incomplete coverage cannot prove
unreachability. Expression components stay in their attachment's source scope.

Use `read_obo_records(view, term_iri)` for explicitly selected OBO-in-OWL
definitions and exact/broad/narrow/related synonyms. Records retain original
literal terms and separate axiom attachments, including cross-references,
synonym types and opaque qualifiers. Attachments require exact anchors and a
matching assertion in the same source scope; malformed or ambiguous structures
remain visible. Attachment `complete` describes its outgoing structure, while
`status` and `assertions` establish the association. Ordinary RDF and SKOS
annotations remain outside this selected convention.

Use `terms_page`, `statements_page` and `search_page` to browse an immutable
`VocabularyView` with `page_size` and its returned `next_cursor`. A page report
covers that page; only `enumeration_complete` certifies the whole selection.
Cursors bind to the actual snapshot, filters and ordering, and support resumed
reads after cancellation or exhaustion. They are opaque in-process objects.
Term and lexical ordering are prepared lazily and shared by compatible views;
ordinary inspection and statement paging reuse the existing index.

Use `select_concepts(view, scheme=iri, closed=True)` or `collection=iri` to
bind an explicit controlled set with `subset.bind(field)`. Scheme selections
accept interior `roots` and authored hierarchy policies. Choose deprecation
`allow`, `exclude` (the default), or `require-active`; the last requires explicit
nondeprecated metadata. Known members and definite exclusions retain their
witnesses. Open or incomplete selections cannot prove outsider exclusion or
complete coverage, and their original reports remain required by output checks.
Binding preserves other predicates and rejects an existing enumeration.

Use `check_collection_sequence(inspect_vocabulary(source), collection_iri,
expected=(first_iri, second_iri), unique_members=True)` for authored list order.
`minimum_size`/`maximum_size` count every position; duplicates remain visible.
`collection_members(view, iri, ordered=True)` exposes each immutable member's
zero-based `position`; direct `skos:member` assertions have no position. Missing
or ambiguous lists remain unverified, and proved violations survive bad tails.

`expand_collection(view, collection_iri, nested="expand")` retains concept
occurrences and their complete membership paths, reusing shared child decoding.
Its `concepts` set and duplicate-preserving `sequence` serve distinct purposes:
a sequence is available only for complete, fully ordered membership. Mixed or
unordered membership supplies no total order. `nested="retain"` keeps child
collections atomic; cycles, unresolved roles and exhausted bounds remain visible.

Use `check_record_keys(records, KeyContract(name, components), complete=True)`
for application uniqueness over an explicitly supplied corpus. `KeyRecord` binds
each identifier and subject to an owned source and graph; `KeyComponent` accepts
finite predicate, sequence and inverse RDFLib paths. Absence and multiplicity
are explicit policies. Default equality is exact RDF identity; string business
normalization and named semantic regimes require an explicit choice. Partial
corpora and lossy literal preparation remain unverified; collisions retain their
original values and source associations without implying OWL identity.
`project_record_key` exports QueryRules and SHACL-SPARQL for supported exact
singleton keys and named records in one graph. Keep its projection report
alongside backend results, select the same graph, and use `trusted_sparql=True`
for SHACL validation. Unsupported policies never acquire a verified projection.

Core RDF and vocabulary operations do not require PyLD. Install `bijux-ontology[jsonld]` for JSON-LD processing. Hosts pinned to another PyLD version can use the core without changing their dependency requirements; JSON-LD operations require a compatible runtime.

Use `SubjectIdentity(expected_iri=record_iri, allow_blank=False)` on a model
or individual `ModelUsage` to check effective subjects after base resolution.
`ModelUsage(model_id, reference=record_iri, graph=None)` joins local descriptions
for that named entity in the default graph; pass an exact graph IRI for a named
graph. Ordinary path bindings keep per-source-object required fields; `entity=True`
explicitly joins their facts. Unsupplied references remain unknown unless
`closed=True` requires a local description. Identity checks perform no OWL
equality, inequality or classification.

Use `ContextExpectation(path, lists=(ExpectedList((first, second)),))` to
check complete RDF lists. Outer list occurrences are unordered; each list keeps
its own member order and duplicates. Explicit expanded literal objects and
absolute `@id` references express exact RDF terms. Anonymous member identity
stays unverified. JSON expectation files accept `lists: [[first, second]]`;
the existing `ordered_values` contract still describes one complete list.

RDF direction encodings use the explicit `direction="i18n-datatype"` or
`direction="compound-literal"` conversion policy. If maintained conversion cannot
distinguish directional siblings on the same RDF edge, preservation stays
unverified and `prepare_jsonld` returns no owned source. Separate list positions,
subjects, graphs and predicates retain their distinct scopes.

For JSON-LD, pass original usages and expectations to `check_context`. Reuse explicit resources and logical bases across parsing, serialisation and `prepare_jsonld` conversion. Default loaders do not fetch missing resources. Consumer-owned loaders or validation services have their own network behaviour; configuring this library does not make an entire application offline.

Use `canonicalize_rdf(source)` for bounded RDFC-1.0 canonical N-Quads over the selected RDF 1.1 quads. The result records its algorithm, hash choice, literal fidelity and graph scope. Empty or unknown graph inventory prevents publication unless explicitly excluded. Canonical blank labels do not establish identity across changed graphs, raw-resource integrity or OWL equivalence. An optional OS address-space bound fails explicitly where unavailable.

Use `export_jsonld(source, context=context, base=logical_base)` to compact selected RDF through PyLD and verify its re-expanded RDF before writing with `write_export`. An optional standard `frame` requests embedding or reverse fields; losing selected triples blocks export. Context loading is offline by default. Source JSON shape and index metadata are outside this RDF preservation claim. Empty named graphs require an explicit exclusion or prevent export.

Use `flatten_document(document, base=logical_base)` for checked flat JSON-LD.
Its report checks RDF preservation, including named empty graphs; its owned
`document` and graph-qualified `nodes` support `node = result.bind(record_iri,
graph=graph_iri)` without array-order assumptions. Check `node.value` with
`check_emission(node.value, models, [ModelUsage(model_id)])`.

With `correspondence=True`, inspect `correspondence_report` separately. Named
descriptions expose `expanded_paths` into expanded input, not compact source
pointers. Blank-node or anonymous-graph correspondence remains unverified without
an identity witness. RDF preservation does not promise original JSON shape.

Keep findings and unsupported comparisons visible. Apply narrow code/location exceptions through `AcceptancePolicy`; they do not change verification truth. Retain application validation, reference checks and any existing reasoning gate. Supplied-shapes validation is an optional additional operation, not general OWL consistency checking.

Author explicit application shapes with `ShaclNodeShape`, `ShaclPropertyShape`
and `compile_shapes` from `bijux_ontology.shacl`. Constraints include value and
count rules, cross-property comparisons, qualified counts and logical shape
references. Property paths accept RDFLib paths such as `~ex.owner / ex.name`;
construction captures their intent before later caller mutation. References must
resolve to supplied definitions; recursion remains an RDF link. Compilation
returns an owned `PreparedSource` and raises on unsupported or exhausted input
without returning partial shapes. It infers no constraints from ontology axioms.
Use `validate_shapes(data, compiled, preflight=True)` to run maintained
meta-SHACL and data validation; compilation alone does not establish conformance.

Use `compile_model_shapes(models, bindings)` with explicit `ModelShapeBinding`
targets to translate supported output declarations into the same typed shapes.
Asserted model/value types use exact `rdf:type` values; subclass edges do not
substitute for required assertions. Reverse fields, alternatives, explicit value
counts and closed enumerations retain their meaning. Keep the returned `report`
alongside its optional `source`: source permissions, conditional groups, list and
role rules, and native lexical checks beyond the backend's supported subset
remain required unknowns. Finite exact enumerations can qualify lexical domains;
an unbounded datatype shape alone cannot promise native lexical agreement.
An explicit target without a positive assertion requirement still needs native
subject-occurrence evidence; naming a SHACL focus node does not supply a record.

SHACL findings retain report-local identities, nested details and finite typed
property paths in `result.findings` and each generic issue’s `evidence`.
Supply `ShaclSource(prepared_usages, paths, source=...)` through `source_usages=`
to correlate actual JSON-LD selections. Multiple or unsupported locations remain
explicit; the full RDF report stays available through `report_graph()`.
`validate_shapes(..., focus_nodes=(rdf_term,), use_shapes=(authored_shape,),
mode="pairs")` accepts literal focus values and owned blank focus/shape terms.
Obtain blank terms from the corresponding `PreparedSource.graph()` export;
equal labels from other snapshots are rejected. Transport keeps independent
role scopes separate and preserves blank node kinds. Literal and blank focus
selection requires explicit pair mode; findings retain report-local identities.

For repeated datasets, use `with ShaclSession(shapes, ontology=ontology) as session:` from `bijux_ontology.shacl` and call `session.validate(data)`. The session owns its fixed resources and reuses one isolated worker; construct a new session to replace them. `validate_batch(datasets)` retains per-dataset results and explicit pending coverage. Deadlines include worker queueing and startup; cancellation, worker failure and close release owned processes. `scope="records"` preserves local conformance while leaving wider dataset conformance unverified. Sessions currently require POSIX process groups.

For supplied SHACL shapes, `ShaclCoverageContract(minimum_focus=1,
focus_nodes=record_iris, pairs=shape_focus_pairs)` from `bijux_ontology.shacl`
requires completed root target evaluations when passed as `coverage=` to
`validate_shapes`. Inspect `result.coverage` separately from `conforms` and
`conformance_report`; a targetless shape can conform while required coverage fails.
Nested conditional constraint execution is not inferred from root coverage.
Use `required_constraints=(ShaclConstraintRequirement(shape_iri, parameter_iri),)`
for explicit supported parameter/component bindings and inspect `result.constraints`
for execution coverage. A required nested-only constraint remains unverified.
Trusted SPARQL queries with unavailable function calls are rejected before validation;
`trusted_sparql=True` alone does not enable function implementations.

Use `check_contract` for required consumer acceptance. Name each `ContractObligation`,
provide its check callback and set `minimum_checked` or explicit
`bindings`/`minimum_bindings` for extraction coverage. For example:

```python
from bijux_ontology import ContractObligation, check_contract

result = check_contract([
    ContractObligation("mapping", lambda: extraction.report,
                       bindings=mapped_field_count, minimum_bindings=1),
    ContractObligation("emission", check_actual_output),
    ContractObligation("optional-profile", None, required=False),
])
result.require_verified()
```

The result retains raw reports and strict policy acceptance separately. Only an
absent optional check is non-applicable; an executed check retains its failures.
An explicit approval uses `IssueSelection.for_issue` on the named aggregate issue,
so another finding at the same broad location remains rejected. Approval can make
`result.accepted` true while `require_verified()` still rejects unverified truth.

Evaluate a fixed wheel in a dependency-consistent environment on the consumer’s supported interpreter. Check installed import origin, run real integration cases and include a deliberate missing-term or incorrect-mapping case. The consumer remains responsible for required input fields, domain decisions and dependency migration.

Use `WorkerExecutor(python=installed_python, timeout=30, limits=Limits(...))`
for bounded embedding. `run("context", {"document": output, "expectations":
expectation_data, "resources": contexts, "base": logical_base})` transfers JSON
resources and returns a raw `report`. Supported operations also include
`emission`, `serialization`, `flatten`, `vocabulary` and `project`; declarations
use their existing JSON codecs. `project` takes `configuration` and an absolute
`directory`, retaining completed scopes on cancellation or deadline.

The trusted worker interpreter must already contain the package and requested
extras. No environment installation occurs. `memory_bytes=` requests the platform
address-space limit and fails explicitly when unavailable; cancellation accepts a
`threading.Event`. Worker crashes, protocol failures and limits never verify.
Synchronous calls keep their existing execution behavior.

`PreparedChecks(config).replace_resources(rdf={name: descriptor},
contexts={url: document})` returns an independent revision with acquired
replacements and fresh bounds. Unaffected owned resources are reused; dependent
preparation is invalidated. The old session retains its snapshots. Unread original
paths remain lazy, and per-call documents/declarations are checked afresh.

Use `load_bundle` from `bijux_ontology.workflows.projects.bundles` for a directory,
packaged resource or ZIP containing `project.json` and its declared files. Logical
IRIs and graph selections survive moving the bundle. `bundle.vocabulary(name)`
and `bundle.document_loader` reuse owned offline resources; `materialize(new_path)`
returns a project configuration. The CLI accepts `project BUNDLE --bundle`.
Completeness covers the selected RDF closure and exercised context positions;
it does not establish semantic acceptance or coverage of future scoped contexts.

`prove_obligations` answers typed `OntologyObligation` questions independently,
retaining established, contradicted or unknown status and compact witnesses.
`assess_models(..., obligations=...)` retains raw `model_report` and requires every
selected answer in its aggregate `report`. A proved named ancestor never removes
an unresolved class restriction; disjointness does not contradict subsumption
because the class may be empty. Global consistency remains a separate question.

`check_instance(instance, serializer, models, usages, vocabulary=vocabulary,
options={"mode": "json", "by_alias": True, "exclude_none": True}, context=context)`
calls an ordinary serializer once with the chosen keyword options. It returns
owned `output`, the contextual `document`, and combined declaration/output checks.
Pass an adapter’s `declaration_report` to retain extraction limitations. This is
sampled evidence; it does not prove every possible serializer output. Callbacks
execute in-process.

`ValueDeclaration` treats `language=None` and `direction=None` as unchecked;
`ABSENT` requires absence. Languages compare without case, while `ltr` and `rtl`
compare exactly. Model and serializer checks accept `rdf_direction="i18n-datatype"`
or `"compound-literal"` to qualify supported directional output. Physical RDF
datatypes and resource kinds still govern range checks. Ambiguous compound
resources, unsupported encodings and unresolved source-to-RDF counts remain
unverified. `PreparedModels(..., rdf_direction=...)` freezes that policy for
its fresh checks. Pydantic sample checks and serializer corpus checks accept the
same keyword separately from actual serializer options.

Pass `ValueDeclaration(..., constraints=ValueConstraints(...))` for actual
emitted values. Enumerations preserve exact RDF terms; finite `Decimal` bounds
compare qualified integer/decimal values. Lengths count original Unicode code
points, and `iri_namespaces` uses exact IRI prefixes. Install
`bijux-ontology[patterns]` for RE2 UTF-8 patterns with explicit `fullmatch` or
`search` mode. Unsupported syntax or conversions remain unverified; constraints
never rewrite RDF identity or assert ontology inconsistency.

Pass `fact_selection=FactSelection((FactPredicate(predicate_iri),), scopes=("local",))`
to `compare_vocabularies` or `assess_upgrade` for arbitrary exact assertions.
The resulting `selected_facts` retains graph ownership, original RDF terms and
per-position roles alongside categorized changes. Custom endpoint roles require
explicit `subject_role`/`object_role` choices. Upgrade `role_uses` accepts explicit
`RoleUse` contracts; `fact_uses` distinguishes direct from uncertain role joins.
Anonymous comparisons preserve joint sharing through maintained RDF comparison;
changed anonymous ownership remains unverified. Incomplete inventories establish
no missing facts, and graph selectors never pair foreign blank labels.

Use `ValueDeclaration("iri", role=ResourceRole("class", punning="allow"))`
to select class identifiers explicitly. Concept and instance roles remain
separate; an instance role's `types` require actual asserted or supported named
witnesses. `check_resource_roles(vocabulary, [RoleUse(term, role)], data=source,
graph=graph_name)` checks finite selected roles directly. `RoleBridge` follows
one explicit vocabulary link; target facts never discharge source type
obligations. Source emission requires named endpoints and leaves bridge-to-instance
unknown; native checks can use exact owned blank nodes. Punning prohibition is
application policy. Full OWL profile requests require a separate capability and
remain unverified when unavailable. `classify_statement` retains per-position
roles: the two occurrences in `C rdf:type C` are individual and class.

Use `check_data_compatibility(data, vocabulary)` for finite actual RDF checks
of named domain/range consequences, explicit disjoint types, property kinds and
supported literal range membership. Select the data `graph_name` or `union` and
ontology source `scopes` explicitly. The default `mode="inferred"` includes named
endpoint consequences; `mode="asserted"` examines supplied types. Neither mode
claims global OWL consistency or complete reasoning. Unsupported expressions
remain unverified. `asserted_types="required"` adds a separate application
validation report requiring exact type assertions; open-world consequences do
not count as missing fields. Raw ontology evidence is retained in `children[0]`.

`compare_literal_values(left, right, regime=...)` returns the original RDF
literals and an `equal`, `different` or `indeterminate` relation. Choose
`sparql-1.1` numeric promotion or `owl2-datatype-map` data-value identity;
the latter preserves disjoint numeric families, signed zeros and timezone
offsets. Exact RDF term equality remains separate. Supported calendars require
`[xsd]`, explicit timezone, years 1–9999 and at most six fractional digits;
SPARQL supports `dateTime`, while OWL also supports `dateTimeStamp`.
NaN, custom equality and unavailable conversions remain indeterminate.

`DatatypeRegistry` owns exact custom-IRI `DatatypeEvaluator` entries. Supply
conjunctive sample validators, an optional bounded scalar converter and an
explicit directional datatype relation independently. `convert` returns a
`DatatypeConversion` retaining the original literal; conversion establishes no
inclusion relation. Pass `datatype_registry=` to fresh model, data, source/native
emission or generic serializer checks. Missing capabilities and callback failures
remain unverified; standard datatypes cannot be replaced. Callbacks execute in
process with their ordinary state and are evaluated afresh by each operation.

`check_index_maps(prepared_usages, expectations)` checks exact source map keys
and processor-qualified correspondence using `IndexMapExpectation` and named,
graph-qualified `IndexBinding` records. Raw key facts survive unresolved origin;
merged or anonymous records remain unverified. `ContextExpectation(index=...)`
checks expanded node/value metadata; `list_index=...` checks the wrapper metadata
separately from members. `None` leaves either unchecked; `ABSENT` requires absence.
Plain index metadata and source keys do not change RDF equality. An explicit
`index_property` selects a custom property index whose contribution is ordinary
RDF. No association is inferred from raw positions or generated blank labels.

`check_document_form(document_or_prepared, requirements, expectations=...)`
checks explicit source representation with `FormRequirement` selectors for
object/array/scalar shape, exact required/allowed keys and reference/inline
embedding. Selections require source occurrences. Unrequested forms remain
unrestricted. `form_report` records representation, `semantic_report` requires
maintained conversion and selected `ContextExpectation` meanings, and `report`
requires both. RDF-equivalent flat and nested documents can have different form
outcomes. Reused `PreparedUsages` pins its snapshot, resources and shared budget;
applied source omissions cannot witness original form.

Use `compile_context(probes, type_scoped=False)` to generate mappings from
explicit `ContextProbe` model/field intent. Supported forward/reverse fields,
IRI coercions, datatypes, languages and list/set containers are checked by the
maintained processor. Type-scoped mode requires explicit compact type terms.
Conflicts, unsupported scope or exhausted qualification withhold `.context`;
a successful export is an independent JSON object. Retain `compiled.report`
with `check_instance(..., context=compiled.context,
declaration_report=compiled.report)` when checking actual serializer output.
Generated representatives do not establish every possible output.

For ordinary nested Pydantic models, an authoritative context can supply the RDF
mappings without duplicate primitive declarations:

```python
from bijux_ontology import OutputRequirement
from bijux_ontology.interfaces.adapters.pydantic import check_instance

checked = check_instance(
    catalogue,
    vocabulary=ontology,
    context=context,
    options={"mode": "json", "by_alias": True, "exclude_none": True},
    requirements={(Item, "label"): OutputRequirement(require_property=True)},
)
output = checked.output
```

Actual output must assert model types. The profile observes real serializer
output, including aliases and annotated serializers, and checks supported nested
children automatically. An optional independent `inventory` retains its own
mappings and requirements. Unresolved/custom output shapes remain unverified;
absent optional fields are explicit sample exclusions. Preparation and checking
share one operation budget. Success qualifies this output and these options.

For LinkML, `interfaces.adapters.linkml.model_declarations(view,
serializer="rdflib")` qualifies the maintained schema-driven RDFLib dumper.
The view must be import-free or contain a fully resolved local import closure.
Identifier slots define subjects and enum meanings can emit IRIs. Use
`interfaces.adapters.linkml_rdf.check_instance(instance, view=view,
vocabulary=ontology, options={}, class_name="Catalogue")` to retain the exact
checked `source` and combined `report`. `RdfSerializer(view, options={})` reuses
owned schema preparation for repeated `.check(...)` calls. Unsupported serializer
features remain unverified; the adapter retrieves no missing imports.

Resolve finite selectors through `prepared.select(...)`. `SourceSelector` composes
`Property`, `ArrayItems` and `KeyedItems`; each step’s bounds apply to every selected
parent, independently of aggregate bounds. Check the returned concrete `paths`
with existing expectations. Required missing children name their selected parent.
`EntitySelector(types=(class_iri,), graph=None, coverage_unit="rdf_entity")` selects
joined asserted entities; `coverage_unit="source_occurrence"` instead retains each
describing object’s obligations. IDs/types are expanded identities, graph scope
is explicit, and optional zero matches remain non-applicable.

For context releases, `assess_context_corpus` compares explicit
`ContextCorpusDocument` outputs under separate before/after resource loaders.
Declare required `CorpusBranch` witnesses by document name and concrete usage
path. Inspect baseline, candidate, selected usages, RDF preservation and exercised
coverage separately; unchanged failures stay visible, and candidate success
cannot establish a missing baseline. `compatible` is true only for verified
scoped preservation, false for a demonstrated counterexample, otherwise unknown.

Use `allowed_omissions=(("records", 0, "label"),)` for exact whole-property
exclusions before checking retained usages. Excluded paths are reported and do
not satisfy required coverage. Invalid, overlapping or ambiguous paths apply no
exclusions; context definitions, keyword aliases and opaque value interiors
remain protected. `allowed_drops` still applies to every matching term spelling.

Dataclass and TypedDict integrations use the corresponding
`bijux_ontology.interfaces.adapters` modules’ `model_declarations`. Supply a
finite registry and explicit `RdfField` metadata: dataclasses use the `rdf`
field metadata key, while TypedDict fields use `Annotated`. Serializer sidecars
can describe output aliases and contracts; conflicting metadata remains visible.
Python field types, defaults and required keys do not imply RDF emission.

`load_catalog(Path("catalog.xml"), versions=[OntologyVersion(ontology, version)])`
provides immutable offline imports for `load_vocabulary`. The supported OASIS
subset is `catalog`, nested `group` and exact `uri` entries with inherited
`xml:base`. Targets are local files; rewrites, chaining, DTDs, duplicate names
and conflicting versions fail. Logical entry/version IRIs supply RDF bases.
Use `formats={iri: "xml"}` for ambiguous `.owl` targets and explicitly select
dataset graphs through the returned `RdfSource` values.

A root `ModelUsage` also checks unambiguous nested models declared by its value
branches. Actual processor-established contributions identify children through
arrays and list/set wrappers; named references follow descriptions in their
exact graph. Explicit bindings remain available and conflicting model choices
fail. Ambiguous anonymous correspondence or model alternatives stay unverified;
removing an anonymous node’s only identifying evidence can cross that boundary.

`FieldDeclaration(output=OutputRequirement(...))` separates required source
properties, null/empty permission, distinct RDF value bounds and per-list member
bounds. Repeated unordered values count once; repeated list positions count
separately, and an empty list contributes `rdf:nil` with zero members. These
are application output requirements, independent of Python constructor fields
and OWL cardinalities. Legacy `optional=True` aliases optional emission and
rejects conflicting required output; its default `False` adds no guarantee.

`PreparedUsages.observe_many(paths)` shares maintained processing for supported
primitive fields on uniquely named subjects. Scoped contexts, anonymous subjects,
lists and ambiguous correspondences retain conservative individual observation.
The same owned preparation can serve repeated checks within its cumulative
budget. Worker calls still start separate processes; this API adds no session.


`ModelDeclaration(..., emission_groups=(EmissionGroup("name", ("a", "b"),
minimum=1, maximum=1),))` declares an exactly-one group over emitted RDF
predicates. Explicit `EmissionScenario` cases describe whole field combinations;
`closed=True` excludes unlisted cases, while open uncovered cases remain
unverified. `EmissionCondition(field, equals=literal_or_iri)` compares one exact
RDF discriminator. Missing, multiple or unresolved terms remain unknown.
Source presence/null requirements remain separate. Declaration compatibility and
actual source/entity/native RDF checking preserve their respective scopes.
