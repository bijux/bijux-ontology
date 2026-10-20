# Consumer integration

Supply independent vocabulary snapshots, intended mappings and actual application output. The [supported entry points](../README.md#supported-python-entry-points) use explicit concrete imports; package roots do not re-export operations.

## Run a complete recipe

These scripts contain their inputs, positive checks and meaningful negative controls. Run them with the installed wheel in the selected consumer environment; they do not require source-path injection.

| Work | Runnable recipe | Dependencies |
| --- | --- | --- |
| Prepare, inspect and check a vocabulary inventory | [check_declarations.py](../examples/check_declarations.py) | Core |
| Declare model intent independently of output | [dataclass_declarations.py](../examples/dataclass_declarations.py) | Core |
| Check JSON-LD mappings and actual JSON-LD/native RDF output | [check_output.py](../examples/check_output.py) | `[jsonld]` |
| Validate supplied shapes and reuse fixed preparation | [validate_records.py](../examples/shacl/validate_records.py) | `[shacl]` |
| Check multilingual SKOS labels | [multilingual_vocabulary.py](../examples/multilingual_vocabulary.py) | Core |
| Assess an ontology upgrade against consumer usage | [catalogue_upgrade.py](../examples/catalogue_upgrade.py) | Core |

For example, after installing the wheel with `[jsonld,shacl]`:

```sh
python examples/check_output.py
python examples/shacl/validate_records.py
```

Run paths refer to the examples shipped in the source distribution. Preparation owns input snapshots; graph exports are copies. Choose local/import/lookup roles and dataset graph selections explicitly. Missing resources never trigger implicit network retrieval.

## Interpret the result

Keep raw findings, `report.verified` and policy acceptance separate. A complete negative answer is evidence of disagreement; unsupported or interrupted work cannot become verified through an exception. Use `check_contract` for a strict application gate, as shown in the output recipe. Existing application validation and reasoning gates remain required.

Samples cover only the output actually supplied. Required named fields can use `BindingIdentity` and `BindingObservation` from `bijux_ontology.declarations.bindings`, returned as `ContractEvidence` to an obligation's `required_bindings`. Observations must describe actual checks; repeating a successful field cannot cover a different required identity. Unknown and excluded associations remain inspectable and do not satisfy that requirement.

## Advanced contracts

The [contract reference](contracts.md) preserves detailed input ownership, supported fragments and limitations by topic:

- [RDF input and queries](contracts.md#rdf-input-and-queries), [transformations](contracts.md#rdf-transformations) and [stored/streamed data](contracts.md#stored-and-streamed-data).
- [Serializer samples](contracts.md#serializer-samples), [JSON-LD identity](contracts.md#json-ld-identity-and-values) and [model/schema projections](contracts.md#model-and-schema-projections).
- [SKOS publication](contracts.md#skos-publication-and-navigation), [lexical lookup](contracts.md#lexical-lookup-and-domain-adapters) and [mapping exchanges](contracts.md#mapping-exchanges-and-upgrades).
- [Shapes and repeated validation](contracts.md#shapes-and-repeated-validation), [structured data](contracts.md#structured-data-and-analysis) and [domain adapters](contracts.md#domain-integration-contracts).
- [Required acceptance](contracts.md#required-consumer-acceptance), [project execution](contracts.md#project-execution-and-resources) and [controlled acquisition](contracts.md#controlled-acquisition-and-publication).
