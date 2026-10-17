# Consumer integration

Applications supply their own vocabulary snapshots, declarations and representative output. Use `TermDeclaration`, `ModelDeclaration`, `FieldDeclaration` and `ValueDeclaration` directly, or extract them through the optional Pydantic/LinkML adapters. The library does not discover application source or infer biological meaning. Runnable examples include ordinary dataclasses and independent catalogue workflows.

Select local, import and lookup scopes deliberately. Use coverage for owned namespaces and membership checks for external terms; lookup documents must not conceal missing local declarations. A required constructor field does not prove RDF emission: declare that guarantee explicitly and check representative serialised output with `check_emission`.

For finite RDFLib namespace classes, `namespace_inventory({"app.BIA": BIA})`
from `bijux_ontology.interfaces.adapters.namespaces` retains qualified declaration
names and exact IRIs. Call `inventory.check(vocabulary,
owned_namespaces=("http://bia/",))` for local membership; add
`require_coverage=True` and `coverage_terms=selected_owned_iris` for reverse coverage.
Ownership and accepted RDF kinds are explicit caller contracts.

Dataset-backed `RdfSource` resources declare `graph_name=graph_iri`,
`graph_name="@default"`, or `union=True`. Imports, lookup resources and prepared
SHACL inputs retain that selection. `prepared.dataset()` still exports every
original graph; conflicting graph overrides fail explicitly.

Core RDF and vocabulary operations do not require PyLD. Install `bijux-ontology[jsonld]` for JSON-LD processing. Hosts pinned to another PyLD version can use the core without changing their dependency requirements; JSON-LD operations require a compatible runtime.

Use `SubjectIdentity(expected_iri=record_iri, allow_blank=False)` on a model
or individual `ModelUsage` to check effective subjects after base resolution.
`ModelUsage(model_id, reference=record_iri, graph=None)` joins local descriptions
for that named entity in the default graph; pass an exact graph IRI for a named
graph. Ordinary path bindings keep per-source-object required fields; `entity=True`
explicitly joins their facts. Unsupplied references remain unknown unless
`closed=True` requires a local description. Identity checks perform no OWL
equality, inequality or classification.

For JSON-LD, pass original usages and expectations to `check_context`. Reuse explicit resources and logical bases across parsing, serialisation and `prepare_jsonld` conversion. Default loaders do not fetch missing resources. Consumer-owned loaders or validation services have their own network behaviour; configuring this library does not make an entire application offline.

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

SHACL findings retain report-local identities, nested details and finite typed
property paths in `result.findings` and each generic issue’s `evidence`.
Supply `ShaclSource(prepared_usages, paths, source=...)` through `source_usages=`
to correlate actual JSON-LD selections. Multiple or unsupported locations remain
explicit; the full RDF report stays available through `report_graph()`.

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

Resolve finite selectors through `prepared.select(...)`. `SourceSelector` composes
`Property`, `ArrayItems` and `KeyedItems`; each step’s bounds apply to every selected
parent, independently of aggregate bounds. Check the returned concrete `paths`
with existing expectations. Required missing children name their selected parent.
`EntitySelector(types=(class_iri,), graph=None, coverage_unit="rdf_entity")` selects
joined asserted entities; `coverage_unit="source_occurrence"` instead retains each
describing object’s obligations. IDs/types are expanded identities, graph scope
is explicit, and optional zero matches remain non-applicable.
