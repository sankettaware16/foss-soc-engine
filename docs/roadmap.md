# TLSOC Engine Roadmap

Component roadmap for the parsing engine. The platform-wide roadmap lives in
[tlsoc — Roadmap](https://github.com/sankettaware16/tlsoc/blob/main/docs/roadmap.md).

## Current

Shipped and maintained:

- Five parsing strategies (`stateless`, `multi_match`, `stateful`, `json_map`,
  `xml_xpath`) with prematch gating and ReDoS linting.
- ECS-validated output with the two-timestamp model and offline GeoIP/ASN
  enrichment.
- At-least-once delivery, per-source DLQs, worker-pool scaling, live rule
  reload.
- Validation toolchain: `preflight.py`, `replicate.py`, `test_*` battery,
  golden-sample CI.
- Web UI console and native Kibana plugin.

## Next Release

- Ecosystem alignment: standardized documentation, community health files, and
  release tagging (this refactoring).
- Additional built-in rules with golden-sample coverage.
- Broader timestamp format coverage as new sources demand it.

## Future

- Optional direct-to-Elasticsearch output alongside file output.
- Rule packs distributed via the planned
  [tlsoc-parser-plugins](https://github.com/sankettaware16/tlsoc) repository,
  with CI-enforced golden samples.
- Enrichment plug-in points beyond GeoIP/ASN (e.g. local threat-list lookups).
- Prometheus-format metrics endpoint alongside `stats.json`.

## Long Term Vision

- First-class integration with the planned `tlsoc-dashboard` (pipeline health:
  EPS, DLQ rates, consumer lag in one place).
- Detection-as-code hooks so normalized events can drive shared detection rules
  across the TLSOC platform.

## Proposing changes

Open a [feature request](https://github.com/sankettaware16/foss-soc-engine/issues) using the template, or start with a
discussion issue for larger designs. Roadmap changes land here via pull request.
