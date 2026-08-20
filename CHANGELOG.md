# Changelog

All notable changes to TLSOC Engine are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this repository adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- Joined the unified TLSOC ecosystem: standardized README, branding, and
  cross-repository links. The project is now presented as **TLSOC Engine**
  (historically *FOSS SOC Engine*); service names (`foss-soc`, `foss-soc-ui`)
  are unchanged for compatibility with existing deployments.
- Documentation reorganized into `docs/`: `WRITING_RULES.md` →
  `docs/writing-rules.md`, `WEB_UI_GUIDE.md` → `docs/web-ui-guide.md`,
  `UPGRADING.md` → `docs/upgrading.md`, plus new installation, configuration,
  architecture, deployment, and development guides.

### Added

- **Internal IP map enrichment** — GeoIP for your own address space. A plain
  YAML file (`database/internal_ips.yaml` beside the GeoIP databases, or a
  directory of files; config block `internal_map:`) declares which
  subnet/range/IP is which building, room, lab or owner, and matching
  `source.ip`/`destination.ip` values are enriched with those fields
  (`geo.name` + custom `site.*`). Range syntax: CIDR, `a.b.c.d-a.b.c.d`,
  short `a.b.c.d-N`, single IPs, lists; overlapping ranges layer with
  most-specific-wins, resolved once at load into binary-searchable segments
  with an LRU in front (<1 % throughput cost at 5,000 entries; one boolean
  when disabled). Hot-reloads on edit with registry-style fail-safe; the map
  file is `.gitignore`d (site-local data, never published); validated by
  `test_config.py`/`preflight.py` (ranges + the ECS field gate); new
  regression suite `test_internal_map.py`. Worked example in
  `examples/internal_ips.example.yaml`.
- **Web UI "IP Map" tab** — a visual editor for the internal IP map: add /
  edit / duplicate / delete entries through a form (range → name → fields),
  per-field live ECS checking, file-wide defaults editor, entry filter,
  instant range validation, one-click test lookups ("Try an IP"), and a Raw
  YAML mode for power users. Saves hot-reload into the running engine.
- Community health files: contributing guide, security policy, code of conduct,
  issue templates, and a pull request template.
- Component roadmap (`docs/roadmap.md`).
- **Monitor: per-source DLQ folders.** "Recent errors / DLQ" now groups
  dead-letters into one collapsible folder per source program (`squid`,
  `nginx`, `postfix`, … — new sources appear automatically), each with its
  own recent tail, a failure-reason breakdown, on-disk size, and
  newest-failure timestamp (UTC-labeled), ordered freshest-trouble-first —
  so a storming source can no longer bury the few lines from another source
  an analyst is hunting. Expand/collapse choices survive "Load latest".
  The `/api/monitor/dlq` endpoint behind it was hardened: bounded tail
  reads (256 KiB per file, never whole files, so a 200 MB DLQ no longer
  spikes the UI's RAM), rotated `.json.1` files are included (a source that
  stormed and then went quiet stays visible), `raw`/`error` ship as
  previews (400/80 chars; the full line stays on disk), and a hostile flood
  of forged program names is capped (400 files / 50 sources per request,
  with the leftovers counted in the response and shown in the UI). The old
  flat `entries` key is still returned for existing consumers.

### Fixed

- **Elasticsearch index template now maps the `destination.*` enrichment
  side** (`destination.geo.*` incl. `geo_point` location, `destination.as.*`,
  and `geo.name` on both sides). Previously only `source.*` was declared, so
  `destination.geo.location` fell to dynamic mapping (object of floats) and
  could never be used as a Kibana Maps layer for outbound/proxy traffic
  (squid, modsec, openvas, suricata). Regenerating also picked up
  `network.bytes` and `squid.forwarded_for`, which were missing from the
  checked-in template. Existing indices keep the old mapping (field types
  cannot change in place) — new indices are correct once the updated template
  is loaded; reindex only if Maps on historical data are needed.
  ([#8](https://github.com/sankettaware16/foss-soc-engine/pull/8), thanks
  @HelixY2J)
