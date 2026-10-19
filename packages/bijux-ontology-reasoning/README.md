# bijux-ontology-reasoning

Optional OWL 2 profile checks and DL diagnosis through pinned OWLAPI 5.5.1 and
HermiT 1.4.5.519 with explicit Java 11 or newer. Imports start no JVM or download; core
bijux-ontology remains usable without Java. Profile membership does not establish
consistency, satisfiability or entailment. Incomplete parsing remains unknown.

Build a bundle from the repository root with Maven and a Java development kit.
Use a fresh bundle directory for each revision; native runtime operations never
compile or fetch dependencies.

```sh
ontology_root="$(pwd)"
mkdir "$ontology_root/artifacts/owl-profile-bundle"
mvn -B -f packages/bijux-ontology-reasoning/java/pom.xml \
  -Dmaven.repo.local="$ontology_root/artifacts/maven" \
  -DoutputDirectory="$ontology_root/artifacts/owl-profile-bundle" \
  -DskipTests package dependency:copy-dependencies
cp artifacts/bijux-ontology-reasoning/java/owl-profile-worker-0.1.0.jar \
  artifacts/owl-profile-bundle/
python3 - <<'PYTHON'
import hashlib, json
from pathlib import Path
bundle = Path("artifacts/owl-profile-bundle")
manifest = {
    "protocol": 1,
    "owlapi": "5.5.1",
    "main_class": "org.bijux.ontology.profiles.ProfileWorker",
    "loader_profile": "turtle-permissive-no-punning-repair-offline",
    "jars": [{"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
             for path in sorted(bundle.glob("*.jar"))],
}
raw = (json.dumps(manifest, indent=2) + "\n").encode()
(bundle / "manifest.json").write_bytes(raw)
print(hashlib.sha256(raw).hexdigest())
PYTHON
```

Pass that printed SHA to `prepare_profile_backend(Path(java), Path(bundle),
manifest_sha256=sha, artifacts=Path("artifacts/owl-jobs"))` from
`bijux_ontology_reasoning.profiles`. Then call
`check_profile(backend, prepared_source, profile="owl2-dl", imports=offline_map)`.
Inspect `.in_profile`, `.violations`, `.loaders` and `.report`; a raw native answer
alone does not qualify the supplied RDF. The finite runtime profile supports
POSIX process ownership and one physical graph per supplied document. Missing
imports, unavailable runtimes and exhausted execution cannot pass. Original
anonymous identities shared across separately loaded documents are unsupported;
independent equal blank labels stay distinct.

The `documents` API captures original OWL/XML, Functional Syntax, Manchester
Syntax or OBO with `prepare_owl_document(content, format=...,
document_iri=..., effective_url=...)`. Call
`convert_ontology(backend, document, format="functional", imports=offline_map)`
for a maintained structural roundtrip. Inspect `.content`, `.changes`,
`.lossless` and `.report`. The default rejects lossy output; `loss="report"`
returns an explicit projection while retaining findings. Selected `profiles=`
record actual membership before and after conversion.

Native text uses UTF-8. OWL/XML supports absolute IRIs and qualified fragment
references; unsupported encodings, relative interpretation and anonymous
individual correspondence remain unknown. Only captured imports are available.
Preservation concerns annotated axioms and ontology headers, not original bytes
or exact RDF lexical identity.

Use `axioms.inspect_axioms(backend, source, axiom_types=None)` for bounded,
immutable typed observations of original native documents or selected prepared
RDF. Restrictions, object/data expressions and nested annotations retain their
roles. `.unsupported` records actual selected axioms outside the finite model;
`.exclusions` records families explicitly omitted by the caller. Missing input
structure or incomplete execution prevents a verified complete projection.
Inspection makes no consistency, satisfiability or entailment claim.

`authoring.templates.compile_table_template` executes the documented ROBOT 1.9.11 CSV/TSV
class templates: ID, LABEL, TYPE, named parents, named-property existentials and
plain literal annotations. Supply exact `TemplateColumn`, `TemplateIdentifier`
and role-specific `TemplateTerm` bindings, local declaration/label ontology,
prefixes and nonempty consumer postconditions. Original rows and native cell
diagnostics remain available; every emitted axiom must match the declared rows.
Only complete native profile, roundtrip and consumer proofs publish output.
`prepare_template_backend` requires explicit Java and the official standalone
jar pinned to SHA256
`53cd275c8e4c0dad1fc2c1c34174876795cd7d6b6926e1444adcbb0cb3c7ab26`.
Sealing its 82,708,624 bytes needs an explicit preparation allowance such as
`Limits(max_total_bytes=128_000_000)`; execution uses a separate caller budget.

`inference.consistency.check_consistency(backend, source, named_classes=None)` asks actual
ontology consistency and separate named-class satisfiability questions. A
consistent ontology may contain unsatisfiable classes. An inconsistent ontology
provides no ordinary class proof. `None` selects the actual class signature;
explicit classes must occur in the selected closure. Inspect `.consistent`,
`.consistency_report`, `.classes`, `.loaders` and `.report`. The operation requires
qualified original input, complete supplied imports and OWL 2 DL membership.
Unsupported datatypes, axioms and failed execution remain unknown. The reported
Maven artifact version and native reasoner version are retained separately.

`inference.entailment.check_entailments(backend, source, obligations)` accepts existing typed
`SubClassOf` and `ClassAssertion` requests with supported class expressions.
Each answer is `entailed`, `not_entailed` or `unknown`. A negative assertion uses
an explicit `ClassComplement`; failure to entail a positive assertion never
proves its negation. Actual consistency and complete input qualification precede
all proofs. Unsupported queries retain their positions as unknown answers.

`authoring.patterns.prepare_dosdp_pattern(content, prefixes=...)` captures the JSON subset
of DOSDP's YAML input. `check_patterns(backend, source, patterns, expected,
scope=...)` retains actual generated queries, complete named filler assignments
and the single maintained RDFLib engine answer. Distinct assignments remain
distinct; `extras="allow"` explicitly relaxes only the expected inventory.
Syntactic absence does not prove logical nonconformance. Inferred matching,
nontrivial variable constraints and unsupported pattern fields remain unknown.
Use a separate local [DOSDP 0.20.0 distribution](https://github.com/INCATools/dosdp-tools/releases/tag/v0.20.0)
with `prepare_dosdp_backend`: its pinned manifest has `protocol=1`,
`dosdp="0.20.0"`, `main_class="org.monarchinitiative.dosdp.cli.Main"` and ordered
`jars` entries containing exact `name`/`sha256` pairs. Its 194-jar dependency set
requires an explicit preparation allowance such as
`Limits(max_total_bytes=200_000_000)`; subsequent checks use ordinary limits.
Pattern paths, prefixes and required ontology imports are local and explicit.

`axioms.changes.prepare_axiom_change` applies explicit additions/removals using
the existing immutable axiom classes and maintained OWLAPI factories. Capture
`expected=axiom_base(source)` before planning: both raw integrity and native
resolution context must match. Imports stay read-only; annotated removals must
exist in the root. `compare_axioms` reports structural set changes separately
from RDF differences or logical equivalence. Required `after_checks` use the
ordinary profile, logical or application APIs with the shared budget; passing
syntax alone proves no consistency. Inspect the candidate, then publish unchanged
checked bytes with `write_axiom_change(candidate, path, accept=True)`. A failed
check, stale receipt or interrupted operation cannot replace the destination.

`inference.relationships.query_inferred_contracts(backend, source, queries)` enumerates
named class and object/data property relationships through actual HermiT APIs.
Use `InferredQuery` with existing typed entity values and explicit direct/indirect
hierarchy selection. Native equivalence nodes, inverse expressions, builtins and
matching asserted-axiom owners remain separate; no assertions are materialized.
Class satisfiability is independent of successful enumeration. Complete results
cover the selected named-signature query, not every possible expression; result
caps, unsupported data-property ranges/inverses and unqualified inputs remain
incomplete. The original source and selected physical graph stay unchanged.

`editing.renaming.prepare_role_rename` checks one explicit structural-role rename
across a supplied, connected native module closure. Supply exact `AxiomBase`
preconditions, owned document identities and selected `RoleRename` roles; `all`
is an explicit separate choice. Required annotation IRI positions use
`AnnotationRole` preserve/rename policies. Existing or built-in targets and
required edits in unowned modules refuse the whole transaction. Explicit
`ModuleInterfaceChange` records replace only selected caller-owned interfaces.
Nonempty `ModuleCheck` postconditions receive the complete candidate module set
and shared budget. Returned checked bytes form a reviewable transaction;
publication across multiple destinations remains the caller's responsibility.

`inference.datatypes.check_datatype_obligations` uses actual datatype difference/intersection
satisfiability to check universal inclusion or disjointness. It qualifies original
consistency and an explicitly fresh temporary property; finite samples prove no
universal range relationship. `check_datatype_fields` checks every original value
branch against conjoined global ranges of actual inferred super/equivalent data
properties. Missing ranges, unsupported facets, resource/reverse branches and
application value constraints remain required unknown. Explicit branch selection
through `check_datatype_branches` retains original field identity and indices;
combined consumers own coverage of the remaining branches. Class-context and
model co-emission obligations belong to model compatibility.

`inference.model_contracts.check_model_compatibility(backend, source, declarations)`
checks a finite automatic subset of existing model contracts. Required endpoint
subsumption, possible type satisfiability and declared co-emission contradictions
remain separate answers at original model/field locations. Anonymous intersection
and existential consequences use actual reasoning; literal branches delegate to
the datatype checker. One captured RDF import closure supports both operations.
Unsupported contexts and uncovered scenarios remain unknown. Logical existence
or cardinality never certifies emitted required fields; use ordinary model/output
checking for those obligations.

`inference.instances.check_emitted_consistency(backend, ontology, emitted, bindings)`
checks all statements in the two explicitly selected prepared RDF graphs with
one captured ontology import closure. Inspect `.diagnosis` for global logical
consistency and `.records` for original node participation. Bindings never filter
the graph or identify a uniquely guilty record. Original scopes stay separate
from the derived joint input. Missing explicit existential values and different
IRIs retain ordinary OWL semantics; submission conformance remains a separate
model/output check.

`inference.explanations.explain_reasoning(backend, document, ExplanationQuestion(...))`
returns one bounded maintained owlexplanation 5.0.1 justification for a selected
entailment, named-class unsatisfiability or inconsistency. Original annotated
structural axioms retain their source modules and rendering hashes; these are
not physical source coordinates or universal canonical identities. Standalone
proof bytes are independently reread and reasoned over. Inspect `.underlying`
separately from `.explanation_report`: failed or capped explanation work retains
proved logical findings. Smallest size, subset minimality and complete enumeration
are not claimed. Anonymous support and unavailable tautology justifications remain
unknown; generated signature declarations are separate replay scaffolding.

`integration.check_integration` compares explicitly selected `TheoryCombination`
deployments from separately owned `TheoryComponent` originals. Supply the named
classes and consumer entailments that must hold. Original modules retain their
native import identities; conflicting versions cannot overwrite one another.
Inspect each original, the unbridged union and any explicitly authored OWL bridge
separately. Newly established interactions have rechecked native justifications
and exact source owners; pre-existing failures are preserved without new blame.
Completion covers the selected combinations and obligations only; unselected
subsets, absent imports or exhausted explanation work receive no certificate.

`bridges.check_mapping_bridges` checks explicit `BridgeInterpretation` approvals
of original SSSOM `MappingOccurrence` rows. Choose declared class/property facets
and nonempty protected entailments; an exactMatch relation or confidence never
supplies an OWL interpretation. Original source and unbridged union baselines stay
separate from the joint bridge. Rechecked native justifications correlate candidate
rows through exact original module ownership, preserving duplicate occurrences
and pre-existing failures without new blame. Unsupported qualifiers and endpoint
facets remain unverified; completion covers only the selected obligations.

`extraction.locality.extract_locality_module` runs maintained OWLAPI BOT/TOP/STAR extraction
for an explicit typed signature. Choose flattened imports, retained annotations
and a fresh module identity. Inspect original annotated axiom owners and separate
before/after consumer entailment proofs; only the selected obligations are
certified. Unsupported original axioms prevent a complete certificate. This
operation stays separate from RDF neighborhoods and MIREOT selection.

`editing.reductions.analyze_redundancy` tests explicit unannotated root subclass,
class-assertion and equivalent-class axioms against independent remaining
theories. Proposals need joint verification with `prepare_reduction`: individual
redundancy does not authorize the union. Choose retained annotations and read-only
imports, and supply nonempty consumer entailments. Original inconsistency blocks
proof; publish only verified candidates with `axioms.changes.write_axiom_change`.

`extraction.mireot.extract_mireot` runs maintained ROBOT extraction with explicit same-role
`MireotBoundary` lower/upper pairs and `MireotRetention` property, axiom and
annotation requirements. Supply an import-free Functional source, distinct module
identity/version and nonempty consumer entailments. Actual hierarchy and
before/after proofs qualify the selected scope; missing boundaries or omitted
required content publish no output. Paired Functional and Turtle outputs must
preserve the same annotated axioms and headers. This syntactic import profile
claims no locality or universal consequence preservation; it reuses the explicit
pinned `backend.robot.prepare_robot_backend` runtime and its separate sealing allowance.

`editing.repairs.propose_repairs` checks finite alternatives from one established native
inconsistency justification. Supply nonempty consumer entailments, exact protected
source axioms and any caller-authored `AllowedReplacement` meanings. Imports stay
read-only. Each proposal independently needs native consistency and all selected
consumer proofs; rejected candidates and original inconsistency remain visible.
`search_complete` covers only the configured support/edit frontier;
`enumeration_complete` stays false and minimality is unclaimed. Choose an accepted
sealed candidate explicitly before using the ordinary axiom publication API.

`editing.upgrades.assess_logical_upgrade` compares explicit stable consumer IDs across
owned before/after logical snapshots: entailments, satisfiability, model contracts
and selected role-separation groups. Inspect `.transitions` and finite
`.compatible` separately from the raw `.report`, which retains prior findings.
Changed contract fingerprints stay visible; unmatched consumers remain unknown.
Selected explanations provide actual positive support and cannot erase a proved
regression when unavailable. No ontology-wide compatibility is asserted.

`inference.sessions.prepare_reasoning_theory` captures one exact native/RDF closure and
backend revision. `with sessions.ReasoningSession(theory)` loads and classifies
once, then recomputes supported queries and batches. Capture and `bind` a new
theory for changed imports or backend semantics; external mutable inputs are
not watched. An optional fixed Budget is cumulative across requests.
`session.realize(queries)` retrieves independently qualified named types,
instances, object values, same/different individuals and supported data values.
Use typed `IndividualQuery` records and explicit depth for types/instances. Actual
native node policy remains visible; BY_NAME nodes do not imply sameAs groups.
Data values use native entailment over supplied assertion literals, restricted to
positive named assertions and data sub/equivalent-property axioms. Other logical
data constructs make that family unverified while supported families stay usable.
Returned literal representatives retain their metadata; they do not enumerate all
RDF spellings of equal logical values. Absence asserts no negation and existential
restrictions manufacture no named witnesses.
`session.query_classes(expression, direct=False)` retrieves actual named subclass
and equivalent groups for supported typed intersections, unions, complements and
existential/universal restrictions. Inspect separate expression satisfiability,
global consistency, native node flags and enumeration completeness. Strings have
no implicit label resolver; unsupported or fresh-role requests remain unverified.
Direct and transitive views reuse the same classification without caching answers.
`session.project` exports explicitly bounded subclass/existential domains as
a separate ordinary RDF artifact, retaining quantifiers and unknown pairs.
Its raw required-axiom answers remain inspectable separately from pair coverage;
no class-instance assertions or complete inventory of all fillers is implied.

`editing.modules.prepare_module_refactor` moves exact annotated axioms or explicitly
inlines owned local modules as one candidate set. Supply exact `axiom_base`
preconditions for every original, unchanged required consumer contracts, explicit
import/provider changes and a selected `TheoryCombination` deployment. Actual
consumer import closures and native joint consistency/entailments are checked
before and after; a synthetic all-source union cannot establish consumer access.
Protected axioms and read-only sources remain intact. Namespace transfers are
explicit policy records; no IRI spelling establishes ownership. Legal import
cycles are supported. Any failed obligation withholds all candidate module bytes;
passing the finite selected obligations does not establish full equivalence.
