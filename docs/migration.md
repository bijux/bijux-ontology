# From the initial 0.1 API

The package is still unpublished. These changes replace the initial local implementation; they are not a claim that a public release has migrated.

- CLI commands remain supported. Import Python symbols from their documented domain modules; root and `interfaces.api` convenience exports have been removed. SHACL symbols likewise live in their owning shape modules. Framework extraction lives under `interfaces.adapters`. Concrete owners are `rdf`, `jsonld`, `owl`, `documents`, `vocabulary`, `declarations`, `models`, `skos`, `data` and `shacl`. Composed checks live under `workflows`; former local module paths are not compatibility APIs.
- Owned local file inventories and revision verification live under `resources.files`, shared by disk stores and explicit native backends.
- Shared input failures and reports live under `diagnostics`; limits and subprocess lifetimes live under `execution`. Strict JSON syntax, concrete JSON paths and offline schema validation live under `json`, independently of JSON-LD semantics. Import their concrete modules directly.
- JSON-LD processing, immutable evidence, usage checks and expectations have distinct owners under `jsonld.processing`, `jsonld.evidence`, `jsonld.usages` and `jsonld.contracts`. Finite OWL encoding checks and asserted relations live under `owl.encoding` and `owl.relations`. Declaration classes are imported from their concrete modules; the eager declaration export inventory has been removed.
- SHACL SPARQL catalogs and query dependencies live under `shacl.sparql`; completed root-target coverage belongs to `shacl.targets.coverage`. Import owned findings and outcomes from `shacl.evidence.findings` and `shacl.evidence.results`; checking no longer re-exports those classes. SHACL domain packages likewise use their concrete declaration and operation modules instead of eager export inventories. Directed component traversal now charges shared workspace; shape recursion and vocabulary cycle policies remain separate.
- Portable exact RDF records, record codecs and snapshot materialization live under `rdf.records`, independently of patch transactions. The documented patch entry points retain their existing concrete record types.
- Invalid declarations, duplicate JSON keys and conflicting model identities now fail before semantic shortcuts. Use `terms_from_data`, `models_from_data` and `expectations_from_data` for the same validated input shapes as the CLI.
- `Report` records requested work, completion, regime and child scopes. Required empty checks fail; explicitly optional empty scopes are not applicable. Inspect `verified` for agreement and `exit_code(policy=...)` for process acceptance. A warning or permitted unknown remains visible and unverified.
- A trait set to `None` is unchecked. Use `ABSENT` to require absence; JSON declaration codecs represent it as `{ "absent": true }`. Language, direction, datatype, identifier and ordered values are separate comparisons.
- Parsed RDFLib graphs are copied. `RdfSource` separates content, syntax, logical identity and effective URL. Strings are content; use `Path` for files. Datasets require a graph selection or explicit union. Exact literal preservation may reject an RDFLib representation that would normalise text.
- Local, import and lookup ownership is explicit. Lookup copies cannot conceal missing local declarations. Supply every required import and context; no implicit network fallback is provided.
- Model field `emission="guaranteed"` is an explicit output contract, independent of constructor requirements. Unknown custom serializers remain unverified. Use `check_emission` for representative actual output.

```python
from bijux_ontology.absence import ABSENT
from bijux_ontology.jsonld.contracts.expectations import ContextExpectation
from bijux_ontology.jsonld.usages.checking import check_context

report = check_context(
    {"@context": {"label": "https://example.org/label"}, "label": "Colour"},
    [ContextExpectation(("label",), "https://example.org/label", language=ABSENT)],
)
assert report.verified
```

Common corrections:

| Diagnostic | What to inspect |
| --- | --- |
| `vocabulary.term_missing` | Exact IRI and selected source; do not fabricate a declaration to match a typo |
| `context.mapping_mismatch` | Expected and actual expanded IRIs; choose the intended context or expectation |
| Invalid datatype declaration | Supply an absolute datatype IRI before interpreting ranges |
| Missing required import | Supply the authoritative snapshot under its logical URL |
| `models.expression_unverified` | Relevant unsupported construct; retain the existing reasoning gate |

`bijux-ontology explain CODE` gives installed corrective guidance. Upgrade reports retain original before/after issues: a persistent finding is distinct from an introduced one, and absent prior evidence cannot prove a new break. CLI `contract_verdict` covers selected application contracts; `usage_meaning` separately reports representative context differences.
