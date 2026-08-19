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
