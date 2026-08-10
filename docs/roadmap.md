# TLSOC Engine Roadmap

Component roadmap for the parsing engine. The platform-wide roadmap lives in
[tlsoc — Roadmap](https://github.com/sankettaware16/tlsoc/blob/main/docs/roadmap.md).

## Current

Shipped and maintained:

- Five parsing strategies (`stateless`, `multi_match`, `stateful`, `json_map`,
  `xml_xpath`) with prematch gating and ReDoS linting.
- ECS-validated output with the two-timestamp model and offline GeoIP/ASN
  enrichment on **both** endpoints (`source.ip` and `destination.ip`), plus
  automatic classification of ambiguous `.address` values into `.ip`/`.domain`.
- At-least-once delivery, per-source DLQs, worker-pool scaling, live rule
  reload.
- Validation toolchain: `preflight.py`, `replicate.py`, `test_*` battery,
  golden-sample CI.
- Benchmarking (`benchmark.py` + Web UI section): per-rule capacity and parse
  latency with live utilization %, real-time pipeline-lag check, and a
  historical lag/EPS timeline reconstructed from Elasticsearch — validated on
  a production 12-server squid onboarding (~35× traffic step, sub-second lag).
- Web UI console (with engine control and benchmarking) and native Kibana
  plugin.
- Elasticsearch index template pre-defining every engine field, so a first
  document can never mis-type a field for the whole index.

## Next Release

Priorities drawn from production operation:

- **Output-file lifecycle**: shipped logrotate profile (compression that keeps
  up with high-volume sources) and retention guidance — the output NDJSON is a
  buffer for the shipper, not an archive.
- **Template deploy helper**: merge the engine's field mappings into an
  *existing* higher-priority index template instead of assuming the shipped
  one wins (composable templates are winner-takes-all).
- **Per-source timezone overrides**: a `source_host → tz` map for producers
  whose clocks or zone labels are wrong, instead of per-rule workarounds.
- Optional `confluent-kafka` (librdkafka) consumer for higher per-core
  throughput on the Kafka leg.
- Additional built-in rules with golden-sample coverage; broader timestamp
  format coverage as new sources demand it.

## Future

- Optional direct-to-Elasticsearch output alongside file output.
- Rule packs distributed via the planned
  [tlsoc-parser-plugins](https://github.com/sankettaware16/tlsoc) repository,
  with CI-enforced golden samples.
- Enrichment plug-in points beyond GeoIP/ASN (e.g. local threat-list lookups).
- Prometheus-format metrics endpoint alongside `stats.json`.
- Native extensions (Rust) for profiled hot spots if a deployment ever
  outgrows per-core throughput — the YAML rule format stays the contract; the
  execution engine behind it is replaceable.

## Long Term Vision

- First-class integration with the planned `tlsoc-dashboard` (pipeline health:
  EPS, DLQ rates, consumer lag in one place).
- Detection-as-code hooks so normalized events can drive shared detection rules
  across the TLSOC platform.

## Proposing changes

Open a [feature request](https://github.com/sankettaware16/foss-soc-engine/issues) using the template, or start with a
discussion issue for larger designs. Roadmap changes land here via pull request.
