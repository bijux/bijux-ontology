# Installed CKAN/DCAT integration

This executable qualifies CKAN `2.11.6`, ckanext-dcat `2.4.4`, RDFLib `7.6.0`,
PyLD `3.3.0` and pySHACL `0.40.1` on Python `3.12.13`. Install CKAN's native
prerequisites, including libpq development headers and libmagic, using its
[installation instructions](https://docs.ckan.org/en/2.11/maintaining/installing/install-from-source.html).
From the repository root:

```sh
mkdir -p artifacts/consumers artifacts/tmp artifacts/pip-cache
export TMPDIR="$PWD/artifacts/tmp" PIP_CACHE_DIR="$PWD/artifacts/pip-cache"
python3.12 -m venv artifacts/consumers/ckan-env
artifacts/consumers/ckan-env/bin/python -m pip install 'ckan[requirements]==2.11.6' 'ckanext-dcat==2.4.4' './packages/bijux-ontology[jsonld,shacl]'
curl -fsSL https://raw.githubusercontent.com/ckan/ckan/a22d24505d1a5f75820d43cc3b0656c2c6b925a2/ckan/test-core.ini -o artifacts/consumers/ckan-test.ini
artifacts/consumers/ckan-env/bin/python -I -B examples/ckan_integration/consumer.py --ckan-ini artifacts/consumers/ckan-test.ini --output artifacts/ckan-integration
```

The normal CKAN configuration loader and core/DCAT declarations process the
supplied INI. This pure serializer integration uses complete package and resource
identities, so database, Solr and Redis services are unnecessary. It does not
qualify a running CKAN application. Existing installations can supply their own
INI with `--ckan-ini`.

The installed `RDFSerializer` runs `graph_from_dataset` and `serialize_dataset`
with the `euro_dcat_ap_2` profile. Its actual dataset, distribution and temporal
blank node pass declaration, required emission and SHACL checks. Turtle and
JSON-LD retain the same RDF graph; an explicit union selects the producer's
conjunctive graph. Context-controlled export returns separately checked JSON-LD.
Changing the declared title predicate or removing the real resource's required
download URL fails the corresponding checks.

`profile.ttl` is a finite consumer-owned declaration inventory and shape contract;
it does not claim complete DCAT-AP validation. Qualification reports, actual
serializer output and the checked compacted document go to the requested output
directory. Catalogue policy stays in this example and uses ordinary public APIs.
