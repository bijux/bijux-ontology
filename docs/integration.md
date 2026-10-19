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

`interfaces.adapters.oak.as_oak(view, registry=registry, language="en")` exposes
an owned selected view through the maintained OAK `BasicOntologyInterface`.
Install `[oak]` on Python <3.15 for OAK 0.7.4. Core imports need no OAK package.
Labels, aliases, definitions, asserted relationships and obsolescence retain
their selected source meanings; the original `.view` keeps qualified evidence.
Subset enumeration requires explicit `closed_subsets=True` for this finite
selected source. Incomplete metadata, unsupported inference and mutation raise
`OakOperationError` with the required report. One budget covers each whole batch;
an explicitly supplied shared `Budget` also spans successive calls.

`interfaces.adapters.obo_graphs` reads and writes a selected OBO Graphs ontology
with `[jsonschema]` and the bundled upstream schema. Class edges retain subclass
or authored existential meaning; supported logical definitions and qualified
annotations round-trip. Supply an ontology `graph_id` and exact physical graph
selection; generic plain-literal metadata predicates require explicit selection.
Unsupported source assertions appear in `.omitted` and block export by default.
`allow_lossy=True` permits supported output while retaining required loss findings.
Preservation concerns supported axiom meaning, with distinct ontology/entity roles.

Install `bijux-ontology[templates]` for `IdentifierTemplate(template, keys)` with
explicit `TemplateKey` string/integer bindings. `.construct()` uses maintained
URI escaping; `.construct_many()` rejects exact IRI collisions atomically.
Forward profiles support one `{key}` or `{+key}` per key. `.recognize(iri)` retains
the original spelling and checks only unmodified slots separated by reserved
literals, strict single decoding and exact re-expansion. Ambiguous or unsupported
inverse profiles return no guessed record.

`vocabulary.accessions.check_accessions(values, registry, rules=...)` applies
explicit `AccessionRule` policies to `AccessionValue` occurrences with declared
CURIE or IRI intent and source locations. Install `[patterns]` for RE2 grammar,
`[checksums]` for ISO 7064 MOD 11-2 or Luhn, and `[templates]` for alternate IRIs.
ASCII case corrections are separate suggestions; original values stay intact.
Unknown namespaces may be explicitly excluded. Missing rules and unsupported
alternate recognition remain unverified, and valid accessions imply no term
existence.

`interfaces.adapters.bioregistry.read_bioregistry` imports explicitly selected
records from supplied bytes with a required SHA256 and revision label. Original
records remain available; supported namespace aliases feed the existing registry.
Use `BioregistryOverride` for local namespaces, aliases and `AccessionRule`
replacements. Source patterns and unsupported provider semantics remain visible;
local rules do not establish equivalence to them. `read_bioregistry_bundle`
loads an explicit member through the ordinary offline bundle reader. Neither
operation creates ontology declarations.

Use `workflows.upgrades.identifiers.compare_identifier_releases` with two
`IdentifierRelease` policies and actual `AccessionValue` occurrences to distinguish
changed expansion, canonical spelling, validity and recognition ambiguity.
Raw before/after checks remain available. Advisory updates require explicit
`IdentifierEquivalence` IRI pairs and actual after-policy validation; no input is
rewritten and ontology membership cannot erase a changed identifier.

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

Use `check_rdf_stream(path, vocabulary)` from `bijux_ontology.data.streams` for
plain N-Triples/N-Quads statement-local checks. Parsing, local checks and requested
global obligations have separate reports; explicit excluded IRIs remain visible.
A sealed disk vocabulary requires an explicit `vocabulary_scope=QueryScope(...)`.
Rows that pass are discarded; retained findings keep exact statement occurrences.

For caller-managed Pyoxigraph stores, use `prepare_external_store` and
`query_external` from `bijux_ontology.rdf.stores.external`, or
`check_external_terms` from `bijux_ontology.vocabulary.external`. Each operation
uses one native read transaction; later calls may observe later commits. Selected
declarations require no whole-store copy. Hard deadlines and named-scope presence
checks are unavailable for this borrowed in-process profile.

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

Use `skos.label_projection.project_xl_labels` on one prepared physical graph to
produce plain SKOS label consequences. Original equal-form label resources stay
distinct; correspondence and omitted metadata or relations remain explicit.
`promote_labels` adds XL assertions from exact `LabelIdentity` assignments with
fresh distinct IRIs, including checks against existing datatype IRIs. Plain
labels remain. Both operations require explicit consumer `after_checks` and
selected native label integrity. Export accepted candidates through
`export_migration`; plain projection does not claim a lossless round trip.

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

Use `vocabulary.relation_paths.check_relation_path` for explicit typed regular
languages: compose `RelationStep(predicate, role)` with `RelationSequence`,
`RelationChoice`, `RelationInverse` and `RelationRepeat`, and select a
`RelationEndpoint`. Class, SKOS, existential restriction and raw asserted roles
remain distinct. Witnesses retain original assertions and scopes; inverse means
navigation only. Cycles terminate, and incomplete traversal cannot prove a
nonmatch. Missing sources cannot satisfy zero repetition. `relation_enumeration`
classifies an explicit candidate inventory for ordinary application field
constraints; its closed selection covers only the supplied view and language.
For classification, `find_relation_path` permits complete nonmatches: its report
qualifies search coverage, while `status` records whether a path was found.

Use `vocabulary.slims.assign_slim` with original `SlimAnnotation(record_id, term)`
occurrences, explicit roots and `policy="all"` or `"nearest"`. The default view
uses asserted subclass paths; `path` can select the typed relation language.
Nearest counts typed edges and retains all minimum-distance ties. Every required
root comparison must finish before selections are final. Counts distinguish
annotation occurrences from distinct exact record IDs; uncovered and unknown
remain separate. Original fine terms and partial witnesses stay visible.

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

Use `check_record_topology(source, records, contract, roots=record_ids,
complete=True, closure="closed")` for explicit application reachability,
ownership and cycle rules. `TopologyRelation` selects each path's policy roles;
references become containment only when declared. `TopologyRecord` binds a
named or owned blank subject and optional known output location. Roots are
exempt from minimum ownership, and explicit `shared_targets` from its maximum.
The selected graph supplies all record and path support. Open or partial
external scope retains unresolved absence; witnessed cycles and excess owners
remain findings. This operation makes no OWL consistency claim.

Declare a logical multi-hop field with `PathFieldDeclaration` and bind its
actual subject using `PathFieldUsage`. `check_path_fields` checks finite
predicate, sequence and inverse paths on owned RDF or `JSONLDPreparation`.
Endpoint alternatives/counts and `IntermediateRequirement` node counts, types
and ownership are separate obligations. Graph scope applies throughout; paths
never become direct ontology properties. Source permissions, unsupported paths
or ambiguous bindings remain unknown. The default output contract waives JSON
null/empty restrictions; requesting them requires separate source evidence.
`project_path_field(..., shape=shape_iri)` from `bijux_ontology.shacl` reuses
typed model translation for supported Core constraints. Keep its report with
backend results, including any lexical or intermediate-description limitation.

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

`vocabulary.lexical.prepare_lexical_index(view, fields=..., languages=...)`
reuses the selected vocabulary's lexical inventory for repeated `autocomplete`
and `suggest` calls. Results retain labels, identifiers, languages, source roles
and ranking evidence. Suggestions allow one codepoint insertion, deletion or
substitution; they never replace identifiers or assert semantic mappings.
Inspect both `.report.complete` and `.truncated` when consuming bounded results.

`annotate_text(index, text)` reuses that index to locate overlapping dictionary
mentions with all selected candidate terms and lexical evidence. Its half-open
spans count original Python characters, including when casefold expands a
character. Word boundaries are explicit; `boundary="substring"` also permits
embedded hits. Unicode composition is unchanged. Keep the returned report:
interrupted extraction retains complete mentions but cannot prove their absence.

`vocabulary.obo.cross_references.check_cross_references(view, terms, registry,
rules=...)` checks original term, definition and synonym dbxrefs with the existing
identifier policies. Optional `LocalCatalogue` inventories resolve exact IRIs;
only explicitly closed catalogue coverage can prove a dangling reference.
External references remain external, and alternate spellings establish no IRI
equivalence. Native OBO annotation records remain separate evidence; the required
report covers xref capture, attachment, accession and selected resolution policy.

Install `[fhir]` for `interfaces.adapters.fhir.code_system.read_code_system` and
`write_code_system`, backed by the maintained R5 model. Supply explicit reversible
`FhirCodeBinding(FhirCodeIdentity(system, version, code), iri)` values from
`interfaces.adapters.fhir.identity`; neither versions nor RDF IRIs are inferred.
Supported concepts retain hierarchy, multilingual designation qualifiers and
code/string/integer/boolean properties. The owned vocabulary supplies ordinary
annotation checks; its RDF labels do not represent every FHIR qualifier.
`check_code_membership` distinguishes complete content from fragments and
supplements. Case-insensitive comparison requires explicit `case_comparison="ascii"`;
original spellings remain unchanged. Keep all reports: unsupported fields,
fractional numbers and unavailable comparison regimes cannot establish acceptance.

The sibling `fhir.value_set` adapter reads and writes supported R5 ValueSets.
`compile_value_set(..., mode="compose", systems=..., bindings=...)` applies finite
local includes minus exclusions through the existing allowed-value contracts;
`mode="expansion"` keeps abstract grouping entries separate from submitted codes.
Every coded entry needs its own system and version policy. Paged expansions,
ambiguous totals, unavailable versions and unsupported filters remain open;
neither nesting nor an available parent supplies a missing identity. Export
preserves the original presentation tree and parameters without declaring closure.

`workflows.ontology.ownership.check_ownership` applies explicit `OwnerClaim`
declarations to protected outgoing `FactSelection` predicates and exact terms.
Claims supply actual authoritative views; exact terms and the longest declared
namespace select ownership, optionally by RDF role. An external extension may
omit imported assertions and add annotations outside the protected selection.
New protected assertions must match authoritative content. Evidence retains
original source/graph witnesses; namespace overlap, duplicate assertions and
legal punning alone establish no conflict. Anonymous definitions remain unknown.
Each current term needs a subject occurrence in the selected scope; an owner's
separate evidence cannot supply that coverage.

Use `canonicalize_rdf(source)` for bounded RDFC-1.0 canonical N-Quads over the selected RDF 1.1 quads. The result records its algorithm, hash choice, literal fidelity and graph scope. Empty or unknown graph inventory prevents publication unless explicitly excluded. Canonical blank labels do not establish identity across changed graphs, raw-resource integrity or OWL equivalence. An optional OS address-space bound fails explicitly where unavailable.

`rdf.skolem.skolemize(source, caller_owned_base)` names blank resources with
fresh scoped IRIs across subject, object and graph roles. Retain its `.mapping`
for `deskolemize(result.source, result.mapping)` on that exact output snapshot.
Independent operations share names only through explicit owned `BlankNodePair`
correspondence. Naming changes RDF identity; reversal preserves original sharing
and exact literals. Similar IRI spellings never authorize reversal.

`rdf.algebra.rdf_set_operation(left, right, operation, ...)` computes exact
intersection, difference or symmetric difference. Explicit graph selections
operate on triples; whole datasets retain quad and empty-graph inventories.
Every retained quad keeps its graph. Independent anonymous inputs require
complete owned `blank_pairs` or explicit `named_only=True`; one snapshot shares
identity across its graph selections. No logical equivalence is inferred.

`rdf.reification.inspect_reifications(source)` inventories exact per-graph
statement descriptions and annotations without asserting their described triples.
Missing or ambiguous components remain visible. Use `check_reifications(source,
ReificationContract(...))` for explicit completeness, component uniqueness,
`rdf:Statement` typing or per-graph statement uniqueness. Separate reifiers
retain their identities even when their described triples match.

`rdf.containers.inspect_containers(source)` retains numbered membership in
numeric order, including gaps, ambiguous slots and repeated values. Closure is
an explicit `ContainerContract("sequence")` or `ContainerContract("bag")`.
`container_to_list` rewires one selected `ListTarget` after matching exact
`expected_values`; a bag also needs explicit numbered ordering. Original
container data remains intact. Selected migration checks gate final export.

The opt-in `rdf.triple_terms` API uses the explicit
`TRIPLE_TERM_CONTRACT` for the RDF 1.2 April 2026 Candidate Recommendation and
PyOxigraph 0.5.11. Call `prepare_triple_terms(RdfSource(...), contract=...)`,
then inspect its `.snapshot` or `export_triple_terms(snapshot, format="nt-1.2")`.
This graph-only snapshot retains actual nested terms and exact literals without
asserting described triples. RDF 1.1 export, canonicalization, SHACL and
reasoning APIs reject it; broader RDF 1.2 compatibility is not implied.

Use `export_jsonld(source, context=context, base=logical_base)` to compact selected RDF through PyLD and verify its re-expanded RDF before writing with `write_export`. An optional standard `frame` requests embedding or reverse fields; losing selected triples blocks export. Context loading is offline by default. Source JSON shape and index metadata are outside this RDF preservation claim. Empty named graphs require an explicit exclusion or prevent export.

`jsonld.composition.compose_contexts(modules, resolution="retain-required")`
processes caller-ordered `ContextModule` values as a JSON-LD context array.
Every module supplies nonempty representative `ContextCase` usages and
`ContextExpectation` obligations. Independent and composite checks must both
pass before `.context` is returned. Protection, nullification and scoped
contexts use maintained processing; qualification covers the declared cases.

`rdf.query_transforms.transform_sparql(source, SparqlProgram(...), after_checks=...)`
executes a finite local `CONSTRUCT` or single `UPDATE` on one selected physical
graph, returning an exact patch and `MigrationCandidate`. Use `SparqlBinding`
parameters and explicit typed string constants. Retrieval, graph management,
assignment, aggregation, `VALUES`, subqueries and anonymous WHERE syntax are
outside this profile. Required after checks and explicit acceptance gate export.

`jsonld.publication.check_context_publication(context, PublicationContract(...))`
checks selected root protection, vocabulary/base and local version promises.
Supply retained `ContextCase` usages and actual `ContextOverride` scenarios for
each `PublishedTerm`. Legal extensions must preserve retained mappings; blocked
overrides require the processor's precise protected-definition rejection, which
remains visible as raw evidence. These policies apply only when requested.

`jsonld.html.extract_html_jsonld(content, document_url)` extracts JSON-LD
scripts through the maintained HTML processor and returns `.scripts` locations,
independently owned `.document`, effective `.base` and prepared RDF `.source`.
Choose an exact `script_id`, media-type `profile`, first or all scripts, and
`html_base="honor"` or `"ignore"`. Selected duplicate JSON keys and missing
scripts fail; contexts still require the ordinary supplied offline loader.

Use `flatten_document(document, base=logical_base)` for checked flat JSON-LD.
Its report checks RDF preservation, including named empty graphs; its owned
`document` and graph-qualified `nodes` support `node = result.bind(record_iri,
graph=graph_iri)` without array-order assumptions. Check `node.value` with
`check_emission(node.value, models, [ModelUsage(model_id)])`.

With `correspondence=True`, inspect `correspondence_report` separately. Named
descriptions expose `expanded_paths` into expanded input, not compact source
pointers. Blank-node or anonymous-graph correspondence remains unverified without
an identity witness. RDF preservation does not promise original JSON shape.

Use `frame_document(document, frame, retained=EntitySelector(...),
predicates=(...))` to check an intentional projection through PyLD. Select named
roots in one explicit graph; required predicates and their attached anonymous
structures compare jointly, preserving shared identity, lists and exact literals.
Declare omitted records with `omitted` and seed predicate exclusions with
`omitted_predicates`; exclusions require observed source content. Those predicates
are outside preservation truth, including framing defaults there. Required loss
or invented required values withhold the checked document and RDF. This proves
selected retention, with no whole-dataset equivalence or pure-subset guarantee.
`result.bind(iri, graph=...)` supplies actual output paths for `ModelUsage`; pass
`result.base` to subsequent JSON-LD checks when IDs are relative. Direct processing
runs in process; `WorkerExecutor.run("framing", ...)` supplies explicit isolated
deadlines and cancellation using the same offline resource protocol.

Loading errors expose `InputError.failure` from `bijux_ontology.errors`: the
logical document IRI, supplied location and role, known import chain, and any
parser-provided span. Pass `location=` and `role=` on `RdfSource` when available.
Reports retain these as `Issue.details.failure`, independently of the job's
configuration location; absent parser coordinates remain absent.
Source identity, supplied location and nondefault role text also consume the
shared cumulative byte budget during preparation.

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

`extract_profile(shapes, (root_iri,), cases=(ProfileCase("valid", data, True),))`
from `bijux_ontology.shacl` captures finite referenced shapes, paths, lists and
prefixes. Supply a separate `ontology=` resource when needed. Independent roots
are omitted; unresolved or dynamic dependencies remain unverified. Inspect
`structural_report` separately from the actual source/derived case comparisons.
`export_profile(profile, format="turtle")` exports qualified shapes and ontology
through faithful RDF serialization. Agreement covers supplied cases only;
independent role exports cannot preserve shared anonymous constants across roles.

`interfaces.adapters.shacl.import_shape_contracts` imports selected property
constraints through explicit `ShapeFieldBinding` serializer keys. Use
`check_imported_output` for actual serialized JSON-LD or `check_imported_rdf`
for selected native RDF. Both retain untranslated original obligations and
qualify aggregate values; bare declarations cannot establish full SHACL
conformance. Selected list-shaped values remain unknown. Shape targets and
`sh:class` never manufacture exact application type declarations.

`inspect_shapes(shapes, (root_iri,))` projects supported supplied constraints into
the immutable authoring records and reports uninterpreted semantic RDF in
`remainder`. `edit_shape_counts(inspection, (ShapeCountEdit(property_shape,
ShaclCount(minimum=1)),), data=actual_data)` supports ordinary property count
edits. The candidate retains every other assertion in the selected physical
graph and runs maintained validation with root coverage. Unknown extensions
block edits by default; explicit `allow_opaque=True` preserves them while keeping
the projection unverified. Export `candidate.source` through `export_rdf`.

`analyze_shape_profile(profile)` checks finite count, fixed-value and enumeration
incompatibilities before loading data. Local conflicts remain conditional on
invocation; only justified necessary conflicts propagate to selected roots.
Inspect signed dependencies and components for recursion boundaries. The analyzer
checks dependency metadata against actual RDF and preserves valid alternatives.
`not_determined` never certifies arbitrary satisfiability; recursive semantics,
qualified sibling exclusions and unsupported metadata remain explicit unknowns.

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

Author reusable parameterized checks with `ShaclComponent`, `ShaclParameter`,
`ComponentValidator` and `ComponentUse` from `bijux_ontology.shacl`.
`compare_shape_refinement(parent, child)` from `bijux_ontology.shacl` compares
`RefinementContract(source, root, data_graph=...)` under aligned declared targets
and graph scopes. The finite fragment covers counts, exact fixed/enumerated terms,
datatypes, node kinds and acyclic node conjunction/disjunction. Refinement requires
a universal supported implication proof plus actual compatible data; weakening
requires a witness passing the child's complete target scope and failing the
parent's. Unsupported constraints or unsuccessful finite witness construction
remain unknown. Witness literals must pass the maintained lexical checker.

`compile_components(shapes, components, uses)` returns ordinary owned SHACL RDF.
The supported forms are generic ASK and node/property SELECT; required parameters,
query forms, pre-bound variables and offline prefixes are qualified explicitly.
`check_components(data, compiled, trusted_sparql=True)` runs the actual backend
without enabling advanced mode for ordinary ASK. Keep structural and query
reports separate; unavailable custom functions and anonymous execution/data
bindings remain unknown. Package the artifact through ordinary `export_rdf`.

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

For repeated datasets, use `with ShaclSession(shapes, ontology=ontology) as session:` from `bijux_ontology.shacl` and call `session.validate(data)`. The session owns its fixed resources and reuses one isolated worker; construct a new session to replace them. `validate_batch(datasets)` retains per-dataset results and explicit pending coverage. Deadlines include worker queueing and startup; cancellation, worker failure and close release owned processes. `scope="records"` preserves local conformance while leaving wider dataset conformance unverified. Sessions use shared platform process ownership; Windows runtime qualification remains pending.

Pass `budget=operation` to share one cumulative budget across session construction
and validation calls. Preparation, transport and result projection use its actual
remaining allowances. Omitting it keeps calls independent; each batch still has
one cumulative budget. Late coverage exhaustion retains the qualified raw report
and completed negative findings.

`shacl.examples.generate_examples(shapes, root, focus, seed=seed, values=pool)`
searches caller values for a finite acyclic direct-path contract. The nonempty
seed and original explicit shape/focus binding remain fixed. An optional
`MissingValueExample` requests an actual failing MinCount witness and checked
preserved siblings. Inspect construction status separately from each native
validation result; unsuccessful search is no general unsatisfiability proof.

For checked RDF deltas, `prepare_incremental(data, shapes, use_shapes=(shape_iri,))`
from `bijux_ontology.shacl.incremental` runs an actual baseline. Pass its owned
`.state` and an exact `DatasetPatch` to `revalidate`. Complete passing local Core
checks reuse unaffected named focus/shape pairs; target, schema, global and
complex-path changes require full selected checking. Results retain checked and
reused counts, raw executions and the checked patch. Unsupported shared anonymous
identities yield no reusable state.

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
`emission`, `serialization`, `flatten`, `framing`, `vocabulary` and `project`; declarations
use their existing JSON codecs. `project` takes `configuration` and an absolute
`directory`, retaining completed scopes on cancellation or deadline. Direct SHACL
and query-rule jobs execute inside that owned worker; the host also enforces
their configured job timeout, without creating an independent nested process.

Use `check_project_parallel(configuration, workers=2, artifacts=Path("artifacts/checks"))`
from `bijux_ontology.workflows.projects.parallel` for independent vocabulary,
single-document context and SHACL jobs. Captured regular files and separate
processes keep inputs stable. Disjoint grants share the configured budget; raw
verified dependencies govern starts and results retain declaration order.
Inspect `.jobs` and `.report`. Prepared resource revisions and other job profiles
remain explicit unsupported scopes.

For async hosts, use `async with AsyncChecks(executor, max_running=2, max_queued=8)`
from `bijux_ontology.interfaces.execution.async_checks`, then await
`checks.run(checks.prepare(operation, parameters))` or `checks.batch(requests)`.
Requests own JSON inputs; each worker retains its ordinary limits. Batch collection
has a separate cumulative host budget. Cancellation returns incomplete execution
evidence after owned cleanup; session exit drains admitted work.

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

`bijux_ontology.workflows.projects.profiles` provides `load_profile`,
`compose_profile`, `profile_to_prof` and `read_prof`. A `profile.json` sidecar names
required parents from an explicit offline registry beside ordinary `project.json`.
`compose_profile(domain, registry).check(new_directory)` retains every selected
layer's jobs and raw reports. Optionality applies only to declared parent layers;
conflicting named shapes require an explicit policy retaining both checks.
Composition and materialization inventories are bounded; execution and report
limits apply separately per project. PROF interchange describes resources and
parents without manufacturing executable checks.

`bijux_ontology.workflows.projects.routing.check_profile_routes` reads configured
declaration predicates from actual selected records and dispatches a closed
`ProfilePlan` registry. Every record retains the caller's mandatory baseline,
including missing, unknown or weaker declarations. Multiple declarations require
an explicit `multiple="all"` policy. `ProfileJobBinding` binds every selected
SHACL job's complete named shape set to the supplied snapshot; completed named
shape/focus coverage is required. Unsupported data jobs and blank focus coverage
remain unknown. Whole selected graphs retain shared nodes. Pass actual JSON-LD
conversion evidence as `input_report`; no re-expansion or remote discovery occurs.
Routing capture and report adoption are bounded separately from each project's
backend limits.

`bijux_ontology.workflows.projects.instantiation` provides `ParameterizedProfile`
and `instantiate_profile`. Explicit `ConstraintSlot`, `InventorySlot` and
`ResourceSlot` bindings compile typed RDF terms, integers, enum choices and owned
vocabulary resources into an ordinary profile in a new directory. A `ProfileShape`
uses the existing typed SHACL authoring records. The supported baseline is a
complete ordinary graph with named authored shapes; opaque or unqualified
representations remain unknown. Parameters retain exact RDF terms and resource
identities. `instance.report` describes preparation; run `instance.effective.check`
for actual validation, including every required parent layer.

`load_bundle(..., dependencies=(parent,))` retains owned parent RDF and context
resources separately from local files. Dependent bundles require explicit
`materialize(path, local_only=True)` and cannot be exported as standalone archives.

Project version 2 adds file-backed `emission`, `context-corpus` and `upgrade`
jobs. `ProjectBuilder(directory=path)` builds the same `ProjectConfig` consumed by
`bijux-ontology project project.json`; its `emission`, `context_corpus` and
`upgrade` methods accept explicit input filenames. `CorpusDocumentInput` names
actual before/after documents and expectations; `ApplicationInput` selects a
vocabulary and a data-only application record. Emission jobs check supplied
serialized output through `check_emission`; serializer callbacks stay in ordinary
Python APIs. Application records use version 1 with `terms`, `models`, `fields`,
`outputs` and optional context/resources, reusing declaration and usage codecs.

Every job can name earlier required jobs with `depends_on`. A failed or
unverified prerequisite blocks its dependents while independent jobs retain their
results. Acceptance exceptions do not turn an unverified prerequisite into truth.
Version 1 projects retain their existing job schema.

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
Anonymous comparisons preserve joint sharing through maintained RDF comparison.
`VocabularyChanges.anonymous` retains complete original neighborhoods and their
named subject attachments, including shared owners and actual graph provenance.
`UpgradeAssessment.anonymous_uses` identifies affected supplied fields and named
hierarchy dependencies. Relevant or unowned changes retain scoped uncertainty;
complete unused attachments leave unrelated finite asserted checks complete.
`change_qualification` explains this required consumer scope while the original
selected-fact report remains available. Attachment attribution establishes no
internal blank-node bijection or arbitrary OWL consequences. Incomplete
inventories establish no missing facts; foreign blank labels establish no pairing.

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

Install the optional `bijux-ontology-reasoning` distribution for actual OWL 2
DL/EL/QL/RL membership. Its `profiles` API requires an explicit Java executable
and pinned OWLAPI bundle; the package README gives the build commands.
`check_profile(backend, source, profile="owl2-dl", imports=offline_map)` retains
native violations and loader metadata. Unparsed triples, guessed declarations or
missing imports prevent acceptance. This finite POSIX profile accepts one
physical graph per document; it does not establish consistency or satisfiability.

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

For computed Pydantic outputs, `model_declarations(..., field_contracts=...)`
accepts the public computed member name and retains its serialization alias.
Use `serializer_contracts={Model: declaration}` for explicitly described whole
model output, including renamed nested fields and emission conditions. Pass the
result as `check_instance(..., inventory=extraction, model=declaration.identifier)`
to check one actual serialization. Whole output subsets without explicit
`EmissionCoverage` remain unverified; sidecars preserve their independent RDF
intent when a serializer or context changes.

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


`compare_applications(old_vocabulary, new_vocabulary, before=old, after=new)`
checks separate `ApplicationVersion` declarations and actual outputs. Stable map
keys and sample IDs establish consumer correspondence. `ApplicationField` binds
an actual model field to explicit sample source paths; `ApplicationOutput`
requires explicit `ModelUsage` bindings, while `SerializerCase` invokes the real
callback once with its exact options. Inspect `candidate_report`, failure
`contracts`, declaration changes, field representation, mapping and RDF dataset
comparisons separately. Changed predicates remain visible even when both versions
pass. Missing identities, ambiguous source ownership and unavailable baseline
checks remain unverified; no matching is inferred from IRIs or diagnostic prose.
Anonymous attachment impacts retain independent before/after use roles.


`plan_rdf_patch(base, target)` proposes a portable exact `DatasetPatch` with
explicit base and target witnesses, quad edits and named-graph inventory changes.
Use `patch_to_data`/`patch_from_data` for JSON transport and `apply_rdf_patch`
for transactional application. Changed anonymous content needs explicit owned
`BlankNodePair` bindings and created/removed-node inventories. Affected base
references must resolve uniquely or have jointly checked `PatchBlankBinding`
witnesses. Canonical labels establish no historical correspondence. Wrong bases,
absent removals, ambiguous bindings and failed identity-preserving postconditions
return no candidate. Exact numeric lexical forms, shared blanks and selected
empty graphs survive; union projections and unqualified fidelity remain unknown.
The same cumulative budget covers host and owned worker bookkeeping; maintained
isomorphism internals are bounded by the operation's wall-clock deadline.

`rdf.merging.plan_rdf_merge(base, left, right)` records disjoint edits and
property, connected-list or graph conflicts. Pass that exact plan to
`apply_rdf_merge`, supplying a `MergeResolution` for every conflict and selected
before/after contracts. Raw conflicts remain in `.plan.report`; structural
agreement and application acceptance are separate. Anonymous correspondence
requires actual owned witnesses, and stale bases cannot apply.

`migrate_values(source, ValueSelection((predicate,)), action, ...)` proposes
an exact object-position migration within the supplied snapshot's scope.
`LiteralConversion(source_datatype, target_datatype)` preserves lexical characters
for supported string-to-integer and decimal/integer-family interpretations.
`ValueMap(((old_term, new_term),))` declares explicit exact rewrites, including
repairs; language changes and value collapse require their separate permissions.
Unsupported conversions and rejected selected samples expose no partial source.
The `ValueMigrationCandidate` retains an exact patch and separate inventory counts.

Supply named `MigrationCheck` callbacks through `before_checks` and `after_checks`
to retain actual application contracts on independent authorized snapshots.
Baseline failures remain visible when a repair passes its after contracts;
missing, empty or incomplete after checks cannot qualify acceptance. Callbacks
are ordinary caller-owned Python code. Inspect the candidate, then explicitly
call `export_migration(candidate, accept=True, format="trig")` to request an
export through the existing faithful serialization boundary.

`migrate_graphs(source, (GraphRoute(old_graph, new_graph),), ...)` routes original
quad memberships simultaneously. Routes may select subjects and predicates;
`None` denotes the default graph and blank selectors require owned terms.
Competing partition contributions require `merge=True`. Graph creation and
removal use explicit `added_graphs` and `removed_graphs`; a removed graph must
be empty after the proposed moves. Exact terms, shared blanks and unrelated
partitions remain intact. The same before/after contract and explicit export
gate applies, including actual graph-local constraint failures.

`execute_migration_plan(source, steps, final_checks=...)` runs explicit
`MigrationStep` operations over independent owned snapshots. Each operation must
satisfy its named preconditions, native migration report, exact current-base patch,
returned candidate comparison and postconditions. Final checks run on the actual
last candidate. A missing, failed or exhausted required gate withholds the final
source; earlier patches and named failure evidence remain reviewable. Callback
exceptions propagate, and trusted callbacks keep their ordinary Python lifetime.
`export_migration_plan(result, accept=True, format="trig")` rechecks the retained
final witness before serializing. `write_export(..., destination, overwrite=True)`
publishes complete bytes atomically; failed staging or replacement preserves an
existing file. Caller-owned binary streams retain ordinary streaming semantics.

`bijux_ontology.workflows.upgrades.replacements.plan_replacements` accepts
explicit `ApprovedReplacement` mappings, their entity roles and namespace owners.
One-to-one changes rewrite selected declaration slots and actual RDF occurrences;
joining identities requires `merge=True`. A split requires an explicit discriminator
on the original snapshot. Multiple candidates, unknown roles and replacement hints
alone leave the proposal unresolved. Generic property approvals preserve known
object, datatype and annotation property kinds. Required `ReplacementCheck`
callbacks inspect the updated declarations and fresh candidate data; missing,
empty or failed after checks block acceptance and export. Existing exact patches,
selected graph scope and `export_migration(..., accept=True)` remain the artifact
boundary. Trusted callbacks retain their ordinary Python execution lifetime.

Install `[jsonschema]` to import supported offline JSON Schema 2020-12 contracts.
`bijux_ontology.interfaces.adapters.jsonschema` exposes `SchemaModel`,
`model_declarations` and `check_instance`. Supply explicit RDF classes and
`RdfField` mappings for each referenced object variant. The checks invoke the
real serializer once with its supplied options and return the exact checked output. Set
`output_schema=True` to check that output against the JSON Schema assertions.
Schema `required` and `additionalProperties` remain JSON rules; RDF occurrence
guarantees require explicit `OutputRequirement` records. Missing mappings,
unavailable offline references and unsupported applicators remain unverified.

`bijux_ontology.vocabulary.obo.subsets.read_obo_subsets(view, closed=True)` reads
explicit OBO-in-OWL subset definitions and their original membership assertions.
Use `.select(subset_iri)` as `ValueConstraints(enumeration=...)` in ordinary model
checks. Only a complete closed inventory establishes absence; open, undefined or
anonymous membership remains explicit in the returned reports. Subsets do not
imply SKOS schemes or logical ontology modules.

`bijux_ontology.rdf.migrations.lists.migrate_list` accepts a `ListTarget` naming
one exact incoming edge and a `ListInsert`, `ListRemove` or `ListReorder` action.
Supply exact `expected_before` and `expected_after` RDF-term tuples and ordinary
migration checks. Dataset targets require an explicit physical graph. Changed
lists receive fresh cells; original cells, annotations and shared-tail siblings
remain intact. Actual after-checks and explicit accepted export use the same
migration gates as value and graph changes.

`bijux_ontology.interfaces.adapters.vocabulary_bindings.generate_vocabulary_bindings`
returns candidate Python source and a required report for explicitly selected
vocabulary IRIs. Use `names` for IRI-to-name overrides, `aliases` for additional
name-to-IRI bindings, and `invalid_names="normalize"` to request safe identifiers.
Reserved names and normalized collisions remain unresolved. Generated modules
expose exact RDFLib constants, immutable `TERMS`, and typed `INVENTORY` usable by
`namespace_inventory`; unknown members never expand to new IRIs. Optional
`annotations` select literal documentation without executing ontology text.

`bijux_ontology.data.foreign_references` provides `ForeignReferenceContract`,
`prepare_foreign_targets` and `check_foreign_references`. Supply scoped `KeyRecord`
corpora, aligned source/target `KeyContract` policies, and explicit completeness.
Reuse the prepared target index across source batches. Results retain actual
source keys and matched target records; open or incomplete lookup cannot prove
absence, while known excess matches still fail. An optional `target_model` runs
ordinary native RDF model checks on each matched owned target. These are
application reference obligations, independent of OWL identity assertions.

`bijux_ontology.interfaces.adapters.jsonschema.generation.compile_output_schema`
compiles explicit models and root names into JSON Schema 2020-12 plus linked RDF
mappings and annotations. `JsonRepresentation` selects native scalars, arrays or
explicit `@list` wrappers. Inspect `translations` and the required `report` for
untranslated or changed obligations; `.validate(actual_output)` checks JSON
conformance separately. JSON validation establishes neither RDF context/subject
semantics nor exact RDF numeric identity. Ordinary array length does not prove
RDF distinct-value counts. The existing importer accepts the documented finite
scalar/local-definition subset; general unions and list wrappers remain outside
that importer's supported scope. Install `[jsonschema]` to request compilation.

Use `author_vocabulary` from `bijux_ontology.vocabulary.authoring` for explicit
SKOS creation, preferred-label replacement, scheme membership, deprecation or
annotation edits. Supply typed edits, the exact physical graph and required
`MigrationCheck` postconditions. The resulting candidate uses the existing
checked patch/export gates; shared qualified annotations and unrelated facts
retain their identities. No edit applies to the original source automatically.

`inspect_context` and `compare_context_definitions` from
`bijux_ontology.jsonld.contexts` expose bounded effective definitions, including
unused terms and defaults. Supply the processing mode, base, offline resources
and inherited context explicitly; `ContextScope` selects finite property/type
transitions. Uninstantiated nested scopes remain unverified. These comparisons
qualify selected definitions, without claiming compatibility for all documents.
The same operations are available through `WorkerExecutor` as
`context_inspection` and `context_definitions`.

`decode_numeric_json` from `bijux_ontology.jsonld.numbers` qualifies original raw
JSON tokens under an explicit processor, portable-integer or source-exact policy.
Inspect numeric observations and their pointers before conversion. A requested
`project_numeric_json` call with `NumericProjection` needs an explicit supported
datatype and context-free input;
unsupported projections return no checked output. Already decoded Python floats
carry no raw-token precision claim. Ordinary JSON decoding remains unchanged.

`check_term_usages` from `bijux_ontology.jsonld.term_usage` checks direct keys in
selected source-object regions of one `PreparedUsages`. Use `TermUsageRegion` to
require explicit definitions, permit default-vocabulary extension regions, or
require membership through an owned vocabulary view. Findings retain actual key
pointers and expanded IRIs. Scoped contexts, keyword aliases, nullified terms and
opaque JSON retain their processor roles; the policy does not redefine JSON-LD
validity. Select descendants explicitly with existing source selectors.

`check_dataset_layout` from `bijux_ontology.data.layout` checks an explicitly
supplied complete dataset against `DatasetLayout`. Declare open or closed graph
inventory, required graphs (including empty ones), and `ContentPlacement` rules
for selected predicates, types or owned subjects. Findings identify the physical
graph and original statement. Incomplete empty-graph fidelity cannot establish a
closed inventory; pinned or union views do not establish original partitions.

Use `prepare_corpus` and `consolidate_entities` from
`bijux_ontology.data.consolidation` for explicitly supplied repeated descriptions.
`CorpusDocument` selects actual document/graph participation; `PredicateAgreement`
requires exact or selected literal-value agreement, and additive predicates permit
complementary facts. A candidate is returned only after agreement and required
`ConsolidationCheck` callbacks verify. Contributions retain both document origins;
independent blank nodes and physical graph partitions remain separate unless an
explicit union is requested. These are application agreements, not OWL consistency.

RDF/XML input uses a bounded Expat boundary before native RDF parsing. XML 1.0
UTF-8 and BOM-qualified UTF-16 LE/BE preserve base, language and RDF terms. Safe
internal general text entities are supported; external/parameter entities and
other DTD constructs are explicit capability failures. Entity declarations
combined with RDF XMLLiteral content are unsupported because the qualified native
parser cannot preserve namespace-entity references inside that literal. Expanded
content, nesting and normalized bytes share the operation limits. Malformed XML
retains original parser coordinates and their column unit: Unicode code points
for UTF-8, UTF-16 code units for UTF-16. Native RDF failures without coordinates
remain spanless; an unqualified caller-supplied column unit stays absent.

`inspect_annotation_graph` from `bijux_ontology.vocabulary.annotations.graphs`
reads selected direct, axiom and recursively qualified annotations from one owned
physical graph. It retains exact RDF terms, attachment paths and shared blank
identities. `compare_annotation_graphs` compares the joint selected graph and
attributes changes where correspondence is established. `adapt_obo_record`
requires the original complete OBO attachment inventory. `AnnotationValueEdit`
uses `author_vocabulary` with explicit after checks to replace an exclusively
owned leaf qualifier; shared, ambiguous or nonleaf edits remain unverified.
Referenced resource bodies stay opaque, and export still requires acceptance.

`check_assertion_witnesses` from `bijux_ontology.owl.assertions` checks selected
finite individual/property assertions without a DL backend. It retains original
premises for irreflexive, asymmetric, disjoint-property, negative-assertion and
identity/difference contradictions. Functionality, inverse functionality and
supported named-class keys can establish equality; different IRIs alone do not
establish difference. Keys require a shared witness for every property, with named
object fillers. Literal values use the selected supported OWL datatype map while
preserving RDF lexical terms. Results distinguish `contradiction_found`,
`no_witness_in_selected_scope` and `incomplete`; none is a general consistency
verdict. Unsupported entailments and stopped work remain explicit.

`to_sarif(report)` and `export_sarif(report, destination)` from
`bijux_ontology.interfaces.exports.sarif` produce local SARIF 2.1.0 with Errata 01.
Use `bijux-ontology project project.json --sarif --output findings.sarif` for the
same report through the CLI; `--artifact-root` qualifies relative artifact paths.
Native findings, unresolved obligations and failed execution retain separate
meanings. Policy acceptance never changes invocation completion or raw findings.
Parser regions retain their qualified column units; unsupported unit conversion
uses line-only regions and preserves the original span in native properties.
Actual `ShaclResult` findings and related locations retain their original report
graph. Python file export requires a new destination; no export uploads data.

RDF extraction, IRI remapping and transformation rechecks honor the prepared
source's physical graph or explicit union. Hidden partitions cannot introduce
mapping collisions or enter callback snapshots. Renamed graph selections and
selected empty graphs remain explicit. Successful syntactic checks do not grant
ontology or publication approval; required after checks remain separate.

`check_annotation_hygiene` from `bijux_ontology.vocabulary.annotations.hygiene`
checks explicit `HygieneRule` policies over inspected annotation paths. Whitespace,
character categories and Unicode normalization are opt-in; findings retain
half-open codepoint spans in original literal text and the actual Unicode version.
An unavailable requested Unicode version or unexercised required policy stays
unverified. No language, script or multiline restriction is imposed by default.

`repair_annotation_hygiene` accepts exact `AnnotationLiteralRepair` witnesses and
required caller-selected publication checks. It replaces the literal and its
actual OWL target anchors while preserving reifiers, nested qualifiers, language
and datatype. Attachment and lexical checks always rerun; collision, language
and other publication obligations use ordinary `MigrationCheck` callbacks.
Missing checks, shared ownership and anonymous identity correspondence that
cannot be established prevent approval. Export requires explicit acceptance.

`bijux_ontology.interfaces.adapters.sdrf` connects the maintained SDRF ontology
validator to prepared local RDF terminology. Declare `SdrfOntology` resources with
explicit accession and label/synonym predicates, then use
`prepare_sdrf_terminology` and `check_sdrf(parsed_table, effective_schema, terminology)`.
Inputs are actual `SDRFDataFrame` and effective `SchemaDefinition` instances,
including ordinary registry composition. Native diagnostics retain original
cells and physical ordinals. The local provider performs no network requests.

Install the separately qualified optional consumer with all dependencies:

```sh
uv pip install 'sdrf-pipelines[ontology] @ git+https://github.com/bigbio/sdrf-pipelines.git@4ba20f5778b0b52055b71bad0a7654cc60dacbe6'
```

This qualifies version `0.1.7.dev72`; other versions remain unsupported. Core-only
installations can prepare terminology; requesting the absent consumer raises a
resource error with the installation requirement. No consumer runtime is mandatory.
An explicitly closed, completely indexed catalogue can establish absence. Raw
unqualified imports and imports in selected lookup resources retain uncertainty;
observed positive ancestry remains usable. Partial indexes cannot establish
unique accession identity. Custom ontology validators and unsupported policies
stay unverified. Native in-process validators retain their ordinary execution
boundary; adapter budgets do not preempt arbitrary consumer code.

`bijux_ontology.vocabulary.schemaorg` provides an opt-in dialect over supplied
release bytes. `prepare_schemaorg(RdfSource(...), release="30.1",
namespace=URIRef("https://schema.org/"))` records the exact byte hash and owns the
parsed release. Supply its logical document IRI and acquisition limits; no URL is
fetched. `schemaorg_property` retains original includes, inverse and lifecycle
assertions. Multiple includes targets are alternatives. Exact extension choices
and finite IRI mappings remain explicit; HTTP and HTTPS names stay distinct.

`check_schemaorg` checks selected actual subjects and predicates. Its default
`published` report is advisory guidance. Choose `mode="application"` for required
endpoint expectations, `text_fallback="allow"` to permit text explicitly, and
`lifecycle="reject"` to diagnose a superseded property with its supplied replacement.
The raw guidance report remains available when application fallback is allowed.
Named class ancestry is supported. Native scalar bridges cover Text, Boolean,
URL, Integer, Number and Float; unsupported datatype encodings, list/container
expectations, inherited-only property hints and missing class proof stay unknown.
These are selected application rules, with no OWL consistency claim. Anonymous
subjects require actual witnesses from the supplied `PreparedSource`.

`bijux_ontology.vocabulary.obo.resolve_alternative_ids(view, identifiers)` reads
only asserted `oboInOwl:hasAlternativeId` aliases in the selected inventory.
Choose exact literal matching, absolute IRI interpretation, or CURIE expansion
with an explicit `IdentifierRegistry`. Each request retains all canonical
candidates, original assertions and deprecation state. Different canonical
subjects remain ambiguous; a duplicate alias assertion never selects a winner.
Labels and replacement hints do not establish aliases. Missing or incomplete
inventories remain unknown, and exhaustion keeps observed candidates with
incomplete status. Resolution proposes identities without rewriting caller data.

Use `bijux_ontology.workflows.projects.acquisition.acquire_bundle` for explicit
HTTP resource preparation. Supply `AcquisitionRequest(member, url)`, an
`AcquisitionPolicy` with exact allowed origins and a new ZIP destination. The
ordinary project bundle skeleton declares resource identities, graph selections
and unchanged check jobs. Redirects, transitive imports, observed context links,
media and compressed/expanded sizes remain bounded; requested and effective URLs
stay distinct. Inspect `result.report`, `result.resources` and `result.archive`.
Preparation proves selected resource availability; run ordinary bundle checks to
qualify the application. Existing checks never acquire missing resources.

`workflows.projects.representations.check_representations(resource, policy=...)`
checks a `PublishedResource` with explicit `RepresentationVariant` Accept,
media/profile and effective-URL bindings. Actual context/import closure and RDF
comparison share the acquisition limits and owned worker lifetime. Inspect each
variant's response and raw report. An explicit `graph_name` verifies only that
projection; complete agreement includes required empty named graphs.

The console equivalent is `bijux-ontology acquire project-directory --request
ontology.ttl https://example.org/ontology --origin https://example.org
--destination resources.zip --timeout 30 --json`. Repeat `--request` and `--origin`
explicitly. Native RDF works in core; JSON-LD context processing uses `[jsonld]`.
Directory input aliases, including missing declared members, reject before HTTP.
Fetch/closure and final ordinary bundle validation retain separate documented
Limits boundaries inside the owned worker deadline. Host capture/report adoption
are separately bounded; filesystem I/O keeps its ordinary OS execution boundary.

Use `bijux_ontology.workflows.ontology.publication.SkosPublication` and
`compile_skos_publication(source, specification)` to publish selected concepts,
multilingual labels, XL resources and ordered collections from one selected
physical graph. Declare `mapping_boundary="retain-reference"`, `"include-target"`
or `"omit"`, and supply nonempty ordinary `MigrationCheck` publication checks.
The result is a checked `MigrationPlanResult`; use `export_migration_plan` with
explicit acceptance for portable RDF. Excluded collection members block complete
publication; mapping exclusions remain explicit. Shared labels, list order and
authored membership direction are retained. This certifies the selected
publication contract, without claiming OWL equivalence or global minimality.

`bijux_ontology.workflows.ontology.taxonomy.project_taxonomy` publishes a selected
named subclass hierarchy through the same checked publication/export boundary.
Supply source and hierarchy snapshots, `TaxonomyProjection` and an explicit
`hierarchy_basis="asserted"` or `"supplied-inferred"`; the latter declares supplied
evidence rather than running inference. Choose `same-iri-punning` or complete
`ClassConcept` bindings with an ordinary source-class annotation predicate.
Required consumer checks validate the actual SKOS result. Competing preferred
labels and unrepresented selected expressions block publication until exact
source-label choices or explicit omission policies resolve them. Original
assertions, identity bindings and omission witnesses remain inspectable.

`bijux_ontology.workflows.upgrades.shapes.assess_shape_upgrade` compares two
selected `ShapeProfile` releases on the same owned `ShapeSample` data. Inspect
introduced/resolved/persistent/uncertain finding groups and completed target
changes alongside both original `ShaclResult` objects and their raw RDF reports.
Named shape renames can use explicit `ShapeCorrespondence`; exact failing values
and severities retain distinct identities. Pure anonymous shape/path-label churn
can qualify through bounded RDF isomorphism; edited anonymous attribution stays
unknown. Lost targets cannot resolve prior failures or certify compatibility.
Comparison agreement is finite representative evidence, separate from raw data
conformance and universal profile refinement.

`bijux_ontology.vocabulary.mappings` reads and exports SSSOM TSV through the
optional `[sssom]` extra and maintained schema/parser/writer. Supply explicit
`mapping_set_id` and `license`; original rows, duplicates, lexical cells and
metadata remain inspectable. `check_mappings` applies a `MappingPolicy` to exact
subject/object `VocabularyView` scopes, independently of interchange validity.
SKOS mapping directions remain distinct; confidence does not establish truth.
`vocabulary.mappings.proposals.propose_lexical_mappings(source_index, target_index)`
joins prepared lexical indexes with equal case policies. Original predicates,
languages, scopes and homonyms remain reviewable; ranks order spelling and field
priority, never semantic confidence. `approve_mapping_proposals` requires actual
candidate references, explicit predicates, justifications, qualifications and an
`IdentifierRegistry`. Only selected approvals enter the native SSSOM producer;
incomplete candidate generation cannot authorize assertions. Host preparation
and interchange can share a `Budget`; native worker limits remain independent.
Endpoint roles, cardinalities and conflicts require explicit consumer policy.
Modifiers, composed/literal endpoints, extension semantics and unmapped sentinels
remain unknown; sentinels cannot satisfy required cardinality. The supported TSV
body excludes hash characters because maintained comment handling can lose data.

`bijux_ontology.vocabulary.mappings.composition.compose_mappings` joins explicit
intermediate IRIs in two owned mapping sets or raw `MappingRead` results. Supply
finite `MappingCompositionRule` records: only exactMatch transitivity has a SKOS
basis; other supported chains require an explicit application policy. Results
retain every original row, path, qualifier and conflicting target. Uninterpreted
qualifiers block affected derivations; confidence is never multiplied. Candidates
establish neither OWL identity nor a newly authored SSSOM mapping set.

`workflows.upgrades.mappings.reconciliation.reconcile_mappings` groups exact
row content and interpretation context across supplied mapping releases. Every
original occurrence remains available; confidence and differing qualifiers stay
independent alternatives. `apply_mapping_resolution` requires an explicit
`MappingResolution` bound to that review, retained group positions, authority,
reason and output identity/license. The native SSSOM producer and selected
endpoint/cardinality/conflict policies rerun before approved output is available.

`workflows.upgrades.mappings.impact.assess_mapping_impact` compares supplied
releases against explicit directional `MappingUse` occurrences, finite record
coverage and selected qualifications. Used row loss, newly ambiguous cardinality
or newly incompatible relations identify affected records. Unused edits and
unselected metadata remain informational; incomplete baseline or consumer scope
stays unknown. Selected identifier qualifiers need qualified prefix interpretation;
selected `curie_map` metadata compares the actual retained prefix maps.

`documents.patches.rdf.project_rdf_patch` turns explicitly paired ground RDF
replacements into checked compact-source edits. Supply the exact original base,
form and retained-use contracts and a unique or explicit source-occurrence policy.
Typed lexical strings and named forward/reverse references preserve their compact
structure. A complete RDF re-expansion must prove the requested delta and all
unedited assertions before a `DocumentPatch` or operations are returned. Ambiguous
aliases, stale bases and unsupported anonymous correspondence withhold output.

`workflows.reproductions` captures runnable context, emission, SKOS and supported
SHACL Core requests using ordinary public declaration formats. Run the request,
bind `failure_signature` to an actual complete nonempty finding, then call
`reduce_failure`. Bounded structural deletion retains the same required scope and
native contradiction. Inspect the best verified runnable request separately from
search completion; interrupted searches make no global-minimum claim. Missing
worker receipts forfeit delegated allowances and stop further attempts. Anonymous
SHACL finding correspondence and composite paths remain unsupported.

`bijux_ontology.data.cube.check_cube` checks supplied Data Cube structures,
observations, slices and code lists in one physical graph. `complete=True`
declares the supplied observation inventory; named code-list closure is separate.
Its owned normalized view preserves the asserted input. Inspect all IC0–IC21
coverage receipts: IC0 supports builtin literals and direct datatype ranges;
unsupported entailment or comparisons, missing DSDs and open code lists remain
unknown. Coordinate checks reuse native compound keys and literal comparison.

`workflows.upgrades.instances.compare_instance_releases` compares selected
instance records independently of schema changes. Supply physical entity scopes,
explicit identity pairs and `InstanceFieldPolicy` paths for immutable, append-only
or permitted changes. The reusable exact dataset patch retains lexical changes
even when a selected literal-value regime accepts them. Ordered lists and shared
blank support remain significant; ambiguous anonymous identity stays unknown.
Append-only protects selected roots and reachable blank support in their graphs;
named child descriptions need their own entity or path selection.

`bijux_ontology.data.projection.project_records` reads explicit `ReadBinding`
subjects and `ReadField` paths in one selected physical graph. Immutable records
retain exact terms, ordered list members and source-qualified reference handles.
Raw terms are the default; opt into checked string, integer, boolean or Decimal
conversion. Unsupported conversions retain their terms with an unknown report.
Use `construct_projected(result, factory)` for trusted application construction
only after every declared check succeeds. The factory owns its runtime and output;
this read contract makes no serializer inversion or ORM promise.

`bijux_ontology.data.extraction.extract_records` copies finite selected paths,
actual ordered-list structure and explicitly requested child descriptions.
`RecordField` wraps a raw `ReadField`; endpoints without children stay
reference-only. Graph selection remains physical, sharing survives in the owned
output, and receipts retain input ownership. `record_plan_from_shape` supports
finite property/node path plans; recursive and other unsupported obligations
withhold a complete plan. Extraction does not establish SHACL conformance.

`workflows.ontology.modules.check_module_wiring` checks named public exports and
selected class/property/datatype obligations against explicitly bound provider
views. Each requirement needs exactly one binding; lookup resources and other
candidates cannot supply a missing guarantee. Native witnesses retain their scope,
and missing supported relationships remain unknown. Agreement covers this finite
interface; provider dependencies require separate checks and whole-ontology
equivalence or consistency needs its own evidence. Asserted signatures need no Java.

`bijux_ontology.data.analysis.prepare_annotation_population` owns explicit positive
entity annotations, eligible background, selected terms and typed child-to-parent
propagation. Duplicate rows and diamond paths count each eligible entity once per
ancestor; unannotated entities remain in the universe. `resnik_similarity` returns
base-two information content and all tied shared-ancestor witnesses.
`resnik_term_sets` explicitly averages the two directional mean-best scores.
Results carry actual corpus/hierarchy fingerprints. Unknown terms, zero frequency,
cycles, disconnected pairs and incomplete propagation withhold required scores.

`enrich_annotations` uses that population, an explicit study and a prespecified
term family. Install `[analysis]` for SciPy Fisher tests with the `greater`
alternative and whole-family BY or explicitly qualified BH correction. Results
retain contingency counts, population/method identities and exclusions; partial
execution withholds adjusted values. Sampling/dependence declarations concern the
supplied sets and do not establish biological conclusions.

`bijux_ontology.models.persistence.compile_model_artifact` captures ordinary
`RdfSource` text/bytes, declarations and closed context resources as inert data.
Compute `model_preparation_identity` from current inputs before loading; pass it
and the compiler output's SHA256 from a trusted application/build manifest to
`load_model_artifact`. A fresh `PreparedModels` reuses named source/index
preparation and reruns declaration and output checks. Changed resources, contexts,
selections, declarations or installed semantic code invalidate reuse. Unknown
formats/extensions are rejected; successful submissions and callables are never saved.

`validate_shapes(..., trusted_targets=True)` and `ShaclSession` independently
opt into supported SPARQL targets and parameterized target types. The existing
owned worker enumerates each selected root once, then validates ordinary Core
constraints and coverage against those exact focus nodes. Target types use
maintained required/optional binding and Core datatype/class/node-kind guards.
Constraint queries and function catalogs retain their separate trust controls;
unselected targets and pair-mode targets stay inactive. Unsupported target kinds,
missing required bindings, forbidden retrieval and incomplete enumeration cannot
verify. Empty targets may conform while failing an explicit coverage minimum.

`bijux_ontology.shacl.rules.materialize_rules` runs explicitly trusted selected
TripleRules or controlled SPARQL CONSTRUCT rules. Inspect separate asserted and
derived RDF, mode and completed pass count. `ordered-pass` executes one complete
ordered program; `global-fixed-point` repeats the entire program until a full
pass adds no exact RDF terms. Fresh blank-node growth cannot fake convergence.
The initial profile requires one physical graph and named selected owners with
distinct orders; unsupported rules, exhausted closure and missing required
`MigrationCheck` callbacks prevent a verified publication candidate. The result
uses the ordinary checked migration export and explicit acceptance gate.

`workflows.upgrades.values.compare_controlled_values` checks the same actual
stored record values against explicit before/after `ValueEnumeration` contracts.
Reuse `select_concepts` for scheme, subtree and retirement policies. Inspect exact
source statements and introduced/persistent/resolved/uncertain membership; a new
break requires known old permission and known new exclusion. Open or incomplete
membership remains unknown, and unused vocabulary changes do not create data
breaks. `ValueReplacement` evaluates an advisory proposal without rewriting data
or approving a migration. Every selected record/field pair requires an occurrence.

`bijux_ontology.rdf.entailment.prepare_entailment(source, regime="rdfs")`
or `regime="owl-rl"` uses maintained `owlrl` from `[shacl]` as an explicit
preparation step. Inspect separate asserted, derived and effective snapshots,
final generalized consequences, regime and backend. Pass a verified effective
snapshot to ordinary `validate_shapes`; emitted JSON requirements stay separate.
Physical graph pins remain scoped; explicit unions are flattened. For blank
graph names, pass the actual returned `graph_name` to snapshot/check operations.
Unsupported datatypes, unresolved imports and incomplete closure yield no checked
derived/effective artifact. This finite rule closure does not certify OWL 2 DL.

`bijux_ontology.shacl.partitions.check_partitions` takes a `PartitionPlan`
selecting primary records and read-only support graphs from one owned dataset.
Every `PartitionObligation` chooses `primary` or `merged` effective assertions;
primary output fields cannot borrow support assertions. Support subjects do not
become submissions. Cross-record constraints conservatively require declared
complete primary data and actual complete target inventory/coverage. A partial
conforming backend result cannot establish that scope. Inspect raw results,
discovery and exact graph selectors alongside the required aggregate report.

`workflows.upgrades.matrix.check_compatibility_matrix` checks explicit application,
context and ontology combinations for every required `ConsumerVersion`. Supply
each consumer's actual `ApplicationVersion` contract/corpus and closed context
resources. Inspect original single-version checks, exact outputs and deployable
cells; other known combinations and unused consumers remain visibly untested.
Missing versions, inputs or representative corpora keep required cells incomplete.
A passing majority cannot hide a failed or unknown selected obligation.

`bijux_ontology.rdf.stores` creates/reopens sealed native disk snapshots from
local N-Triples/N-Quads. `query_snapshot` runs bounded local ASK/SELECT with an
explicit `QueryScope`; `vocabulary.storage` inspects selected terms and checks
used declarations without copying the entire dataset. Native lexical changes,
such as integer `01` normalization, are required unsupported. Snapshot reads
verify their closed file revision; restart rechecks content hashes and inventory.
`materialize_snapshot(..., max_triples=...)` explicitly bounds other consumers'
in-memory projections. Nonempty-only graph inventory cannot prove empty named
graph coverage, and unsupported physical graph identities cannot be collapsed.

`rdf.literal_values.json.check_json_literal(literal, JsonLiteralContract(...))`
checks opaque `rdf:JSON` values with explicit `JsonRequirement` paths, kinds,
exact Decimal bounds and ordered array requirements. Duplicate keys, nonfinite
numbers and malformed JSON are invalid; nested diagnostics use JSON pointers.
The original RDF literal stays unchanged: parsed value equality proves no RDF
term equality. Optional `jsonschema` checks require an explicit finite JSON Schema
2020-12 dialect and resource identity; unsupported schema features remain unknown.

Install `bijux-ontology[markup]` for
`rdf.literal_values.markup.inspect_markup_literal` and `compare_markup_values`.
The explicit `xml-exc-c14n-with-comments` regime compares self-contained XML
fragments using maintained lxml/libxml2, preserving inline namespaces, language,
base, comments and processing instructions. DTD/entity declarations and external
resolution are outside the supported profile. HTML supports fragment inspection;
it has no supported canonical value comparison. Original RDF terms stay exact,
and native execution has an owned deadline and cancellation lifecycle.

`documents.relocation.relocate_document(..., old_url=..., new_url=...,
policy="pin-old-base", requirements=...)` prepares an owned JSON-LD relocation.
It preserves selected source-form and optional occurrence contracts, then compares
whole resolved RDF. Root context references keep their original effective bases;
null resets and nested/scoped contexts remain unsupported. `export_relocation`
returns checked bytes for the ordinary atomic `write_export` protocol only after
all required preservation checks verify. Intended identity changes require an
explicit migration rather than this preservation policy.

`jsonld.streams.check_jsonld_records` lazily checks explicit `JSONLDRecord` text or
byte frames with independent bases, blank-node scopes and record budgets. One
captured context owner is shared; only the current `last_result` is retained.
An ordinary callback receives each prepared RDF source and its budget. The final
stream report covers actual input exhaustion; malformed tails and early close
remain incomplete, with cross-record obligations explicitly excluded.
`interfaces.execution.record_streams.AsyncJSONLDRecords` bridges the same iterator
with one admitted pull. Cancellation waits for that pull before joining its
private thread; caller producers, loaders, callbacks and PyLD remain cooperative.

Install `bijux-ontology[jsonpatch,jsonld]` for
`documents.patches.apply_document_patch`. Supported add/remove/replace/test edits
require an exact `expected_base`, selected before/after forms, changed meanings
and explicit `RetainedUsage` path pairs. Array rebinding additionally needs the
same actual named parent. Failed base/tests reject publication; unsupported
operations or uncertain identity remain unverified. `export_document_patch`
returns immutable checked JSON bytes for ordinary atomic `write_export`.

Use `interfaces.adapters.csvw.extract_csvw` with optional `[csvw]`, inline
single-table metadata, exact URL-to-CSV-text resources and an explicit metadata
base. Maintained CSVW tooling interprets explicit subject/predicate/value URI
templates, scalar string/integer/decimal/boolean/anyURI cells, column null/default,
inherited required policies, unordered separators and scalar primary keys.
Unsupported rules or missing resources prevent complete extraction. Returned
cells retain original text, row, column, physical start line and emitted RDF;
`CsvwCheck` callbacks inspect the owned source. Acceptance requires the aggregate
report. Shared subjects are legal without declared uniqueness. Decimals retain
native exact values independently of caller Decimal settings; split/default
amplification is bounded before native parsing. No external resources are fetched.


`bijux_ontology.shacl.skos_profiles` exports selected plain-label policies as
portable typed SHACL. Supply `SkosProfile` with explicit named targets, required
languages and rules, then `compile_skos_profile(..., trusted_sparql=True)` for
exact label integrity and language coverage. Execute the returned components
with the existing trusted SHACL APIs against the same asserted physical graph.
Keep the translation report: unsupported rules, XL-dependent combinations and
unpreserved scope remain untranslated, even when an RDF artifact is available.

`bijux_ontology.models.temporal.check_temporal` checks concrete emitted text fields
using explicit `TemporalInterval`, `TemporalOrder` and `RelativeTemporal` rules.
The `[xsd]` extra supplies maintained calendar arithmetic. Supported timezoned
dates/dateTimes retain calendar months, exact chronology and explicit open ends;
partial dates, absent timezones and excess precision remain unknown. Only relative
rules use a supplied clock. Input spellings remain in the returned owned document.

`bijux_ontology.interfaces.adapters.vocabulary_bindings.controlled` composes
selected subsets and explicit enumerations with union, intersection and difference.
Open-set absence cannot establish exclusion. `compile_bindings` prepares finite
text-discriminator cases; `select_binding` captures an actual record without a
fallback. Reuse its owned `.document`, or retain ordinary emission conditions and
SHACL `hasValue` trait guards when checking other records. An enumeration alone
does not reselect a changed trait. `closed_values()` requires complete evidence.

`bijux_ontology.shacl.external_reports.ingest_external_report` reads an ordinary
external SHACL report with its raw graph, typed findings and nested details.
Producer scope/backend claims remain descriptive; even a conforming report cannot
certify fresh local execution or omitted coverage. `aggregate_shacl_reports` keeps
external and local report scopes separate, including equal blank labels, and
retains required unknowns and externally asserted failures.

`bijux_ontology.models.quantities.prepare_quantities` prepares explicit unit-code
or IRI bindings for repeated checks with optional `[quantities]`. Use
`QuantityContract` value/unit paths and Decimal bounds in a declared reference unit.
The supported atomic names are meter, millimeter, centimeter, kilometer, second,
minute, hour, kelvin and degree_Celsius. Unsupported or inexact conversions remain
unknown. `.check(record, contract, convert=True)` exposes a separate `.converted`
record only after intrinsic rechecks; `.source` preserves the actual input.

`bijux_ontology.workflows.upgrades.lifecycle.check_lifecycle` checks explicit
ordered `Release` snapshots and selected IRIs under a `LifecyclePolicy`. Supply
`HistoryCoverage` for the exact interval and optional `Tombstone` records.
Reserved logical predicates and occurrence positions are caller policy; retained
assertions and prohibited reactivation are distinct from ordinary label changes.
Raw observations and adjacent differences remain available. Missing history or
anonymous attribution cannot certify permanent identifier stability.
Each release can bind its own `imports` to supplied local resources. Those
resources qualify the authored import closure; lifecycle observations still
cover the selected physical local graph. Missing resources remain unverified.

`bijux_ontology.shacl.remediation.propose_remediations` accepts explicit
`RemediationStrategy` callbacks keyed by named shape and constraint-component
IRIs. Each receives a private source and actual findings, returning a proposed
snapshot or `None` for unresolved content. Alternatives carry exact patches and
full required SHACL rechecks, including original named target obligations;
additional `MigrationCheck` contracts can reject application-specific changes.
Inspect each candidate and use the existing explicit migration export gate.
Callbacks are trusted in-process Python code; native checks have a deadline.

`bijux_ontology.workflows.ontology.materialization.prepare_materialization` owns
asserted input and an `EntailmentPlan` or `RulePlan`. Its session applies exact
asserted patches with an `expected` revision receipt, then recomputes the complete
maintained closure. Removed supports and replaced programs cannot retain stale
derivations; explicitly asserted duplicates remain asserted. Only a fully checked
update publishes a new revision. Failed updates expose no replacement closure.
Ambiguous anonymous correspondence stays unavailable; rule callbacks remain
ordinary trusted Python. Close the session when its retained inputs are no longer needed.

Install `[shex]` for `bijux_ontology.shex.validate_shex` with explicit `ShexSchema`
text and typed `ShexBinding` obligations. The qualified native profile accepts
ShapeDecl ShExJ, absolute IRIs, supplied local imports and positive structural
expressions, including OneOf, inverse, CLOSED and EXTRA. Datatype constraints
support strings and whitespace-free integers of at most eighteen decimal digits.
Other dialects, scalar domains, actions and unsupported features remain unknown.
Each obligation retains its actual native result; incomplete work cannot verify.
The installed command accepts `shex data.ttl schema.json --schema-iri urn:schema
--binding urn:record urn:shape --schema-import urn:child child.json --json`.
It uses the same owned native execution and completed-result recovery as Python.

`bijux_ontology.workflows.ontology.obo.check_obo_publication` applies an explicit
`OboPublicationProfile` to named owned terms in a `Release`. Select ordinary
annotation contracts, native qualified definition/synonym rules, referenced subset
metadata and supplied-interval lifecycle policy independently. Imported support
cannot fill missing local annotations; each release binds its own local imports.
Explicit term/namespace exclusions remove support terms from the owned selection.
The result retains scoped records and selected subset witnesses. This finite
opt-in convention does not certify every OBO Foundry recommendation.

`interfaces.adapters.rdfa.extract_rdfa(content, document_url, runtime=...)`
extracts HTML5 RDFa through an explicitly selected optional Node runtime. Call
`prepare_rdfa_runtime(directory)`, run ordinary `npm ci` there using the shipped
lock, and supply `RdfaRuntime(node_executable, directory)`. The maintained parser
retains base, language, prefix, reverse and list semantics; its owned graph works
with ordinary record and vocabulary checks. JSON-LD extraction remains separate.
Source association is document-level; unsupported XHTML5, unresolved copy
expansion, empty extraction and failed bounded execution withhold the source.

`rdf.answers.read_answers` and `write_answers` preserve finite ASK/SELECT JSON,
XML and TSV artifacts, including exact RDF terms, unbound cells, duplicate rows
and result-scoped blank sharing. `compare_answers` selects bag, set or sequence
semantics; variable names align independently of header order unless strict
column order is requested. Exact RDF identity is the default; native scalar
value regimes require explicit row and blank correspondence. CSV output needs
explicit lossy opt-in. A valid supplied artifact does not establish external
query completeness; require that separate caller evidence when necessary.
XML-inexpressible RDF literal characters withhold output as unsupported.

`data.query_usage.read_query_usage(text, source=...)` uses maintained SPARQL
query/update translation to resolve class, predicate, datatype, graph, function
and data references. Locations identify algebra positions, without source-span
claims. `check_query_usage` checks actual vocabulary roles and deprecation. Pass
`query_usages=(usage,)` to `index_uses` or `assess_upgrade` to expose those consumer
dependencies. Dynamic vocabulary positions, unsupported composite class paths
and unestablished binding scopes retain required unknowns beside known constants;
comments and string contents do not become predicate declarations.

`rdf.containment.check_simple_entailment(data, template, data_graph=...)` checks
a finite graph template against one complete selected physical graph using
maintained SPARQL basic graph matching. Template blanks are existential variables:
repeated occurrences share a binding, and distinct variables may coalesce.
The returned substitution refers to actual template and data-owned terms. Ground
literals retain exact RDF lexical identity; extra data is allowed, without RDFS,
datatype or OWL inference. Exhaustion leaves entailment unknown. An empty template
is mathematically entailed and makes no nonempty coverage claim.

`rdf.workloads.qualify_workload(source, queries, scope=QueryScope(...))` runs
a supplied finite ASK/SELECT corpus through maintained RDFLib and Pyoxigraph
workers. Each result records actual engine/Python versions, asserted entailment,
default-graph configuration and native binding policy. Exact answer bags/sets
reuse `rdf.answers`; duplicate changes and actual default/binding differences
remain visible. Bag qualification makes no ORDER BY sequence claim. Unsupported
extensions, nondeterminism, native term loss, failed execution and exhausted
work cannot establish portability. Qualification applies to the supplied corpus
and configurations, with explicit row and cumulative execution bounds.

`data.discovery.discover_contracts(source, records, fields, shape=...)` observes
an explicit finite record/path population. Candidates retain original endpoints,
all type/datatype alternatives, per-record counts including zero and exceptions.
Native declarations and independently authored SHACL shapes are checked against
every selected record. `accept_candidate(..., accept_observed_bounds=True)` makes
the qualified observed envelope an explicit application policy; `check_contract`
then checks held-out records without retraining it. Empty, unsupported, incomplete
or invalid observations cannot authorize that policy. Samples establish no
universal requiredness or maxima, and optional SHACL absence remains unverified.

`rdf.reification.conversion.convert_reifications(source, selections, contract=...)`
maps selected complete RDF 1.1 statement descriptions to actual RDF 1.2 reifiers.
Choose the explicit qualified Pyoxigraph 0.5.11 or 0.5.12 contract. Distinct reifier
resources, annotations, graph inventory, shared blanks and separately asserted
triples remain distinct; conversion never asserts a described triple. The owned
candidate accompanies native N-Quads with empty-graph inventory and established
blank correspondence. `restore_reifications(candidate)` derives and checks its
inverse. Independently parsed modern datasets, nested triple terms and ambiguous
descriptions remain outside this reversible profile; it makes no OWL equivalence
claim.

`interfaces.adapters.dcat.prepare_catalog_profile(Dcat3Profile(...))` prepares
reusable DCAT 3 shapes for explicit application choices. `check_catalog` checks
selected catalog, dataset, series, distribution, service, record and period roles
against actual SHACL and supplied theme/license vocabularies. Linked records need
explicit selection; exact permitted external service IRIs are exceptions. Minima
belong to the chosen profile. Shared distributions and absent download URLs are
allowed when the selected access rule is satisfied. Select historical graphs
explicitly; unsupported required extensions, incomplete vocabularies and missing
related populations withhold complete profile qualification.

`interfaces.adapters.ontolex.read_lexicon` reads explicitly selected OntoLex Core
entries, forms, written literals and separate senses against a supplied vocabulary
view. `lookup_written` ranks language and form roles while preserving ties and
sense identities. `write_lexicon` reconstructs the typed Core assertions and retains
every other source graph, its original selection and resource metadata. Required
missing references, unsupported extensions and caller-selected arity violations
remain visible. `project_labels` explicitly reports omitted form/sense structure
and keeps the full lexicon available; labels never become unconditional synonyms.

`interfaces.adapters.xkos.classifications.read_classification` projects ordered
levels, exact codes and distinct generic/partitive relationships from one selected
physical graph. `check_classification` applies explicit level, code and tree
policies. `PartitionContract` requires a supplied finite population and complete
assignments for coverage; authored exhaustive/disjoint claims alone prove no
real-world partition. Blank level selectors must retain their owned witnesses.
The finite profile follows [XKOS 1.2](https://docs.ddialliance.org/XKOS/1.2/).

`interfaces.adapters.xkos.correspondences.read_correspondence` preserves complete
association groups against two supplied `ClassificationRelease` inventories.
`write_correspondence` retains annotations, backing graphs and the original
physical selection without inventing pairwise exact matches. `assess_assignments`
requires a caller-owned `RecordConversion` for each actual record/group choice;
a split never allocates counts or silently selects a destination category.

Install `[frictionless]` for the pinned Frictionless 5.21.0 Data Package v1 CSV
profile. Use a separate environment from `[oak]`: their native dependencies
require incompatible chardet versions. The workspace lock declares that conflict
and installed-package checks exercise both profiles with their real dependencies.
Workspace development requires uv 0.13.1 or later to resolve those profiles correctly.
`interfaces.adapters.frictionless.extract_package` consumes an unchanged inline
descriptor, exactly supplied package-relative bytes, explicit column bindings and
an existing work directory. The owned worker runs actual native typing, missing
markers, scalar keys and foreign keys; semantic checks run on each selected row.
Cells retain original text, native values and record/column locations. Missing
values emit nothing. Unsupported descriptor semantics and truncated validation
remain unqualified; no remote resource or scientific identifier is inferred.

`interfaces.adapters.biosamples.check_sample` is a read-only `[biosamples]`
consumer. Supply `SchemaResource(uri, version, content)` for the
[BioSamples core at `9e552cf`](https://github.com/EBIBioSamples/biosamples-v4/blob/9e552cf60451ad2b6dad20a1234bb07a111a4948/webapps/core/src/main/resources/schemas/core/sample.json),
a versioned local draft-07 checklist and every referenced resource. Actual
`jsonschema` validation uses an offline registry and asserts date-time formats.
`CharacteristicBinding` explicitly selects ontology declarations, finite allowed
terms, literal texts and units; prose supplies no ontology rules. The result
retains checked sample JSON, resource versions/digests, separate raw schema and
semantic reports, and every characteristic array occurrence. The finite profile
supports the four published core regexes; other regexes, unknown assertions,
recursion and missing resources remain unverified. There is no submission or
implicit checklist acquisition.
