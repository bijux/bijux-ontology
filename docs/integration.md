# Consumer integration

Applications supply their own vocabulary snapshots, declarations and representative output. Use `TermDeclaration`, `ModelDeclaration`, `FieldDeclaration` and `ValueDeclaration` directly, or extract them through the optional Pydantic/LinkML adapters. The library does not discover application source or infer biological meaning. Runnable examples include ordinary dataclasses and independent catalogue workflows.

Select local, import and lookup scopes deliberately. Use coverage for owned namespaces and membership checks for external terms; lookup documents must not conceal missing local declarations. A required constructor field does not prove RDF emission: declare that guarantee explicitly and check representative serialised output with `check_emission`.

For finite RDFLib namespace classes, `namespace_inventory({"app.BIA": BIA})`
from `bijux_ontology.interfaces.adapters.namespaces` retains qualified declaration
names and exact IRIs. Call `inventory.check(vocabulary,
owned_namespaces=("http://bia/",))` for local membership; add
`require_coverage=True` and `coverage_terms=selected_owned_iris` for reverse coverage.
Ownership and accepted RDF kinds are explicit caller contracts.

Core RDF and vocabulary operations do not require PyLD. Install `bijux-ontology[jsonld]` for JSON-LD processing. Hosts pinned to another PyLD version can use the core without changing their dependency requirements; JSON-LD operations require a compatible runtime.

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

For supplied SHACL shapes, `ShaclCoverageContract(minimum_focus=1,
focus_nodes=record_iris, pairs=shape_focus_pairs)` from `bijux_ontology.shacl`
requires completed root target evaluations when passed as `coverage=` to
`validate_shapes`. Inspect `result.coverage` separately from `conforms` and
`conformance_report`; a targetless shape can conform while required coverage fails.
Nested conditional constraint execution is not inferred from root coverage.

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
