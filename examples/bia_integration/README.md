# Native BIA integration

This executable qualifies the unchanged native BIA producer at revision
`17e0a80ac02ef1f4878aff54cd415c040129d0d5`. Run these commands from the
Bijux Ontology repository root with Python 3.13:

```sh
mkdir -p artifacts/tmp artifacts/pip-cache
export TMPDIR="$PWD/artifacts/tmp" PIP_CACHE_DIR="$PWD/artifacts/pip-cache"
git clone https://github.com/BioImage-Archive/bia-ro-crate-validator.git artifacts/bia-source
git -C artifacts/bia-source checkout --detach 17e0a80ac02ef1f4878aff54cd415c040129d0d5
python3.13 -m venv artifacts/bia-host
artifacts/bia-host/bin/python -m pip install './packages/bijux-ontology[pydantic]' artifacts/bia-source
python3.13 -m venv artifacts/bia-worker
artifacts/bia-worker/bin/python -m pip install './packages/bijux-ontology[jsonld]'
artifacts/bia-host/bin/python examples/bia_integration/consumer.py --bia-source artifacts/bia-source --worker artifacts/bia-worker/bin/python --output artifacts/bia-integration
```

The host keeps BIA's PyLD 2 dependency. The worker installs the JSON-LD extra
with PyLD 3. BIA's real parser and business validation run before
`BIAROCrateMetadata.to_dict()`. The native parser uses its normal RO-Crate
profile and declared context resources; the worker receives the complete native
context. Installed BIA code is compared with the pinned source checkout.

The typical crate produces 18 records across 9 model classes, exercising 49
mapped fields, 20 with nonempty values. Extraction covers all 25 registered
models, 100 field mappings and 79 namespace declarations. The required example
contract selects 4 named entities across Study, Contributor and Dataset,
requiring their names and the Study's authors. Bindings use established RDF
identities, asserted types and the default graph. Worker selectors require each
selected type's own named-entity coverage. Local/blank keys and other named
entities remain explicitly outside the selected contracts. Shuffling `@graph` preserves
the results; a removed name fails at its entity identity.

The output directory contains the complete native document, declarations,
selected contracts, coverage and raw reports. Unknown guarantees in the full
mapping inventory remain visible. `selected_contract_verified` covers the
selected contracts; the full model report can remain unverified. The executable
also checks predicate, reverse mapping, datatype, coercion, namespace and
emission mutations, native FileList/business failures, and an independently
proved named relation beside a required unresolved restriction.

Consumer policy lives in `selected_contracts`. Extend it with your own required
fields and binding counts. BIA continues to own biological and file-list rules.
