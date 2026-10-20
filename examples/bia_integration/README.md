# Native BIA integration

`adapter.check_metadata(metadata, models, executor=..., vocabulary=...,
required_bindings=...)` calls the real `BIAROCrateMetadata.to_dict()` once.
`check_output` accepts an already captured native document. Both require an
independent intended mapping inventory and leave biological and file-list
validation with BIA. No fixture, revision or model count is enforced by the adapter.

Each emitted entity's mapped fields receive an actual source/entity observation,
or an explicit missing, empty or unsupported result. `NativeOutputCheck.execution`
retains the full raw report, including unknown declaration guarantees;
`required` evaluates only the caller's explicit `BindingIdentity` policy.
An accepted selected contract never certifies the entire declaration inventory.

The qualification executable pins BIA revision
`17e0a80ac02ef1f4878aff54cd415c040129d0d5`, checks installed source parity and
runs both typical and minimal native crates through their real parser/producer.
Its required policy checks Study, Contributor and Dataset names and Study authors.
Predicate, coercion and missing-output mutations must fail that independent policy.
Native business failures remain BIA-owned. All exercised mappings and exclusions
are recorded in `native-adapter-samples.json`.

From the repository root, with Python 3.13:

```sh
mkdir -p artifacts/tmp artifacts/pip-cache
export TMPDIR="$PWD/artifacts/tmp" PIP_CACHE_DIR="$PWD/artifacts/pip-cache"
git clone https://github.com/BioImage-Archive/bia-ro-crate-validator.git artifacts/bia-source
git -C artifacts/bia-source checkout --detach 17e0a80ac02ef1f4878aff54cd415c040129d0d5
python3.13 -m venv artifacts/bia-host
artifacts/bia-host/bin/python -m pip install './packages/bijux-ontology' './packages/bijux-ontology-adapters[pydantic]' artifacts/bia-source
python3.13 -m venv artifacts/bia-worker
artifacts/bia-worker/bin/python -m pip install './packages/bijux-ontology[jsonld]'
artifacts/bia-host/bin/python examples/bia_integration/consumer.py --bia-source artifacts/bia-source --worker artifacts/bia-worker/bin/python --output artifacts/bia-integration
```

BIA keeps PyLD 2 in the host; the explicit worker uses PyLD 3. The complete
original context crosses the data-only boundary. Whole-sample observation pays
for finite differential expansion of every selected field; the harness declares
larger cumulative limits than the default, without changing adapter caller limits.
